"""微信读书 API：签名算法 + 阅读上报 + Cookie 续期。

v2 关键改造：
- `read_once(book_id, chapter_uid)` 参数化，选书选章交给 scheduler
- 仅支持 CDP 模式（Cookie 来自 CDP 抓取）
- 依赖 LocalDB 提供的 bookId / chapterUid
"""
from __future__ import annotations

import hashlib
import json
import random
import secrets
import threading
import time
import urllib.parse
from typing import Any

import requests
from PySide6.QtCore import QObject, Signal

from app.core.config import ConfigStore
from app.utils.logger import get_logger

log = get_logger(__name__)

# ---------- URL 常量 ----------
READ_URL = "https://weread.qq.com/web/book/read"
RENEW_URL = "https://weread.qq.com/web/login/renewal"
CHAPTER_SYNC_URL = "https://weread.qq.com/web/book/chapterInfos"
SHELF_SYNC_URL = "https://weread.qq.com/web/shelf/sync"  # 轻量 GET 健康检查

# ---------- 签名常量 ----------
KEY = "3c5c8717f3daf09iop3423zafeqoi"

# 默认 appId/ps/pc（三体固定模板兜底，服务端不校验 b/c 从属关系）
DEFAULT_APP_ID = "wb182564874603h266381671"
DEFAULT_PS = "4ee326507a65a465g015fae"
DEFAULT_PC = "aab32e207a65a466g010615"

# 章节内容片段（sm 字段，反爬噪声）
DEFAULT_SM_SNIPPETS: list[str] = [
    "19聚会《三体》网友的聚会地点是一处僻静",
    "寂静的峰峦间，墨色的云在天空缓缓移动",
    "夜色中，飞船的轮廓慢慢浮现在海平面上方",
    "在那个年代，人们还相信时间可以被拉长",
    "月光下的书页被风掀起几行清冷的诗句",
    "他合上笔记本，望向窗外无尽的星野",
    "远处的钟敲响了第九下，房间里一片安静",
    "她把书推到一边，指尖还残留着墨香",
]

# 24 位 ObjectId 章节池兜底（read API 的 "c" 字段需要此格式，不接受小整数 chapterUid）
# 服务端不校验 b/c 从属关系，可用任意 ObjectId 配合任意 webBookId
DEFAULT_CHAPTERS: list[str] = [
    "ecc32f3013eccbc87e4b62e",
    "a87322c014a87ff679a21ea",
    "e4d32d5015e4da3b7fbb1fa",
    "16732dc0161679091c5aeb1",
    "8f132430178f14e45fce0f7",
    "c9f326d018c9f0f895fb5e4",
    "45c322601945c48cce2e120",
    "d3d322001ad3d9446802347",
    "65132ca01b6512bd43d90e3",
    "c20321001cc20ad4d76f5ae",
    "c51323901dc51ce410c121b",
    "aab325601eaab3238922e53",
    "9bf32f301f9bf31c7ff0a60",
    "c7432af0210c74d97b01b1c",
    "70e32fb021170efdf2eca12",
    "6f4322302126f4922f45dec",
    "7f632b502707f6ffaa6bf2e",
]

# renewal 请求变体
COOKIE_DATA_VARIANTS: list[dict[str, Any]] = [
    {"rq": "%2Fweb%2Fbook%2Fread", "ql": False},
    {"rq": "%2Fweb%2Fbook%2Fread", "ql": True},
    {"rq": "%2Fweb%2Fbook%2Fread"},
]

# renewal 硬失效错误码（用户不存在/登录超时/签名失效）
_HARD_ERR_CODES = frozenset({-2010, -2012, -2002, -2003})


def encode_data(data: dict[str, Any]) -> str:
    """按官方格式编码：sorted keys + URL quote。"""
    return "&".join(
        f"{k}={urllib.parse.quote(str(data[k]), safe='')}" for k in sorted(data.keys())
    )


def create_id(book_id: str) -> str:
    """将数字 bookId 转换为 webBookId（24 位十六进制）。

    read API 的 "b" 字段需要 webBookId 格式，不接受原始数字 bookId。
    算法移植自 cdp_test_standalone.py（参考 WeReadX 实现）。
    """
    md5_hash = hashlib.md5(book_id.encode()).hexdigest()
    str_sub = md5_hash[:3]
    if book_id.isdigit():
        c = []
        for i in range(0, len(book_id), 9):
            chunk = book_id[i:i + 9]
            c.append(format(int(chunk), 'x'))
        fa = ['3', c]
    else:
        d = ''
        for ch in book_id:
            d += format(ord(ch), 'x')
        fa = ['4', [d]]
    str_sub += fa[0]
    str_sub += '2' + md5_hash[-2:]
    for m in fa[1]:
        n = format(len(m), 'x')
        if len(n) == 1:
            n = '0' + n
        str_sub += n + m
    if len(str_sub) < 20:
        str_sub += md5_hash[:20 - len(str_sub)]
    str_sub += hashlib.md5(str_sub.encode()).hexdigest()[:3]
    return str_sub


def cal_hash(input_string: str) -> str:
    """JS 逆向还原的 s 字段算法（FNV-1a 变体）。"""
    a = 0x15051505
    b = a
    length = len(input_string)
    i = length - 1
    while i > 0:
        a = 0x7FFFFFFF & (a ^ (ord(input_string[i]) << ((length - i) % 30)))
        b = 0x7FFFFFFF & (b ^ (ord(input_string[i - 1]) << (i % 30)))
        i -= 2
    return f"{(a + b) & 0xFFFFFFFF:08x}"


class WeReadApi(QObject):
    """微信读书 API 封装（v2 参数化版）。

    对外主要入口：
      - set_session(headers, cookies, cookies_raw) → 写入登录态
      - check_session() -> bool                    → 轻量校验 Cookie
      - ensure_session() -> bool                   → 失效时尝试 renewal
      - read_once(book_id, chapter_uid) -> bool    → 单次阅读上报（参数化）
    """

    message = Signal(str)
    warning = Signal(str)
    error = Signal(str)
    # 登录态失效：HARD_INVALID=必须扫码 / SOFT_FAIL=可重试
    cookie_invalid = Signal(str, str)

    def __init__(self, config: ConfigStore | None = None) -> None:
        super().__init__()
        self._cfg = config or ConfigStore()
        self._lock = threading.RLock()
        self._session = requests.Session()
        self._last_read_ts: int = 0
        self._update_from_config()

    # ---------- 基础：与 config 同步 ----------
    @staticmethod
    def _cookie_domains_for(entry: dict[str, Any]) -> list[str]:
        """根据 cookies_raw 中的单条记录推导要种到的域列表。"""
        raw_domain = str(entry.get("domain") or "").strip()
        if not raw_domain:
            return [".qq.com", "weread.qq.com"]
        normalized = raw_domain if raw_domain.startswith(".") else "." + raw_domain
        out: list[str] = [normalized]
        try:
            bare = normalized.lstrip(".")
            if bare and bare not in out:
                out.append(bare)
        except Exception:  # noqa: BLE001
            pass
        return out

    def _plant_cookies(
        self,
        *,
        cookies_dict: dict[str, str],
        cookies_raw: list[dict[str, Any]] | None,
    ) -> None:
        """按真实属性把 cookies 种进 requests.Session（必须在 self._lock 内）。"""
        self._session.cookies.clear()
        seen_pairs: set[tuple[str, str]] = set()
        if cookies_raw:
            for entry in cookies_raw:
                name = str(entry.get("name") or "")
                value = str(entry.get("value") or "")
                if not name or not value:
                    continue
                path = str(entry.get("path") or "/") or "/"
                secure = bool(entry.get("secure"))
                rest = {"path": path}
                if secure:
                    rest["secure"] = True  # type: ignore[assignment]
                for domain in self._cookie_domains_for(entry):
                    try:
                        self._session.cookies.set_cookie(
                            requests.cookies.create_cookie(
                                name=name, value=value, domain=domain, **rest
                            )
                        )
                    except Exception:  # noqa: BLE001
                        try:
                            self._session.cookies.set(name, value, domain=domain, path=path)
                        except Exception:  # noqa: BLE001
                            pass
                    seen_pairs.add((name, domain))
        # 兼容 cookies_dict 中漏网之鱼（无 raw 记录降级双域盲种）
        for k, v in cookies_dict.items():
            v_s = str(v) if v is not None else ""
            if not v_s:
                continue
            for domain in (".qq.com", "weread.qq.com"):
                if (k, domain) in seen_pairs:
                    continue
                try:
                    self._session.cookies.set(k, v_s, domain=domain, path="/")
                except Exception:  # noqa: BLE001
                    pass

    def _update_from_config(self) -> None:
        """从 config 载入 headers + cookies 到 Session。"""
        with self._lock:
            headers = self._cfg.get("headers", {}) or {}
            clean_headers = {str(k): str(v) for k, v in headers.items()}
            # 强制 Content-Type
            for bad in ("content-type", "Content-Type", "CONTENT-TYPE"):
                clean_headers.pop(bad, None)
            clean_headers["Content-Type"] = "application/json;charset=UTF-8"
            self._session.headers.clear()
            self._session.headers.update(clean_headers)
            cookies_dict = self._cfg.get_cookies_dict()
            cookies_raw = self._cfg.get_cookies_raw()
            self._plant_cookies(cookies_dict=cookies_dict, cookies_raw=cookies_raw)

    def _augment_headers_baggage(self) -> None:
        """动态合成 baggage / sentry-trace header（反风控必需）。"""
        cookies_dict = self._cfg.get_cookies_dict()
        cookies_raw = self._cfg.get_cookies_raw()

        def _find_raw(name: str) -> str:
            for r in cookies_raw:
                if str(r.get("name") or "") == name:
                    return str(r.get("value") or "")
            return ""

        qimei_42 = cookies_dict.get("_qimei_uuid42") or _find_raw("_qimei_uuid42")
        qimei_36 = cookies_dict.get("qimei36") or _find_raw("qimei36")
        trace_id = secrets.token_hex(16)  # 32 hex chars
        parts = [
            "sentry-public_key=9e103f84d02c4b05a318c0a2d72d3e3f",
            f"sentry-trace_id={trace_id}",
            "sentry-environment=production",
            "sentry-release=weread-web%401.0.0",
        ]
        if qimei_42:
            parts.append(f"qimei_uuid42={urllib.parse.quote(qimei_42, safe='')}")
        if qimei_36:
            parts.append(f"qimei36={urllib.parse.quote(qimei_36, safe='')}")
        baggage = ",".join(parts)
        with self._lock:
            self._session.headers["baggage"] = baggage
            span = secrets.token_hex(8)
            self._session.headers["sentry-trace"] = f"{trace_id}-{span}-1"

    # ---------- 登录态写入 ----------
    def set_session(
        self,
        headers: dict[str, str],
        cookies: dict[str, str],
        cookies_raw: list[dict[str, Any]] | None = None,
    ) -> None:
        """登录完成后被 UI 层调用，写入配置并刷新 Session。"""
        clean_headers: dict[str, str] = {
            k: str(v)
            for k, v in headers.items()
            if k.lower() not in ("cookie", "content-length", "host")
        }
        clean_cookies: dict[str, str] = {str(k): str(v) for k, v in cookies.items() if v}
        clean_raw: list[dict[str, Any]] = []
        if cookies_raw:
            for r in cookies_raw:
                name = str(r.get("name") or "")
                value = str(r.get("value") or "")
                if name and value:
                    clean_raw.append(dict(r))
        # Content-Type 强制覆写（避免残留错值导致 -2007）
        for bad in ("content-type", "Content-Type", "CONTENT-TYPE"):
            clean_headers.pop(bad, None)
        clean_headers["Content-Type"] = "application/json;charset=UTF-8"
        self._cfg.set("headers", clean_headers)
        self._cfg.set("cookies_raw", clean_raw, auto_save=False)
        self._cfg.set("cookies", clean_cookies, auto_save=True)
        with self._lock:
            self._plant_cookies(cookies_dict=clean_cookies, cookies_raw=clean_raw)
            self._session.headers.update(clean_headers)
        self._augment_headers_baggage()
        log.info(
            "登录态已更新：dict=%d, raw=%d, headers=%d",
            len(clean_cookies), len(clean_raw), len(clean_headers),
        )

    # ---------- 健康检查 ----------
    def check_session(self, *, timeout: int = 8) -> bool:
        """轻量校验 Cookie 是否仍有效。

        成功条件（任一）：
          - chapter_sync：HTTP 200 + JSON errCode==0 或含 books/chapters/data[0].bookId
          - shelf_sync：HTTP 200 + 含真实同步字段且无错误标记
          - read 回退：HTTP 200 + JSON succ==1
        """
        cookies_dict = self._cfg.get_cookies_dict()
        if not (cookies_dict.get("wr_vid") and cookies_dict.get("wr_skey")):
            log.debug(
                "check_session 前置失败：wr_vid=%s wr_skey=%s",
                bool(cookies_dict.get("wr_vid")),
                bool(cookies_dict.get("wr_skey")),
            )
            return False
        self._augment_headers_baggage()
        json_ct = {"Content-Type": "application/json;charset=UTF-8"}

        # 1) chapter_sync（强语义）
        try:
            with self._lock:
                resp = self._session.post(
                    CHAPTER_SYNC_URL,
                    headers=json_ct,
                    data=json.dumps({"bookIds": ["3300060341"]}, separators=(",", ":")),
                    timeout=timeout,
                )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                if isinstance(data, dict):
                    err = data.get("errCode")
                    data_list = data.get("data") if isinstance(data.get("data"), list) else None
                    first_item = data_list[0] if (data_list and len(data_list) > 0) else None
                    first_has_book = isinstance(first_item, dict) and "bookId" in first_item
                    field_ok = (
                        ("books" in data) or ("chapters" in data) or bool(first_has_book)
                    )
                    if (err == 0) or field_ok:
                        log.info("check_session 命中 chapter_sync：errCode=%s", err)
                        return True
                    log.warning(
                        "check_session chapter_sync 失败：errCode=%s keys=%s",
                        err, list(data.keys())[:10],
                    )
        except requests.RequestException as exc:
            log.warning("check_session chapter_sync 请求异常：%s", exc)

        # 2) shelf_sync（强语义）
        try:
            with self._lock:
                resp = self._session.get(SHELF_SYNC_URL, timeout=timeout)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                if isinstance(data, dict):
                    err = data.get("errCode")
                    valid_fields = {"books", "shelfBookIds", "updated", "syncKey", "archiveBooks", "collapsedBookIds"}
                    has_valid = bool(valid_fields & data.keys())
                    has_error_marker = (
                        isinstance(err, int) and err < 0
                    ) or (
                        "errMsg" in data and bool(data.get("errMsg"))
                    )
                    no_err = (err == 0) or (err is None and not has_error_marker)
                    if has_valid and no_err:
                        log.info("check_session 命中 shelf_sync：errCode=%s", err)
                        return True
                    log.warning(
                        "check_session shelf_sync 失败：errCode=%s has_valid=%s",
                        err, has_valid,
                    )
        except requests.RequestException as exc:
            log.warning("check_session shelf_sync 请求异常：%s", exc)

        return False

    def ensure_session(self) -> bool:
        """若当前会话失效，尝试通过 renewal 接口刷新 wr_skey。

        CDP 模式判定：
          1) check_session 通过 → True
          2) wr_skey 缺失或长度<8 → HARD_INVALID
          3) _renew_wr_skey 拿到新 wr_skey + check_session 通过 → True
          4) 拿到新 wr_skey 但 check_session 仍失败 → HARD_INVALID
          5) renewal 失败 → HARD / SOFT（由 _renew_wr_skey 判定）
        """
        if self.check_session():
            return True
        log.info("当前会话已失效，开始刷新 wr_skey ...")
        self.message.emit("登录态失效，正在刷新 Cookie ...")

        cookies_dict = self._cfg.get_cookies_dict()
        wr_skey_val = cookies_dict.get("wr_skey", "")
        if not wr_skey_val or len(str(wr_skey_val)) < 8:
            reason = "wr_skey 缺失或长度不足，必须重新扫码"
            log.error("登录态硬失效：%s", reason)
            self.error.emit(f"登录态硬失效：{reason}")
            self.cookie_invalid.emit("HARD_INVALID", reason)
            return False

        result = self._renew_wr_skey()
        if not result.get("ok"):
            sev = result.get("severity", "SOFT_FAIL")
            reason = result.get("reason", "未知原因")
            if sev == "HARD_INVALID":
                log.error("登录态硬失效：%s", reason)
                self.error.emit(f"登录态硬失效：{reason}")
            else:
                log.warning("Cookie 刷新失败（软）：%s", reason)
                self.error.emit(f"Cookie 刷新失败（软）：{reason}，后续会再试")
            self.cookie_invalid.emit(sev, reason)
            return False

        # 续命成功：双写 cookies dict + raw
        new_skey = result["wr_skey"]
        cookies_raw = self._cfg.get_cookies_raw()
        cookies_dict["wr_skey"] = new_skey
        for entry in cookies_raw:
            if str(entry.get("name")) == "wr_skey":
                entry["value"] = new_skey
        if not any(str(e.get("name")) == "wr_skey" for e in cookies_raw):
            cookies_raw.append({
                "name": "wr_skey",
                "value": new_skey,
                "domain": ".weread.qq.com",
                "path": "/",
                "secure": True,
                "httpOnly": False,
                "sameSite": 0,
            })
        self._cfg.set("cookies_raw", cookies_raw, auto_save=False)
        self._cfg.set("cookies", cookies_dict, auto_save=True)
        with self._lock:
            self._plant_cookies(cookies_dict=cookies_dict, cookies_raw=cookies_raw)
        log.info("wr_skey 刷新成功（长度=%d）：%s***", len(new_skey), new_skey[:4])
        self.message.emit(f"Cookie 刷新成功（新 wr_skey len={len(new_skey)}: {new_skey[:4]}***）")
        if self.check_session():
            return True
        reason = f"renewal 拿到新 wr_skey（{new_skey[:4]}***）但 check_session 仍失败；账号可能被风控或多端登出"
        log.error("登录态硬失效：%s", reason)
        self.error.emit(f"登录态硬失效：{reason}")
        self.cookie_invalid.emit("HARD_INVALID", reason)
        return False

    def _renew_wr_skey(self) -> dict:
        """刷新 wr_skey，返回结构化结果。

        返回：{ok, wr_skey, severity, reason, http, errCode}
          severity ∈ OK / SOFT_FAIL / HARD_INVALID
        """
        self._augment_headers_baggage()
        json_ct = {"Content-Type": "application/json;charset=UTF-8"}

        last_http: int | None = None
        last_errcode: int | None = None
        last_reason = "未知原因"
        had_request_exception = False

        for idx, payload in enumerate(COOKIE_DATA_VARIANTS):
            try:
                with self._lock:
                    resp = self._session.post(
                        RENEW_URL,
                        headers=json_ct,
                        data=json.dumps(payload, separators=(",", ":")),
                        timeout=10,
                    )
                last_http = resp.status_code
                body_text = resp.text or ""
                log.info("renewal 变体 %d：HTTP=%s resp(前200)=%s", idx, resp.status_code, body_text[:200])

                # 解析 errCode
                errcode: int | None = None
                try:
                    body_json = json.loads(body_text)
                    if isinstance(body_json, dict):
                        ec = body_json.get("errCode")
                        if isinstance(ec, int):
                            errcode = ec
                            last_errcode = ec
                except (ValueError, TypeError):
                    pass

                # 硬失效 a：errCode 命中已知根失效错误码
                if errcode in _HARD_ERR_CODES:
                    last_reason = f"变体{idx} 服务端拒绝续命 errCode={errcode}"
                    log.error("renewal 变体 %d 硬失效：errCode=%s", idx, errcode)
                    return {
                        "ok": False, "wr_skey": "", "severity": "HARD_INVALID",
                        "reason": last_reason, "http": last_http, "errCode": errcode,
                    }

                # 硬失效 b：302/303 跳登录页
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location", "") or ""
                    if "login" in loc.lower():
                        last_reason = f"变体{idx} HTTP {resp.status_code} 跳转登录页"
                        log.error("renewal 变体 %d 硬失效：HTTP %s 跳登录页", idx, resp.status_code)
                        return {
                            "ok": False, "wr_skey": "", "severity": "HARD_INVALID",
                            "reason": last_reason, "http": last_http, "errCode": errcode,
                        }

                # 尝试从 resp.cookies / Set-Cookie 抓 wr_skey
                wr_skey = resp.cookies.get("wr_skey")
                if wr_skey:
                    log.info("renewal 变体 %d 命中 resp.cookies.wr_skey len=%d", idx, len(str(wr_skey)))
                    return {
                        "ok": True, "wr_skey": str(wr_skey), "severity": "OK",
                        "reason": f"变体{idx} 续命成功", "http": last_http, "errCode": errcode,
                    }
                set_cookie = resp.headers.get("Set-Cookie", "")
                if "wr_skey=" in set_cookie:
                    for segment in set_cookie.split(","):
                        for part in segment.split(";"):
                            pair = part.strip()
                            if pair.startswith("wr_skey="):
                                val = pair.split("=", 1)[1].strip()
                                if val:
                                    log.info("renewal 变体 %d 命中 Set-Cookie wr_skey len=%d", idx, len(val))
                                    return {
                                        "ok": True, "wr_skey": val, "severity": "OK",
                                        "reason": f"变体{idx} 续命成功（Set-Cookie）",
                                        "http": last_http, "errCode": errcode,
                                    }

                # 软失败：5xx
                if 500 <= resp.status_code < 600:
                    last_reason = f"变体{idx} HTTP {resp.status_code} 服务端错误"
                    continue
                # 硬失效 c：body 含"登录"+"扫码"
                if "登录" in body_text and "扫码" in body_text:
                    last_reason = f"变体{idx} 响应体含登录扫码文案"
                    log.warning("renewal 变体 %d 疑似硬失效：%s", idx, last_reason)
                    continue
                last_reason = f"变体{idx} HTTP {resp.status_code} 无 wr_skey"
            except requests.RequestException as exc:
                had_request_exception = True
                last_reason = f"变体{idx} 请求异常：{exc}"
                log.warning("renew 变体 %d 请求失败：%s", idx, exc)

        # 3 变体全失败：汇总判定
        if "登录扫码" in last_reason or "登录页" in last_reason:
            return {
                "ok": False, "wr_skey": "", "severity": "HARD_INVALID",
                "reason": last_reason + "（3 变体均失败，账号疑似已登出）",
                "http": last_http, "errCode": last_errcode,
            }
        if had_request_exception or (last_http is not None and 500 <= last_http < 600):
            return {
                "ok": False, "wr_skey": "", "severity": "SOFT_FAIL",
                "reason": last_reason + "（疑似网络/服务端抖动，可重试）",
                "http": last_http, "errCode": last_errcode,
            }
        return {
            "ok": False, "wr_skey": "", "severity": "SOFT_FAIL",
            "reason": last_reason + "（3 变体均未拿到 wr_skey）",
            "http": last_http, "errCode": last_errcode,
        }

    # ---------- 阅读上报 ----------
    def _build_payload(
        self,
        book_id: str,
        chapter_uid: str,
        *,
        last_time: int | None = None,
        rt_override: int | None = None,
    ) -> dict[str, Any]:
        """构造单次阅读上报 payload（参数化版）。

        v2 改造：book_id / chapter_uid 由 scheduler 传入。
        rt 区间：[25, 60] 风控安全区间。
        """
        now = int(time.time())
        ts = int(now * 1000) + random.randint(0, 999)
        rn = random.randint(0, 9999)
        if rt_override is not None:
            rt = max(25, min(60, int(rt_override)))
        elif last_time is None:
            rt = random.randint(25, 60)
        else:
            diff = int(now - last_time)
            rt = max(25, min(60, diff))

        # "c" 字段需要 24 位 ObjectId 格式；Skill API 返回小整数 chapterUid 时用 DEFAULT_CHAPTERS 兜底
        c_value = str(chapter_uid)
        if c_value.isdigit() and len(c_value) < 20:
            c_value = random.choice(DEFAULT_CHAPTERS)

        data: dict[str, Any] = {
            "appId": DEFAULT_APP_ID,
            "b": create_id(str(book_id)) if str(book_id).isdigit() else str(book_id),
            "c": c_value,
            "ci": random.randint(1, 200),
            "co": random.randint(100, 800),
            "sm": random.choice(DEFAULT_SM_SNIPPETS),
            "pr": random.randint(1, 200),
            "rt": rt,
            "ts": ts,
            "rn": rn,
            "sg": hashlib.sha256(f"{ts}{rn}{KEY}".encode()).hexdigest(),
            "ct": now,
            "ps": DEFAULT_PS,
            "pc": DEFAULT_PC,
        }
        data["s"] = cal_hash(encode_data(data))
        self._last_read_ts = now
        return data

    def read_once(
        self,
        book_id: str,
        chapter_uid: str,
        *,
        last_time: int | None = None,
        rt_override: int | None = None,
    ) -> bool:
        """执行一次 /web/book/read 上报。

        Args:
            book_id: 书籍 ID（来自 LocalDB）
            chapter_uid: 章节 UID（来自 LocalDB）
            last_time: 上次阅读时间戳（用于计算 rt）
            rt_override: 强制指定 rt（覆盖 last_time 计算）

        Returns:
            True 表示成功（succ=1）
        """
        if not self.ensure_session():
            return False
        self._augment_headers_baggage()
        try:
            payload = self._build_payload(
                book_id, chapter_uid, last_time=last_time, rt_override=rt_override
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("read_once payload 构造异常：%s", exc)
            self.warning.emit(f"read_once payload 构造异常：{exc}")
            return False

        json_ct = {"Content-Type": "application/json;charset=UTF-8"}

        def _do_post(pyld: dict) -> tuple[int, dict | None, str]:
            try:
                with self._lock:
                    resp = self._session.post(
                        READ_URL,
                        headers=json_ct,
                        data=json.dumps(pyld, separators=(",", ":")),
                        timeout=15,
                    )
                resp.raise_for_status()
                try:
                    body = resp.json()
                except ValueError:
                    return resp.status_code, None, resp.text[:400]
                return resp.status_code, body if isinstance(body, dict) else {"_raw": body}, ""
            except requests.RequestException as exc:
                log.warning("read 请求失败：%s", exc)
                self.warning.emit(f"read 请求失败：{exc}")
                return 0, None, str(exc)

        status, body, txt = _do_post(payload)

        if isinstance(body, dict) and body.get("succ") == 1 and "synckey" in body:
            log.info(
                "✅ read_once 成功：succ=1 synckey=%s b=%s c=%s rt=%d",
                str(body.get("synckey"))[:16],
                str(payload.get("b"))[:20],
                str(payload.get("c"))[:20],
                int(payload.get("rt", 0) or 0),
            )
            return True
        if isinstance(body, dict) and body.get("succ") == 1:
            # 有 succ 但无 synckey：服务端已接受但同步状态缺失
            log.info("read 成功但无 synckey：body keys=%s", list(body.keys())[:10])
            return True

        # 失败
        log.warning(
            "read 返回失败：HTTP=%s body=%s",
            status, str(body)[:300] if body else txt[:200],
        )
        snippet = str(body)[:80] if body else (txt[:80] or "(空)")
        self.warning.emit(f"read 返回异常：HTTP={status} body={snippet}")
        return False
