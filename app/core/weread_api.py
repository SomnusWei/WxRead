"""微信读书 Web 接口封装：登录态校验、Cookie 续期、阅读时长上报。

移植自 findmover/wxread，并做了如下改进：
1. 线程安全：通过 RLock 避免并发读写 cookies/headers。
2. 状态可观察：使用 Qt 信号向 UI 层上报进度/结果。
3. 默认书单/章节库动态扩展 + 字段范围随机化。
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import secrets
import threading
import time
import urllib.parse
from datetime import date as _Date  # noqa: F401
from typing import Any

import requests
from PySide6.QtCore import QObject, Signal

from .config import ConfigStore
from app.utils.logger import get_logger

log = get_logger(__name__)

READ_URL = "https://weread.qq.com/web/book/read"
RENEW_URL = "https://weread.qq.com/web/login/renewal"
CHAPTER_SYNC_URL = "https://weread.qq.com/web/book/chapterInfos"
SHELF_SYNC_URL = "https://weread.qq.com/web/shelf/sync"  # 轻量 GET 健康检查

# —— 阅读时长/阅读统计（官方返回，用于状态页展示"今日已读多久"）。
#    web 端优先；i.weread.qq.com 是移动端 API 同 cookie 也能访问，做兜底。
READ_TIMES_URL = "https://weread.qq.com/web/read/readtime"           # 旧：?date=YYYYMMDD / ?month=YYYYMM
READ_TIMES_V2_URL = "https://weread.qq.com/web/readtimes"            # 常见：?date=YYYY-MM-DD
READ_READDETAIL_URL = "https://weread.qq.com/web/read/readdetail"    # 明细：?date=YYYY-MM-DD
AGENT_READDATA_URL = "https://i.weread.qq.com/api/agent/readdata/detail"  # mode=daily / weekly / monthly
USER_RANK_INFO_URL = "https://i.weread.qq.com/user/readrankinfo"     # 周榜/月榜阅读时长，会带 totalTime
USER_SUMMARY_URL = "https://i.weread.qq.com/user/readtimeSummary"    # 官方统计页（今日/本周/本月/累计）
AGENT_GATEWAY_URL = "https://i.weread.qq.com/api/agent/gateway"      # 官方 weread-skills 网关（/readdata/detail mode=daily）
BOOKLIST_URL = "https://weread.qq.com/web/shelf/booklist"            # 书架：含 title / bookId / readingProgress / cover

KEY = "3c5c8717f3daf09iop3423zafeqoi"

COOKIE_DATA_VARIANTS: list[dict[str, Any]] = [
    {"rq": "%2Fweb%2Fbook%2Fread", "ql": False},
    {"rq": "%2Fweb%2Fbook%2Fread", "ql": True},
    {"rq": "%2Fweb%2Fbook%2Fread"},
]

# 默认书籍池（多种组合，降低模式识别）
DEFAULT_BOOKS: list[str] = [
    "36d322f07186022636daa5e",
    "6f932ec05dd9eb6f96f14b9",
    "43f3229071984b9343f04a4",
    "d7732ea0813ab7d58g0184b8",
    "3d03298058a9443d052d409",
    "4fc328a0729350754fc56d4",
    "a743220058a92aa746632c0",
    "140329d0716ce81f140468e",
    "1d9321c0718ff5e11d9afe8",
    "ff132750727dc0f6ff1f7b5",
    "e8532a40719c4eb7e851cbe",
    "9b13257072562b5c9b1c8d6",
    "ce032b305a9bc1ce0b0dd2a",  # 三体，参考实现中的默认值
]

# 章节池
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


def encode_data(data: dict[str, Any]) -> str:
    return "&".join(
        f"{k}={urllib.parse.quote(str(data[k]), safe='')}" for k in sorted(data.keys())
    )


def cal_hash(input_string: str) -> str:
    """JS 逆向还原的 s 字段算法（与 findmover/wxread 一致）。"""
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
    """微信读书接口封装。

    对外的主要入口：
      - set_session(headers, cookies)  → 设置登录态
      - check_session() -> bool        → 轻量校验 Cookie 是否仍有效
      - ensure_session() -> bool       → 若失效，尝试续期
      - read_once() -> bool            → 发起一次阅读上报（成功返回 True）
    """

    progress = Signal(int, int, float)  # (current_step, total_steps, minutes_done)
    message = Signal(str)  # 普通日志消息
    warning = Signal(str)
    error = Signal(str)
    cookie_invalid = Signal()  # 登录态失效（无论如何都救不回）
    # 当前书籍更新（bookId 变化 / 标题 / 进度被成功解析时 emit，供 LoginPage → StatusPage → UI 联动）
    # payload = {
    #   "book_id": "36d322f07186022636daa5e",       # 真实 bookId（原始 id）
    #   "book_reader_id": "wb36d322f07186022636daa5e",  # reader URL 上使用的"wb+bookId"或加密形式
    #   "title": "三体Ⅲ：死神永生",
    #   "author": "刘慈欣",
    #   "url": "https://weread.qq.com/web/reader/wb...",
    #   "progress": 0.35,              # 0.0~1.0 的阅读进度（从书架 readingProgress/10000 算）
    #   "progress_text": "35.0%",
    #   "chapter_id": "ecc32f3013eccbc87e4b62e",   # 当前章节 id（能拿到就带）
    #   "total_chapters": 66,
    #   "source": "shelf_booklist / manual / reader_url",  # 解析来源，日志里用
    #   "updated_at": int(time.time()),
    # }
    current_book_changed = Signal(dict)

    # 当日官方阅读时长（秒）：UI 定期拉取就直接发 Signal 给状态页显示；
    reading_summary = Signal(dict)

    def __init__(self, config: ConfigStore | None = None) -> None:
        super().__init__()
        self._cfg = config or ConfigStore()
        self._lock = threading.RLock()
        self._session = requests.Session()
        self._update_from_config()
        # 书籍/章节扩展池（用户可在 config 中自定义；此处合并默认值）
        self.books: list[str] = list(DEFAULT_BOOKS)
        self.chapters: list[str] = list(DEFAULT_CHAPTERS)
        # —— 当前"正在读的书"持久化字段，供状态页/登录页/调度器统一真相
        self._book_lock = threading.RLock()
        self._current_book: dict | None = None
        self._last_read_ts: int = 0
        self._book_progress_override: float | None = None
        # ============================================================
        # 策略一：用户手动设置当前书的锁定机制
        # ------------------------------------------------------------
        # - 用户在状态页点【更新到阅读状态】→ _manual_book_locked = True
        # - 锁定后：任何 set_current_book 调用如果 source 不是"用户主动操作来源"，
        #   就进入 PATCH（只补缺失字段）模式，绝不覆盖已有的
        #   {book_id, book_reader_id, title, url, author} 五大核心字段。
        # - 能解锁的只有：用户再次点【更新】(manual)、点【清除当前书】(clear)、
        #   或点【从书架选最近在读】(shelf_pick_manual) 这三种"明确用户点击"来源。
        # - 目的：解决 Skill shelf_best 每 10 次 + 登录浏览器导航信号两者
        #   互相抢占，造成嵌合书 + URL 被偷偷改走的历史问题（2026-08-21 errorlog 分析）
        # ============================================================
        self._manual_book_locked: bool = False

    # -------- 基础：与 config 同步 --------
    @staticmethod
    def _cookie_domains_for(entry: dict[str, Any]) -> list[str]:
        """根据 cookies_raw 中的单条记录推导 requests 要种到的域列表。

        策略：以登录时浏览器实际给出的 domain 为准；若 domain 为空，
        则降级种到 .qq.com / weread.qq.com 双域（和旧逻辑兼容）。
        requests 的 CookieJar 要求带前导点的 domain 才会匹配子域。
        """
        raw_domain = str(entry.get("domain") or "").strip()
        if not raw_domain:
            return [".qq.com", "weread.qq.com"]
        normalized = raw_domain if raw_domain.startswith(".") else "." + raw_domain
        out: list[str] = [normalized]
        # 显式加上裸域名（无前导点）的副本，防止某些 Strict 版本的 jar 拒收
        try:
            bare = normalized.lstrip(".")
            if bare and bare not in out:
                out.append(bare)
        except Exception:
            pass
        return out

    def _plant_cookies(self, *, cookies_dict: dict[str, str], cookies_raw: list[dict[str, Any]] | None) -> None:
        """按真实属性把 cookies 种进 requests.Session；必须在 self._lock 内调用。"""
        self._session.cookies.clear()
        # 1) 优先使用 cookies_raw（有 domain/path/secure/httpOnly）
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
                    except Exception:
                        try:
                            self._session.cookies.set(name, value, domain=domain, path=path)
                        except Exception:
                            pass
                    seen_pairs.add((name, domain))
        # 2) 兼容 cookies_dict 中的漏网之鱼（没有 raw 记录的就降级双域盲种）
        for k, v in cookies_dict.items():
            v_s = str(v) if v is not None else ""
            if not k or not v_s:
                continue
            # 如果 raw 已经为这个 name 在至少一个域种过了就跳过
            if any(p[0] == k for p in seen_pairs):
                continue
            for domain in (".qq.com", "weread.qq.com"):
                try:
                    self._session.cookies.set(k, v_s, domain=domain, path="/")
                except Exception:
                    pass

    def _update_from_config(self) -> None:
        with self._lock:
            cookies = dict(self._cfg.get("cookies", {}) or {})
            cookies_raw = list(self._cfg.get("cookies_raw", []) or [])
            headers = dict(self._cfg.get("headers", {}) or {})
            # —— 关键：官方 web 端所有 POST 都是 application/json。
            #    不能用 setdefault，旧 config 里可能残留大小写错 / 值错的 content-type，
            #    一旦请求头里是 text/plain 或 application/x-www-form-urlencoded，
            #    即使 data=json.dumps(...) 服务端也会返回 -2007 "json格式错误"
            #    （用户现场多份日志铁证）。
            for bad in ("content-type", "Content-Type", "CONTENT-TYPE"):
                headers.pop(bad, None)
            headers["Content-Type"] = "application/json;charset=UTF-8"
            self._plant_cookies(cookies_dict=cookies, cookies_raw=cookies_raw)
            self._session.headers.clear()  # 彻底清空旧 Session 头，防止历史残留污染
            self._session.headers.update(headers)
        log.info(
            "从 config 装载登录态：dict=%d, raw=%d, jar=%d",
            len(cookies),
            len(cookies_raw),
            len(getattr(self._session, "cookies", [])),
        )

    def _augment_headers_baggage(self) -> None:
        """按「关键技术点.txt」动态合成 Baggage header：
        sentry-public_key（硬编码）+ sentry-trace_id（32 位 hex 随机）+
        _qimei_uuid42 / qimei36（若采集到了就一并放 baggage，和浏览器一致）。

        关键：官方 web 端 baggage 被用作签名噪声源，不完整时某些请求会被风控
        降级成"疑似机器人"，所以我们一定要放。每次调用前跑一次即可（幂等覆写
        Session.headers['baggage']，config 里不动）。
        """
        # 从当前 cookies_dict / cookies_raw 找设备指纹噪声
        cookies_dict = dict(self._cfg.get("cookies", {}) or {})
        qimei_42 = (cookies_dict.get("_qimei_uuid42") or
                    next((str(r.get("value") or "") for r in (self._cfg.get("cookies_raw", []) or [])
                          if str(r.get("name") or "") == "_qimei_uuid42"), ""))
        qimei_36 = (cookies_dict.get("qimei36") or
                    next((str(r.get("value") or "") for r in (self._cfg.get("cookies_raw", []) or [])
                          if str(r.get("name") or "") == "qimei36"), ""))
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
            # 顺便把 sentry-trace 头也补齐（部分反作弊链路会同时看 trace 与 baggage）
            span = secrets.token_hex(8)
            self._session.headers["sentry-trace"] = f"{trace_id}-{span}-1"

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
        # 权威数据源：cookies_raw 先落盘，然后 cookies_dict 作为向后兼容视图
        # —— Content-Type 必须强制覆写（不能 setdefault）：旧 config 里可能残留错值，
        #    导致 chapter_sync/read 返回 -2007 "json格式错误"（用户现场铁证）。
        for bad in ("content-type", "Content-Type", "CONTENT-TYPE"):
            clean_headers.pop(bad, None)
        clean_headers["Content-Type"] = "application/json;charset=UTF-8"
        self._cfg.set("headers", clean_headers)
        self._cfg.set("cookies_raw", clean_raw, auto_save=False)
        self._cfg.set("cookies", clean_cookies, auto_save=True)
        with self._lock:
            self._plant_cookies(cookies_dict=clean_cookies, cookies_raw=clean_raw)
            self._session.headers.update(clean_headers)
        # 用真实采集到的 _qimei_uuid42 / qimei36 合成一次 baggage
        self._augment_headers_baggage()
        log.info(
            "登录态已更新：dict=%d, raw=%d, headers=%d",
            len(clean_cookies),
            len(clean_raw),
            len(clean_headers),
        )

    # -------- Cookie/会话管理 --------
    def check_session(self, timeout: int = 10) -> bool:
        """轻量校验：调用 chapter_sync / shelf_sync，按 JSON 结构化字段判断。

        成功条件（任一即可，且全都强约束，避免"空 PASS"误判）：
          - chapter_sync：HTTP 200 + JSON errCode == 0 或 含 books/chapters 字段
          - shelf_sync：HTTP 200 + JSON errCode == 0 且含 books/shelfBookIds/updated 等真实同步字段
          - read   回退：HTTP 200 + JSON succ == 1
        绝不再次依赖字符串匹配"登录"这种易被翻译/文案/反爬改写字段的弱判定。
        """
        cookies_dict = self._cfg.get("cookies", {}) or {}
        cookies_raw = self._cfg.get("cookies_raw", []) or []
        has_vid = bool(cookies_dict.get("wr_vid"))
        has_skey = bool(cookies_dict.get("wr_skey"))
        if not (has_vid and has_skey):
            log.debug("check_session 前置失败：wr_vid=%s wr_skey=%s raw=%d", has_vid, has_skey, len(cookies_raw))
            return False
        # 每次请求前补 baggage / sentry-trace（用真实 _qimei 当噪声，和浏览器一致）
        self._augment_headers_baggage()
        # —— 所有 POST 都必须显式声明 application/json；否则 requests 会默认
        #    application/x-www-form-urlencoded，服务端报 -2007 "json格式错误"。
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
                    log.warning("check_session chapter_sync 响应不是 JSON：%s", resp.text[:300])
                    data = None
                if isinstance(data, dict):
                    err = data.get("errCode")
                    # —— 识别"真实同步响应"的字段集合（严格按服务端真实返回判断，
                    #    18:20 smoke 现场铁证：chapter_sync 返回 keys=['data']，
                    #    里面 data=[{bookId,soldOut,chapterUpdateTime,updated:[{chapterUid}]}]，
                    #    所以顶层不一定含 books/chapters/updated，必须按 data[0] 结构识别）。
                    #
                    # 命中任一即算成功（强语义，都要求字段真实存在）：
                    #   A) 显式 errCode == 0
                    #   B) 顶层含 books / chapters 字段（旧契约）
                    #   C) 顶层含 data:list 且 list 里第一项带 bookId（章节同步新契约）
                    data_list = data.get("data") if isinstance(data.get("data"), list) else None
                    first_item = data_list[0] if (data_list and len(data_list) > 0) else None
                    first_has_book = isinstance(first_item, dict) and "bookId" in first_item
                    field_ok = (
                        ("books" in data) or
                        ("chapters" in data) or
                        bool(first_has_book)
                    )
                    if (err == 0) or field_ok:
                        log.info(
                            "check_session 命中 chapter_sync：errCode=%s keys=%s first_keys=%s",
                            err, list(data.keys())[:10],
                            list(first_item.keys())[:8] if isinstance(first_item, dict) else None,
                        )
                        return True
                    log.warning("check_session chapter_sync 明确失败：errCode=%s keys=%s body(前300)=%s",
                                err, list(data.keys())[:10], str(data)[:300])
                else:
                    log.warning("check_session chapter_sync 响应 body(前300): %s", resp.text[:300])
            else:
                log.warning("check_session chapter_sync HTTP=%s body=%s", resp.status_code, resp.text[:200])
        except requests.RequestException as exc:
            log.warning("check_session chapter_sync 请求异常：%s", exc)
        # 2) shelf_sync（强语义：必须有真实同步字段，不能空对象）
        try:
            with self._lock:
                resp = self._session.get(SHELF_SYNC_URL, timeout=timeout)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    log.warning("check_session shelf_sync 响应不是 JSON：%s", resp.text[:300])
                    data = None
                if isinstance(data, dict):
                    err = data.get("errCode")
                    # —— 强约束：必须含真实同步字段（books/shelfBookIds…）；
                    #    然后 errCode 要么显式 == 0，要么 缺失(None) 且其它字段也不是错误形态。
                    #    （18:20 smoke 现场铁证：服务端返回 200 + books/synckey 但 **根本不带 errCode 字段**，
                    #    之前写死 `err == 0 and has_valid` 会把"正确响应"误判成 INFO 未通过，
                    #    只能靠第三条路径的 `read 回退` 才确认成功——太慢也不稳。）
                    valid_fields = {"books", "shelfBookIds", "updated", "syncKey", "archiveBooks", "collapsedBookIds"}
                    has_valid = bool(valid_fields & data.keys())
                    # 错误特征：显式错误码 < 0 / 带 errMsg / 带 errLog（-2012 登录超时、-2010 user not found 等）
                    has_error_marker = (
                        isinstance(err, int) and err < 0
                    ) or (
                        "errMsg" in data and bool(data.get("errMsg"))
                    )
                    no_err = (err == 0) or (err is None and not has_error_marker)
                    if has_valid and no_err:
                        log.info(
                            "check_session 命中 shelf_sync：errCode=%s has_valid=%s keys=%s",
                            err, has_valid, list(data.keys())[:10],
                        )
                        return True
                    # 其余全算失败，并用 WARNING 展示
                    log.warning(
                        "check_session shelf_sync 未通过：errCode=%s has_valid=%s has_err=%s keys=%s body(前250)=%s",
                        err, has_valid, has_error_marker, list(data.keys())[:10], str(data)[:250],
                    )
                else:
                    log.warning("check_session shelf_sync 响应 body(前300): %s", resp.text[:300])
            else:
                log.warning("check_session shelf_sync HTTP=%s body=%s", resp.status_code, resp.text[:200])
        except requests.RequestException as exc:
            log.warning("check_session shelf_sync 请求异常：%s", exc)
        # 3) 回退：假 read 上报
        try:
            payload = self._build_payload()
            with self._lock:
                resp = self._session.post(
                    READ_URL,
                    headers=json_ct,
                    data=json.dumps(payload, separators=(",", ":")),
                    timeout=timeout,
                )
            try:
                data = resp.json()
            except ValueError:
                log.warning("check_session 回退 read 非 JSON：HTTP=%s body=%s", resp.status_code, resp.text[:300])
                data = None
            if isinstance(data, dict) and data.get("succ") == 1:
                log.info("check_session 命中 read：succ=1 keys=%s", list(data.keys())[:8])
                return True
            elif isinstance(data, dict):
                log.warning("check_session 回退 read 失败：HTTP=%s body=%s", resp.status_code, str(data)[:250])
        except Exception as exc:  # noqa: BLE001
            log.warning("check_session 回退 read 异常：%s", exc)
        return False

    def ensure_session(self) -> bool:
        """若当前会话失效，尝试通过 renewal 接口刷新 wr_skey。"""
        if self.check_session():
            return True
        log.info("当前会话已失效，开始刷新 wr_skey ...")
        self.message.emit("登录态失效，正在刷新 Cookie ...")
        new_skey = self._renew_wr_skey()
        if not new_skey:
            log.error("Cookie 刷新失败，可能需要用户重新扫码登录")
            self.error.emit("登录态已失效，Cookie 刷新失败，请重新扫码登录")
            self.cookie_invalid.emit()
            return False
        # 更新 cookies（dict + raw 双写）
        cookies_dict = dict(self._cfg.get("cookies", {}) or {})
        cookies_raw = list(self._cfg.get("cookies_raw", []) or [])
        cookies_dict["wr_skey"] = new_skey
        # 同步覆盖 cookies_raw 中所有名为 wr_skey 的记录，保持域/路径不变
        for entry in cookies_raw:
            if str(entry.get("name")) == "wr_skey":
                entry["value"] = new_skey
        # 若 raw 中没有 wr_skey，补一条最低限度的记录（用最常用的域 .weread.qq.com path=/）
        if not any(str(e.get("name")) == "wr_skey" for e in cookies_raw):
            cookies_raw.append(
                {
                    "name": "wr_skey",
                    "value": new_skey,
                    "domain": ".weread.qq.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                    "sameSite": 0,
                }
            )
        self._cfg.set("cookies_raw", cookies_raw, auto_save=False)
        self._cfg.set("cookies", cookies_dict, auto_save=True)
        with self._lock:
            self._plant_cookies(cookies_dict=cookies_dict, cookies_raw=cookies_raw)
        log.info("wr_skey 刷新成功（长度=%d）：%s***", len(new_skey), new_skey[:4])
        self.message.emit(f"Cookie 刷新成功（新 wr_skey len={len(new_skey)}: {new_skey[:4]}***）")
        # 再检查一次
        if self.check_session():
            return True
        # 仍不行，抛失效信号
        self.error.emit("Cookie 刷新后依然无效，请重新扫码登录")
        self.cookie_invalid.emit()
        return False

    # -------- 当前阅读中的书（统一真相源） --------
    def _emit_book_changed_locked(self) -> None:
        if self._current_book is None:
            return
        # book_changed 的 pyqtSignal 是跨线程安全的，但我们在这里也打一条详细日志
        book_id = self._current_book.get("book_id")
        title = self._current_book.get("title") or "(未命名)"
        log.info(
            "current_book 变更：bookId=%s title=%s progress=%s url=%s",
            book_id, title, self._current_book.get("progress_text"),
            (self._current_book.get("url") or "")[:120],
        )
        try:
            self.current_book_changed.emit(dict(self._current_book))
        except Exception as exc:  # noqa: BLE001
            log.warning("current_book_changed emit 失败：%s", exc)

    def current_book(self) -> dict | None:
        with self._book_lock:
            return dict(self._current_book) if self._current_book else None

    # ============================================================
    # 策略一：手动锁对外 API（供 UI / 调度器查询 / 修改）
    # ============================================================
    # 只有这些来源被视为"用户主动点击"：它们可以解锁 / 覆盖核心字段。
    USER_INITIATED_SOURCES = frozenset({
        "manual",            # 状态页点【更新到阅读状态】
        "shelf_pick_manual", # 状态页点【从书架挑选最近在读】（用户点击）
        "clear",             # 状态页点【清除当前书】
    })

    # PATCH 合并时，source 会被一直拼接，导致 UI 里出现 1000+ 字符的超长 source 标签
    # （2026-08-22 用户截图里可见 "login_browser_nav+skill_book_info+..." 挤爆一行）。
    # 这里限制最多保留最近 N 段 + 历史唯一数，避免无限增长 + 控件被挤压。
    SOURCE_COMPACT_MAX_SEGMENTS = 4

    @classmethod
    def compact_source(cls, raw: str) -> str:
        """把 'a+b+c+c+b+d' 风格的来源拼接压缩为「去重 + 只保留最近 N 段」。"""
        if not raw:
            return ""
        s = str(raw)
        parts = [p for p in s.split("+") if p]
        if not parts:
            return ""
        # (1) 按出现顺序全局去重（保留每种来源"最后一次出现"的位置）
        seen: dict[str, None] = {}
        for p in parts:
            seen[p] = None
        uniq = list(seen.keys())
        if len(uniq) <= cls.SOURCE_COMPACT_MAX_SEGMENTS:
            return "+".join(uniq)
        # (2) 来源数过多：只保留尾部 N 段（= 最近的 N 种来源）+ 前置省略号
        tail = uniq[-cls.SOURCE_COMPACT_MAX_SEGMENTS:]
        return "…+" + "+".join(tail)

    def is_book_locked(self) -> bool:
        """当前是否处于「用户手动锁定当前书」状态。锁定时自动源（Skill/导航）只能补字段，不能覆盖。"""
        return bool(self._manual_book_locked)

    def lock_current_book(self, *, locked: bool = True) -> None:
        """显式上锁/解锁。上锁后，非用户来源只能 PATCH 缺失字段。"""
        self._manual_book_locked = bool(locked)
        log.info("📌 手动锁状态变更：locked=%s", self._manual_book_locked)

    def set_current_book(self, payload: dict | None, *, source: str = "manual") -> dict | None:
        """手动 / 从浏览器 URL / 从书架 选择"正在读的书"。返回最终生效的 payload（None 代表清空）。

        策略一（2026-08-22）核心行为：
        1) payload=None 且 source 是"用户清除" → 解锁并清空当前书。
        2) source ∈ USER_INITIATED_SOURCES → 视为「用户明确点击」：
           - 上锁（_manual_book_locked=True）
           - 允许完整覆盖五大核心字段 {book_id, book_reader_id, title, url, author}。
        3) 其他来源（skill_sync / login_browser_nav / shelf_booklist / cookie_shelf 等）且已锁定 →
           进入"PATCH 模式"：核心字段绝对保留旧值，只补 progress/chapter/source/updated_at
           等非核心字段。
        4) 未锁定：沿用原"稀疏导航 vs 完整 payload"的 merge 逻辑（兼容性）。

        至少允许：{book_id, title, url, reader_id, progress, chapter_id, author}
        """
        if payload is None:
            with self._book_lock:
                self._current_book = None
                self._book_progress_override = None
                # —— 策略一：只有"用户明确清除"才解锁；否则（内部清空重试之类的）保留锁
                if source in self.USER_INITIATED_SOURCES:
                    self._manual_book_locked = False
            return None
        if not isinstance(payload, dict):
            return self.current_book()

        # 策略一：用户明确点击 → 立即上锁；后续 Skill/导航再也不能抢占
        if source in self.USER_INITIATED_SOURCES:
            self._manual_book_locked = True

        # —— 规范化字段 ——
        book_id: str = str(payload.get("book_id") or "").strip()
        title = str(payload.get("title") or "").strip()
        reader_id = str(payload.get("book_reader_id") or payload.get("reader_id") or "").strip()
        url = str(payload.get("url") or "").strip()
        chapter_id = str(payload.get("chapter_id") or "").strip()
        author = str(payload.get("author") or "").strip()
        progress_raw = payload.get("progress")  # 0~1 或 0~10000 都接受
        progress: float | None = None
        if isinstance(progress_raw, (int, float)) and not isinstance(progress_raw, bool):
            v = float(progress_raw)
            if 0.0 <= v <= 1.0:
                progress = v
            elif 100.0 < v <= 10000.0:
                progress = v / 10000.0
            elif 1.0 < v <= 100.0:
                progress = v / 100.0
        progress_text = ""
        if progress is None:
            progress_text = ""
        else:
            progress = max(0.0, min(1.0, progress))
            progress_text = f"{progress * 100:.1f}%"

        # 从 URL 解析兜底：weread.qq.com/web/reader/<encoded>?k<chapter>
        if (not book_id or not reader_id or not url) and url:
            parsed = self._parse_reader_url(url)
            if parsed:
                reader_id = reader_id or parsed.get("reader_id") or ""
                book_id = book_id or parsed.get("book_id") or ""
                chapter_id = chapter_id or parsed.get("chapter_id") or ""
                url = url or parsed.get("url") or ""

        # 还没 url 就拼一个：https://weread.qq.com/web/reader/<reader_id_or_wb_book_id>
        if not url and (reader_id or book_id):
            url = "https://weread.qq.com/web/reader/" + (reader_id or ("wb" + book_id))
            if chapter_id:
                url += f"?k{chapter_id}"

        # 书架池合并：如果有真实 bookId，就放到默认池第 0 个，保证调度读这个 book
        if book_id:
            if book_id not in self.books:
                self.books.insert(0, book_id)
            # 只留最近的 20 本避免无限增长
            if len(self.books) > 20:
                self.books = self.books[:20]
        if chapter_id:
            if chapter_id not in self.chapters:
                self.chapters.insert(0, chapter_id)
            if len(self.chapters) > 40:
                self.chapters = self.chapters[:40]

        now = int(time.time())
        final: dict[str, Any] = {
            "book_id": book_id,
            "book_reader_id": reader_id,
            "title": title or (f"未命名书籍（{book_id[:10]}…）" if book_id else "未选择书籍"),
            "author": author,
            "url": url,
            "progress": progress if progress is not None else None,
            "progress_text": progress_text,
            "chapter_id": chapter_id,
            "source": source,
            "updated_at": now,
        }
        with self._book_lock:
            prev = dict(self._current_book) if isinstance(self._current_book, dict) else None

            # ================================================================
            # 策略一 · 强锁 PATCH 模式（优先级最高，覆盖下方所有 merge 逻辑）
            # ----------------------------------------------------------------
            # 条件：已上锁(_manual_book_locked=True)，且当前来源不是"用户主动点击"，
            #       且已有 prev 存在（否则就是初始化，用户还没 Set 过，放行完整覆盖）。
            # 行为：核心字段{book_id, book_reader_id, title, url, author} 100% 保留 prev；
            #       只允许补"非核心字段"（progress / chapter_id / source / updated_at），
            #       且 progress 仅当旧值为空时才补。
            # ================================================================
            if (
                self._manual_book_locked
                and source not in self.USER_INITIATED_SOURCES
                and prev
            ):
                CORE_FIELDS = ("book_id", "book_reader_id", "title", "url", "author")
                merged = dict(prev)
                # —— 非核心字段：仅补缺失 / 更丰富的值 ——
                # progress：旧为空才用新；否则保留旧
                if merged.get("progress") is None and final.get("progress") is not None:
                    merged["progress"] = final["progress"]
                    merged["progress_text"] = final.get("progress_text") or merged.get("progress_text")
                # chapter_id：旧为空才用新（不要把 A 书章节塞到 B 书 payload 里）
                if not merged.get("chapter_id") and final.get("chapter_id"):
                    merged["chapter_id"] = final["chapter_id"]
                # source / updated_at 永远更新（显示最后一次被谁 touch）
                merged["source"] = self.compact_source(f"{str(merged.get('source') or '')}+{source}")
                merged["updated_at"] = now
                # 防呆：核心字段绝不改动（用 prev 值重新覆写一遍，防止上面 merge 意外污染）
                for cf in CORE_FIELDS:
                    if prev.get(cf):
                        merged[cf] = prev[cf]
                # progress override 与 _current_book 同步
                if merged.get("progress") is not None and isinstance(merged["progress"], (int, float)):
                    self._book_progress_override = float(merged["progress"])
                self._current_book = merged
                log.info(
                    "📌 策略一·强锁生效：拒绝 source=%s 覆盖核心字段，仅补非核心（lock=True）",
                    source,
                )
                self._emit_book_changed_locked()
                return dict(merged)

            # —— 合并策略：防止"稀疏的浏览器 URL 导航"覆盖"已经解析好的完整书籍"
            # 例如用户在登录浏览器里切页到另一本书的 /reader/<encid>，此时 payload 只有 url/reader_id，
            # title/progress/book_id 都是空——不应该把"《祈祷落幕时》 progress=37%" 直接替换成"未选择书籍"。
            # 只有在"新 payload 提供了明确信息"时才覆盖老字段。
            if prev:
                p_book_id = str(prev.get("book_id") or "").strip()
                p_title = str(prev.get("title") or "").strip()
                p_is_placeholder = (not p_book_id) and (not p_title or p_title == "未选择书籍")
                new_title = str(final.get("title") or "").strip()
                new_has_meaningful_id = bool(final.get("book_id"))
                new_is_placeholder = (not new_has_meaningful_id) and (
                    not new_title or new_title in {"未选择书籍", "未命名书籍"}
                )
                # 如果新书是占位（只有 url/reader_id 但没解析出 book_id/title），而旧书不是占位：
                # —— 仅当 reader_id 也没变化 或 旧书 reader_id 也未知 时才保留旧；
                # —— 如果 reader_id 真的变了（用户真的切到了另一本书但我们还没 decode 它的 bookId）：
                #    就 merge（保留旧 title/progress/book_id，只更新 URL/reader_id/chapter）。
                if new_is_placeholder and not p_is_placeholder:
                    same_reader = bool(
                        final.get("book_reader_id")
                        and prev.get("book_reader_id")
                        and str(final["book_reader_id"]) == str(prev["book_reader_id"])
                    )
                    if same_reader:
                        # 相同 reader_id 的重复导航通知（Qt 事件循环会多次发），直接丢
                        self._emit_book_changed_locked()  # 仍然发信号让 UI 不锁死
                        return dict(prev) if self._current_book else None
                    # 不同 reader_id：merge（保留 title/book_id/progress/author，只改 url/reader_id/chapter）
                    merged = dict(prev)
                    # 只在"新 URL 明确提供了字段"时用新值覆盖
                    if final.get("book_reader_id"):
                        merged["book_reader_id"] = final["book_reader_id"]
                    if final.get("url"):
                        merged["url"] = final["url"]
                    if final.get("chapter_id"):
                        merged["chapter_id"] = final["chapter_id"]
                    # source/updated_at 用最新的
                    merged["source"] = source
                    merged["updated_at"] = now
                    # 如果新 payload 提供了明确 title 就用，否则保留旧
                    if new_title and new_title not in {"未选择书籍", "未命名书籍"}:
                        merged["title"] = new_title
                    if final.get("author"):
                        merged["author"] = final["author"]
                    if final.get("progress") is not None:
                        merged["progress"] = final["progress"]
                        merged["progress_text"] = final["progress_text"]
                    # 如果旧 book_id 和新的 reader_id 明显不一致（例如旧 book_id 是 842609 但新 URL 是完全
                    # 不同的 enc 串），就清空 book_id，让下一次 Skill 同步从书架/reader_id 重新解析。
                    rid = str(final.get("book_reader_id") or "")
                    bid = str(merged.get("book_id") or "")
                    if rid and bid and len(rid) >= 6 and len(bid) >= 1:
                        # wb/wr 前缀 + hex(bookId) 一致？一致就保留；不一致认为是换了书 → 清空让下次解析
                        prefix_decoded = ""
                        if rid.lower().startswith("wb") or rid.lower().startswith("wr"):
                            prefix_decoded = rid[2:]
                        if prefix_decoded and prefix_decoded.lower() != bid.lower():
                            merged["book_id"] = ""
                            # 不清空 title/progress（仍然显示旧信息，直到 Skill/shelf 用新 reader_id 解析）
                    self._current_book = merged
                    if merged.get("progress") is not None and isinstance(merged["progress"], (int, float)):
                        self._book_progress_override = float(merged["progress"])
                    self._emit_book_changed_locked()
                    return dict(merged)
            # 默认路径：完整覆盖
            self._current_book = final
            if progress is not None:
                self._book_progress_override = progress
            self._emit_book_changed_locked()
        return dict(final)

    def _parse_reader_url(self, url: str) -> dict | None:
        """解析 `https://weread.qq.com/web/reader/<encodedBookId>[?k<chapterEncId>&...]`。

        encodedBookId 的常见规则：wb<hexBookId>、wr<hexBookId>、或其它前缀；
        chapterId 一般是 `?k<chapterId>`。这里做最"宽松"的解析，解析不到就只保留 reader_id。
        """
        import re as _re
        m = _re.match(
            r"^https?://(?:www\.)?weread\.qq\.com/web/reader/([A-Za-z0-9_.-]+)(?:\?([^#]*))?",
            str(url).strip(),
        )
        if not m:
            # 兼容用户手动复制「书本详情页」/reader 后面有章节锚点：全部当 reader_id 直接保留
            return None
        reader_id = m.group(1)
        qs = m.group(2) or ""
        chapter = ""
        for part in qs.split("&"):
            if part.startswith("k") and len(part) > 1:
                chapter = part[1:]
                break
        # book_id 的推断：一般 reader_id 形如 wb<bookHexId>，去掉 wb 就是 bookId
        book_id = ""
        if reader_id.startswith("wb") and len(reader_id) > 2:
            book_id = reader_id[2:]
        elif reader_id.startswith("wr") and len(reader_id) > 2:
            book_id = reader_id[2:]
        else:
            # 其它前缀/直接是加密串：直接当 book_id 不可解析
            book_id = ""
        full = f"https://weread.qq.com/web/reader/{reader_id}"
        if chapter:
            full += f"?k{chapter}"
        return {
            "reader_id": reader_id,
            "book_id": book_id,
            "chapter_id": chapter,
            "url": full,
        }

    def refresh_current_book_from_shelf(self, *, timeout: int = 10, source: str = "shelf_booklist") -> dict | None:
        """调用 shelf/booklist 抓书架 → 取"进度最高且未读完"的书做默认当前书。

        source 参数说明：
          - "shelf_booklist"（默认）：调度器自动刷新，策略一锁定时不允许覆盖核心字段。
          - "shelf_pick_manual"：用户在状态页点【从书架挑选】，视为用户主动操作 → 解锁 + 覆盖。
        """
        try:
            self._augment_headers_baggage()
            with self._lock:
                resp = self._session.get(
                    BOOKLIST_URL,
                    headers={"Referer": "https://weread.qq.com/web/shelf"},
                    timeout=timeout,
                )
            if resp.status_code != 200:
                log.debug("shelf/booklist HTTP=%s", resp.status_code)
                return self.current_book()
            try:
                data = resp.json()
            except ValueError:
                log.debug("shelf/booklist 非 JSON: %s", resp.text[:200])
                return self.current_book()
            books = []
            if isinstance(data, list):
                books = data
            elif isinstance(data, dict):
                for k in ("books", "book", "booklist", "shelfBookIds", "data"):
                    v = data.get(k)
                    if isinstance(v, list):
                        books = v
                        break
            if not books:
                return self.current_book()
            best = None
            best_score = -1.0
            for b in books:
                if not isinstance(b, dict):
                    continue
                bid = str(b.get("bookId") or b.get("book_id") or "").strip()
                if not bid:
                    continue
                rp_raw = b.get("readingProgress")
                try:
                    rp = float(rp_raw) if rp_raw is not None else 0.0
                except (TypeError, ValueError):
                    rp = 0.0
                if rp > 1.0:  # 绝大多数官方返回 0~10000
                    rp = rp / 10000.0
                rp = max(0.0, min(0.9999, rp))
                # 给"已读过 + 还没读完（<98%）"更高分
                finished = bool(b.get("finished") or rp >= 0.999)
                updated = b.get("readUpdateTime") or b.get("updateTime") or 0
                try:
                    updated_i = int(updated) if updated else 0
                except (TypeError, ValueError):
                    updated_i = 0
                score = rp * 1000.0 - (1000 if finished else 0) + min(100, updated_i / 10_000_000)
                if score > best_score:
                    best_score = score
                    best = (b, bid, rp)
            if best is None:
                return self.current_book()
            b, bid, rp = best
            title = str(b.get("title") or b.get("bookTitle") or "").strip()
            author = str(b.get("author") or "").strip()
            reader_id = b.get("readerBookId") or b.get("readerId")
            if not reader_id:
                reader_id = "wb" + bid
            url = f"https://weread.qq.com/web/reader/{reader_id}"
            chapter = str(b.get("chapterUid") or b.get("chapterId") or b.get("chapter") or "").strip()
            if chapter:
                url += f"?k{chapter}"
            return self.set_current_book(
                {
                    "book_id": bid,
                    "book_reader_id": reader_id,
                    "title": title,
                    "author": author,
                    "url": url,
                    "progress": rp,
                    "chapter_id": chapter,
                },
                source=source,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("shelf/booklist 刷新当前书失败：%s", exc)
            return self.current_book()

    def inject_progress_from_cookie_shelf(self, *, timeout: int = 8) -> dict | None:
        """从 Cookie 会话的 /web/shelf/booklist（真正带 readingProgress 的权威接口）提取当前书的进度，
        并补回到 self._current_book 里。返回更新后的当前书快照。"""
        try:
            self._augment_headers_baggage()
            with self._lock:
                resp = self._session.get(
                    BOOKLIST_URL,
                    headers={"Referer": "https://weread.qq.com/web/shelf"},
                    timeout=timeout,
                )
            if resp.status_code != 200:
                return None
            try:
                data = resp.json()
            except ValueError:
                return None
            books: list[dict] = []
            if isinstance(data, list):
                books = [b for b in data if isinstance(b, dict)]
            elif isinstance(data, dict):
                for k in ("books", "book", "booklist", "shelfBookIds", "data"):
                    v = data.get(k)
                    if isinstance(v, list):
                        books = [b for b in v if isinstance(b, dict)]
                        break
            if not books:
                return None
            # —— 匹配逻辑：优先按 book_id，失败再按 reader_id 或 title 模糊匹配 ——
            cb = self.current_book() or {}
            cur_bid = str(cb.get("book_id") or "").strip()
            cur_title = str(cb.get("title") or "").strip()
            cur_rid = str(cb.get("book_reader_id") or "").strip().lower()
            matched: dict | None = None
            # 1) book_id 精确（BOOKLIST 每本书 bookId 都有）
            if cur_bid:
                for b in books:
                    if str(b.get("bookId") or "").strip() == cur_bid:
                        matched = b
                        break
            # 2) title 精确匹配
            if matched is None and cur_title:
                for b in books:
                    if str(b.get("title") or "").strip() == cur_title:
                        matched = b
                        break
            # 3) readerBookId / readerId 精确
            if matched is None and cur_rid:
                for b in books:
                    rid = str(
                        b.get("readerBookId") or b.get("readerId") or ""
                    ).strip().lower()
                    if rid and rid == cur_rid:
                        matched = b
                        break
            if matched is None:
                return None
            # —— 提取进度 + chapterUid + readerBookId（可能 Skill 版没这些）——
            rp_raw = matched.get("readingProgress")
            rp: float | None = None
            try:
                rp = float(rp_raw) if rp_raw is not None else None
            except (TypeError, ValueError):
                rp = None
            if rp is not None:
                if rp > 1.0:
                    rp = rp / 10000.0
                rp = max(0.0, min(1.0, rp))
            reader_id = str(
                matched.get("readerBookId") or matched.get("readerId") or ""
            ).strip()
            chapter_uid = str(
                matched.get("chapterUid") or matched.get("chapterId") or matched.get("chapter") or ""
            ).strip()
            bid = str(matched.get("bookId") or "").strip()
            # —— 只覆盖"缺的"字段，不改已有（特别是 title/url/reader_id 不动，除非为空）——
            patch: dict[str, Any] = {}
            if rp is not None and (not cb or cb.get("progress") is None):
                patch["progress"] = rp
                patch["progress_text"] = f"{rp * 100:.1f}%"
            if reader_id and (not cb or not cb.get("book_reader_id")):
                patch["book_reader_id"] = reader_id
                patch["url"] = f"https://weread.qq.com/web/reader/{reader_id}"
                if chapter_uid:
                    patch["url"] += f"?k{chapter_uid}"
            if chapter_uid and (not cb or not cb.get("chapter_id")):
                patch["chapter_id"] = chapter_uid
            if bid and (not cb or not cb.get("book_id")):
                patch["book_id"] = bid
            if not patch:
                return dict(cb) if cb else None
            merged = dict(cb) if cb else {}
            merged.update(patch)
            merged["source"] = self.compact_source(str(cb.get("source") or "") + "+cookie_shelf_progress")
            return self.set_current_book(merged, source=merged["source"])
        except Exception as exc:  # noqa: BLE001
            log.warning("inject_progress_from_cookie_shelf 失败：%s", exc)
            return None

    def resolve_book_id_for_reader(self, *, reader_id: str | None,
                                    title: str | None, timeout: int = 12) -> dict | None:
        """用户在状态页贴了一个「加密 reader_id」的阅读页 URL（不是 wb/wr<hex> 格式，
        无法直接解码出 bookId），这时就需要：
          ① 用 Cookie 调 /web/shelf/booklist（364 本书都在）
          ② 用 reader_id 精确 / readerBookId / 子串 / title 模糊 三轮 找对应 book
          ③ 找到后：把 bookId/readingProgress/chapterUid/readerBookId 全量回写 current_book
             + 同步调 refresh_chapters_for_book 把这本书的章节灌到章节池（"开始读书"要用）。
        返回更新后的 current_book 快照（或 None 代表没找到）。"""
        try:
            self._augment_headers_baggage()
            with self._lock:
                resp = self._session.get(
                    BOOKLIST_URL,
                    headers={"Referer": "https://weread.qq.com/web/shelf"},
                    timeout=timeout,
                )
            if resp.status_code != 200:
                return None
            try:
                data = resp.json()
            except ValueError:
                return None
            books: list[dict] = []
            if isinstance(data, list):
                books = [b for b in data if isinstance(b, dict)]
            elif isinstance(data, dict):
                for k in ("books", "book", "booklist", "shelfBookIds", "data"):
                    v = data.get(k)
                    if isinstance(v, list):
                        books = [b for b in v if isinstance(b, dict)]
                        break
            if not books:
                return None
            rid = str(reader_id or "").strip()
            rid_low = rid.lower()
            title_str = str(title or "").strip()
            # —— 候选分级：A 精确命中(1 点) > B 子串命中 > C title 模糊 ——
            best_entry: dict | None = None
            best_rank = 0  # 越大越优
            # 预处理 rid 去掉 ? 后参数，兼容输入带 chapter 查询串
            rid_core = rid.split("?", 1)[0]
            rid_core_low = rid_core.lower()
            for b in books:
                if not isinstance(b, dict):
                    continue
                cand_rid = str(
                    b.get("readerBookId") or b.get("readerId") or b.get("encBookId") or ""
                ).strip()
                cand_rid_low = cand_rid.lower()
                bid = str(b.get("bookId") or "").strip()
                cand_title = str(b.get("title") or "").strip()
                # A. reader_id 精确或派生精确
                rank = 0
                if rid_core_low and cand_rid_low:
                    if rid_core_low == cand_rid_low:
                        rank = max(rank, 1000)
                    # reader_id 常被 URL 截断（输入是 reader/af6... → 对比时看前缀）
                    elif len(rid_core_low) >= 8 and cand_rid_low.startswith(rid_core_low):
                        rank = max(rank, 900)
                    elif len(cand_rid_low) >= 8 and rid_core_low.startswith(cand_rid_low):
                        rank = max(rank, 900)
                    elif rid_core_low in cand_rid_low or cand_rid_low in rid_core_low:
                        rank = max(rank, 800)
                # 派生：deepLink 里若带 bookId 也看一眼
                deep = str(b.get("deepLink") or "")
                if bid and rid and (("bookId=" + bid) in deep):
                    # 没大用处，仍然算弱匹配
                    rank = max(rank, 100)
                # B. title 精确
                if title_str and cand_title:
                    if cand_title == title_str:
                        rank = max(rank, 700)
                    elif cand_title.startswith(title_str) or title_str.startswith(cand_title):
                        rank = max(rank, 600)
                    else:
                        # 前 8 字相同也算
                        if cand_title[:8] == title_str[:8] and len(cand_title) >= 8 and len(title_str) >= 8:
                            rank = max(rank, 500)
                if rank > best_rank:
                    best_rank = rank
                    best_entry = b
            if best_entry is None or best_rank < 400:
                return None
            # —— 从 best_entry 组装完整 payload 并 set_current_book ——
            bid_ok = str(best_entry.get("bookId") or "").strip()
            if not bid_ok:
                return None
            title_ok = str(best_entry.get("title") or "").strip()
            author_ok = str(best_entry.get("author") or "").strip()
            ok_reader = str(
                best_entry.get("readerBookId") or best_entry.get("readerId") or ("wb" + bid_ok)
            ).strip()
            rp_raw = best_entry.get("readingProgress")
            rp: float | None = None
            try:
                rp = float(rp_raw) if rp_raw is not None else None
            except (TypeError, ValueError):
                rp = None
            if rp is not None:
                if rp > 1.0:
                    rp = rp / 10000.0
                rp = max(0.0, min(1.0, rp))
            ch = str(
                best_entry.get("chapterUid") or best_entry.get("chapterId") or best_entry.get("chapter") or ""
            ).strip()
            url_ok = f"https://weread.qq.com/web/reader/{ok_reader}"
            if ch:
                url_ok += f"?k{ch}"
            final = self.set_current_book(
                {
                    "book_id": bid_ok,
                    "book_reader_id": ok_reader,
                    "title": title_ok,
                    "author": author_ok,
                    "url": url_ok,
                    "progress": rp,
                    "progress_text": (f"{rp * 100:.1f}%" if rp is not None else ""),
                    "chapter_id": ch,
                },
                source="resolve_book_for_reader",
            )
            # —— 章节灌进去（确保"开始读书"的 random.choice(chapters) 是这本书的章节，不是别的书残留！）——
            try:
                self.refresh_chapters_for_book(bid_ok, timeout=10)
            except Exception as _e:  # noqa: BLE001
                log.warning("resolve_book_id_for_reader 刷新章节失败：%s", _e)
            return final
        except Exception as exc:  # noqa: BLE001
            log.warning("resolve_book_id_for_reader 异常：%s", exc)
            return None

    # -------- 登录态 --------
    def _renew_wr_skey(self) -> str | None:
        self._augment_headers_baggage()
        json_ct = {"Content-Type": "application/json;charset=UTF-8"}
        for idx, payload in enumerate(COOKIE_DATA_VARIANTS):
            try:
                with self._lock:
                    resp = self._session.post(
                        RENEW_URL,
                        headers=json_ct,
                        data=json.dumps(payload, separators=(",", ":")),
                        timeout=10,
                    )
                log.info("renewal 变体 %d：HTTP=%s resp(前200)=%s", idx, resp.status_code, resp.text[:200])
                wr_skey = resp.cookies.get("wr_skey")
                if wr_skey:
                    # 注意：不再做截断！实际 wr_skey 是服务端签发的完整 token，
                    # 截短会导致后续接口把它当成"伪造 cookie"直接 -2010 或被风控。
                    log.info("renewal 变体 %d 命中 resp.cookies.wr_skey len=%d", idx, len(str(wr_skey)))
                    return str(wr_skey)
                # 也可能在 Set-Cookie 头里（多值用逗号分隔，需逐段解析）
                set_cookie = resp.headers.get("Set-Cookie", "")
                if "wr_skey=" in set_cookie:
                    for segment in set_cookie.split(","):
                        for part in segment.split(";"):
                            pair = part.strip()
                            if pair.startswith("wr_skey="):
                                val = pair.split("=", 1)[1].strip()
                                if val:
                                    log.info("renewal 变体 %d 命中 Set-Cookie wr_skey len=%d", idx, len(val))
                                    return val
            except requests.RequestException as exc:
                log.warning("renew 变体 %d 请求失败：%s", idx, exc)
        return None

    def _fix_synckey(self) -> None:
        self._augment_headers_baggage()
        json_ct = {"Content-Type": "application/json;charset=UTF-8"}
        try:
            with self._lock:
                resp = self._session.post(
                    CHAPTER_SYNC_URL,
                    headers=json_ct,
                    data=json.dumps({"bookIds": ["3300060341"]}, separators=(",", ":")),
                    timeout=10,
                )
            log.info("执行了 chapterInfos 同步修复：HTTP=%s body=%s", resp.status_code, resp.text[:200])
        except requests.RequestException as exc:
            log.warning("synckey 修复请求失败：%s", exc)

    # -------- payload 构造 --------
    def _build_payload(self, *, last_time: int | None = None) -> dict[str, Any]:
        """按官方字段格式构造单次阅读上报 payload。

        优先级：
        1. 用户指定的"当前书/当前章节"（self._current_book）
        2. 默认池随机
        """
        with self._book_lock:
            cb = self._current_book
            cb_book = cb.get("book_id") if cb else None
            cb_chapter = cb.get("chapter_id") if cb else None
        cfg_book = (
            str(cb_book).strip() if isinstance(cb_book, str) and cb_book.strip() else None
        )
        cfg_chapter = (
            str(cb_chapter).strip() if isinstance(cb_chapter, str) and cb_chapter.strip() else None
        )
        if not cfg_book:
            cfg_book = random.choice(self.books)
        if not cfg_chapter:
            cfg_chapter = random.choice(self.chapters)
        now = int(time.time())
        ts = int(now * 1000) + random.randint(0, 999)
        rn = random.randint(0, 9999)
        rt = 30 if last_time is None else max(20, min(90, now - last_time))
        data: dict[str, Any] = {
            "appId": "wb182564874603h266381671",
            "b": cfg_book,
            "c": cfg_chapter,
            "ci": random.randint(1, 200),
            "co": random.randint(100, 800),
            "sm": random.choice(DEFAULT_SM_SNIPPETS),
            "pr": random.randint(1, 200),
            "rt": rt,
            "ts": ts,
            "rn": rn,
            "sg": hashlib.sha256(f"{ts}{rn}{KEY}".encode()).hexdigest(),
            "ct": now,
            "ps": cfg_chapter,
            "pc": cfg_chapter,
        }
        data["s"] = cal_hash(encode_data(data))
        # —— 每次成功 payload 构造后，顺便把"当前阅读上报时间"更新，UI 可以显示"最近一次上报：x 分钟前"
        self._last_read_ts = int(now)
        return data

    # -------- 对外：单次阅读上报 --------
    def read_once(self, *, last_time: int | None = None) -> bool:
        """执行一次 /web/book/read。返回 True 代表成功（succ=1）。"""
        if not self.ensure_session():
            return False
        self._augment_headers_baggage()
        payload = self._build_payload(last_time=last_time)
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
        # 失败 & 诊断 payload：仅当 body 为空或无 succ=1 时
        if status == 200 and isinstance(body, dict) and len(body) == 0:
            # —— HTTP=200 body={}：说明服务器"接受了但拒绝记入"，典型原因：
            #   (a) chapter 不是这本书的有效 chapterUid；(b) bookId 不合法；
            #   (c) payload.s / sg 签名被风控。尝试先强制刷新章节列表，用第一 chapter 再试一次。
            log.warning(
                "read 返回空 body：status=%s payload(b=%s c=%s source=%s url=%s)",
                status,
                str(payload.get("b"))[:40],
                str(payload.get("c"))[:40],
                str((self._current_book or {}).get("source") or ""),
                str((self._current_book or {}).get("url") or "")[:200],
            )
            self.warning.emit("⚠️ read 返回空，正在修复章节列表并重试...")
            retry_payload = dict(payload)
            fixed = False
            cb = self.current_book() or {}
            bid = str(cb.get("book_id") or "").strip() or str(payload.get("b") or "").strip()
            if bid:
                try:
                    refreshed = self.refresh_chapters_for_book(bid, timeout=10)
                except Exception as _e_rf:  # noqa: BLE001
                    refreshed = None
                    log.warning("refresh_chapters_for_book 异常：%s", _e_rf)
                if refreshed and len(self.chapters) > 0:
                    retry_payload["c"] = str(self.chapters[0])
                    retry_payload["ps"] = retry_payload["c"]
                    retry_payload["pc"] = retry_payload["c"]
                    # 重新生成签名
                    now_i = int(time.time())
                    retry_payload["ts"] = int(now_i * 1000) + random.randint(0, 999)
                    retry_payload["rn"] = random.randint(0, 9999)
                    retry_payload["ct"] = now_i
                    retry_payload["sg"] = hashlib.sha256(
                        f"{retry_payload['ts']}{retry_payload['rn']}{KEY}".encode()
                    ).hexdigest()
                    retry_payload["s"] = cal_hash(encode_data(retry_payload))
                    fixed = True
            if fixed:
                status2, body2, _txt2 = _do_post(retry_payload)
                if isinstance(body2, dict) and body2:
                    status, body, txt = status2, body2, _txt2

        if isinstance(body, dict) and body.get("succ") == 1 and "synckey" in body:
            log.info("read 成功：succ=1 synckey=%s...", str(body.get("synckey"))[:16])
            return True
        if isinstance(body, dict) and body.get("succ") == 1:
            # 有 succ 但无 synckey：尝试修复
            log.info("read 成功但无 synckey，执行 chapterInfos 修复：body keys=%s", list(body.keys())[:10])
            self.warning.emit("阅读成功但无 synckey，正在修复同步状态...")
            self._fix_synckey()
            return True
        # 失败：常见是登录态过期 / -2007 json 格式错误
        log.warning("read 返回失败：HTTP=%s body=%s", status, str(body)[:300] if body else txt[:200])
        snippet_body = str(body)[:80] if body else (txt[:80] or "(空)")
        self.warning.emit(f"read 返回异常：HTTP={status} body={snippet_body}")
        # 尝试刷新一次
        if self.ensure_session():
            return False  # 让上层重试
        return False

    def refresh_chapters_for_book(self, book_id: str, *, timeout: int = 12) -> bool:
        """根据 bookId 调用 /web/book/chapterInfos，把这本书的所有章节 uid 灌到 self.chapters 里。
        返回 True 代表刷新成功（至少拿到了 1 个章节）。"""
        CHAPTER_INFOS_URL = "https://weread.qq.com/web/book/chapterInfos"
        try:
            self._augment_headers_baggage()
            json_ct = {"Content-Type": "application/json;charset=UTF-8"}
            with self._lock:
                resp = self._session.post(
                    CHAPTER_INFOS_URL,
                    headers=json_ct,
                    data=json.dumps(
                        {"bookIds": [str(book_id)]},
                        separators=(",", ":"),
                    ),
                    timeout=timeout,
                )
            if resp.status_code != 200:
                return False
            try:
                data = resp.json()
            except ValueError:
                return False
            collected: list[str] = []
            # 已知返回：{data:[{bookId, chapters:[{chapterUid,...}, ...]}]}
            if isinstance(data, dict):
                arr = data.get("data")
                if not isinstance(arr, list):
                    arr = [data] if data else []
                for entry in arr:
                    if not isinstance(entry, dict):
                        continue
                    ch_list = entry.get("chapters")
                    if isinstance(ch_list, dict):
                        # 偶尔 chapters 是 {"0": {...}, "1": {...}}
                        ch_list = list(ch_list.values())
                    if not isinstance(ch_list, list):
                        continue
                    for ch in ch_list:
                        if not isinstance(ch, dict):
                            continue
                        uid = str(
                            ch.get("chapterUid") or ch.get("uid") or ch.get("id") or ch.get("chapterId") or ""
                        ).strip()
                        if uid:
                            collected.append(uid)
            if not collected:
                return False
            # —— 去重，插到章节池最前面，最多保留 80 条 ——
            with self._book_lock:
                for uid in reversed(collected):
                    if uid in self.chapters:
                        self.chapters.remove(uid)
                    self.chapters.insert(0, uid)
                if len(self.chapters) > 80:
                    self.chapters = self.chapters[:80]
            log.info("refresh_chapters_for_book 成功：book=%s 新增章节=%d", str(book_id)[:20], len(collected))
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("refresh_chapters_for_book 异常：%s", exc)
            return False

    # -------- 官方阅读统计（每日累计时长） --------
    @staticmethod
    def _format_hm(total_seconds: int | None) -> str:
        if total_seconds is None:
            return "-"
        sec = max(0, int(total_seconds))
        h, rem = divmod(sec, 3600)
        m, _ = divmod(rem, 60)
        if h <= 0:
            return f"{m}m"
        return f"{h}h{m:02d}m"

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        if value is None or value is True or value is False:
            return None
        try:
            if isinstance(value, float):
                # 容忍毫秒
                v = int(value)
            else:
                s = str(value).strip()
                if not s:
                    return None
                # 兼容科学计数与小数
                if "." in s or "e" in s.lower():
                    v = int(float(s))
                else:
                    v = int(s)
            return v if v >= 0 else None
        except (TypeError, ValueError):
            return None

    def _sum_seconds(self, node: Any) -> int | None:
        """在字典/列表里尝试把 readingTime / time / readingSeconds / totalTime / duration 等字段汇总。"""
        total: int | None = None
        stack = [node]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                for k, v in cur.items():
                    kl = str(k).lower()
                    if kl in {
                        "readingtime", "time", "readingseconds", "totaltime",
                        "duration", "reading_time", "todaytime", "todayseconds",
                        "seconds", "readtime", "read_time",
                    }:
                        iv = self._coerce_int(v)
                        if iv is not None:
                            total = (total or 0) + (iv if iv < 10_000_000 else iv // 1000)
                    else:
                        stack.append(v)
            elif isinstance(cur, list):
                stack.extend(cur)
        return total

    def _first_int(self, data: dict, candidates: list[str]) -> int | None:
        for c in candidates:
            if c in data:
                iv = self._coerce_int(data.get(c))
                if iv is not None:
                    return iv if iv < 10_000_000 else iv // 1000
        return None

    def fetch_daily_reading_summary(self, *, timeout: int = 12) -> dict:
        """多路尝试抓取"今日已阅读时长"等官方数据；永远返回 dict（失败带 error 字段）。

        返回结构约定见 reading_summary Signal 注释；成功也会同步 emit reading_summary。
        """
        cookies = self._cfg.get("cookies", {}) or {}
        if not (cookies.get("wr_vid") and cookies.get("wr_skey")):
            result = {
                "today_seconds": None, "today_hm": "-",
                "source": None, "fetched_at": int(time.time()),
                "raw_keys": [],
                "error": "no_cookies",
            }
            self.reading_summary.emit(result)
            return result

        self._augment_headers_baggage()

        today = _Date.today()
        date_compact = today.strftime("%Y%m%d")
        date_hyphen = today.strftime("%Y-%m-%d")
        month_compact = today.strftime("%Y%m")

        # 所有请求统一带 Referer + 当前 UA（官方 web 会校验，缺 referer 很多接口直接返回 {} 空 JSON）
        ua = self._session.headers.get("User-Agent") or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/127.0 Safari/537.36"
        )
        def _merge_base(extra_headers: dict | None = None, method: str = "GET") -> dict:
            hd = {"User-Agent": ua, "Referer": "https://weread.qq.com/"}
            if extra_headers:
                hd.update(extra_headers)
            if method == "POST":
                hd["Content-Type"] = "application/json;charset=UTF-8"
            return {"headers": hd, "timeout": timeout}

        attempts: list[tuple[str, str, str, dict]] = [
            # —— 先打移动端 Agent 网关：这是"官方 weread 统计页"真实 API（成功率最高）
            (
                "agent_readdata_daily",
                "GET",
                AGENT_READDATA_URL,
                dict(params={"mode": "daily", "date": date_hyphen}, **_merge_base({"Referer": "https://weread.qq.com/web/reading/index"})),
            ),
            (
                "agent_readdata_weekly",
                "GET",
                AGENT_READDATA_URL,
                dict(params={"mode": "weekly"}, **_merge_base({"Referer": "https://weread.qq.com/web/reading/index"})),
            ),
            (
                "agent_readdata_monthly",
                "GET",
                AGENT_READDATA_URL,
                dict(params={"mode": "monthly"}, **_merge_base({"Referer": "https://weread.qq.com/web/reading/index"})),
            ),
            # —— web 端明细 + 统计
            ("readdetail", "GET", READ_READDETAIL_URL,
             dict(params={"date": date_hyphen}, **_merge_base({"Referer": "https://weread.qq.com/web/reading/index"}))),
            ("readtime",       "GET", READ_TIMES_URL,
             dict(params={"date": date_compact}, **_merge_base({"Referer": "https://weread.qq.com/web/reading/index"}))),
            ("readtime_month", "GET", READ_TIMES_URL,
             dict(params={"month": month_compact}, **_merge_base({"Referer": "https://weread.qq.com/web/reading/index"}))),
            ("readtimes_v2",   "GET", READ_TIMES_V2_URL,
             dict(params={"date": date_hyphen}, **_merge_base({"Referer": "https://weread.qq.com/web/reading/index"}))),
            # —— 移动端读榜 / 统计页兜底
            ("readrankinfo",   "GET", USER_RANK_INFO_URL, dict(**_merge_base())),
            ("readtimeSummary","GET", USER_SUMMARY_URL,   dict(**_merge_base())),
        ]
        best: dict | None = None
        collected_week: int | None = None
        collected_month: int | None = None
        collected_total: int | None = None

        def _score(payload: dict) -> int:
            # 分数越高越好：today_seconds 存在 100，同时有周/月/总 +20 each
            s = 0
            if isinstance(payload.get("today_seconds"), int):
                s += 100
            if isinstance(payload.get("week_seconds"), int):
                s += 20
            if isinstance(payload.get("month_seconds"), int):
                s += 20
            if isinstance(payload.get("total_seconds"), int):
                s += 20
            return s

        for name, method, url, kwargs in attempts:
            try:
                with self._lock:
                    if method == "POST":
                        resp = self._session.post(url, **kwargs)
                    else:
                        resp = self._session.get(url, **kwargs)
                if resp.status_code != 200:
                    log.debug("reading_summary %s HTTP=%s", name, resp.status_code)
                    continue
                try:
                    data = resp.json()
                except ValueError:
                    log.debug("reading_summary %s 非 JSON: %s", name, resp.text[:200])
                    continue
                if not isinstance(data, dict):
                    continue
                # 一些"统一错误响应"不参与统计
                err = data.get("errCode") or data.get("errcode") or data.get("code")
                err_msg = data.get("errMsg") or data.get("errmsg")
                if (isinstance(err, int) and err < 0) or err_msg:
                    log.debug("reading_summary %s 错误 err=%s msg=%s", name, err, err_msg)
                    continue

                keys = list(data.keys())[:20]
                log.info("reading_summary %s HTTP=200 keys=%s body(前300)=%s", name, keys, str(data)[:300])

                # —— 根据来源按已知字段取"今日秒数"，其余来源就做"深汇总"兜底
                today_sec: int | None = None
                if name in {"agent_readdata_daily", "agent_readdata_weekly", "agent_readdata_monthly"}:
                    # 响应通常是 {data:{...}} 或直接 {...}，常见 todayTotalSeconds / readingSeconds / minutes 之类
                    for anchor in (data, data.get("data"), data.get("result"),
                                   data.get("payload"), data.get("detail"), data.get("summary")):
                        if isinstance(anchor, dict) and today_sec is None:
                            today_sec = self._first_int(
                                anchor,
                                ["todayTotalSeconds", "todaySeconds", "totalSeconds",
                                 "readingSeconds", "todayReadingSeconds", "todayTime",
                                 "readingMinutes", "totalMinutes", "todayReadingMinutes",
                                 "seconds", "time", "readingTime", "totalTime"],
                            )
                            if today_sec is not None:
                                # minutes 类字段要乘 60
                                keys_hit = [
                                    k for k in
                                    ["readingMinutes", "totalMinutes", "todayReadingMinutes"]
                                    if k in anchor
                                ]
                                if keys_hit:
                                    raw = self._first_int(anchor, keys_hit)
                                    if raw is not None:
                                        today_sec = raw * 60
                    # week/month/total 同步拿（weekly 源直接给"本周"值）
                    if name == "agent_readdata_weekly" and today_sec is not None:
                        collected_week = collected_week or today_sec
                    if name == "agent_readdata_monthly" and today_sec is not None:
                        collected_month = collected_month or today_sec
                    for k, target in (("week", "w"), ("month", "m"), ("total", "t"), ("all", "t"),
                                      ("yearTotalSeconds", "total"), ("totalSeconds", "t")):
                        if isinstance(data.get(k), dict):
                            sec = self._first_int(data[k], ["seconds", "time", "readingTime", "totalTime", "totalSeconds", "readingSeconds"])
                            if sec is not None:
                                if k in {"week"}:
                                    collected_week = collected_week or sec
                                elif k in {"month"}:
                                    collected_month = collected_month or sec
                                else:
                                    collected_total = collected_total or sec
                elif name == "readdetail":
                    # readdetail 常返回 list：[{"start":ts, "during":324}] 或 {data:[{during/readingTime/seconds}]}
                    candidate_nodes: list[Any] = []
                    if isinstance(data, list):
                        candidate_nodes.extend(data)
                    else:
                        for key in ("data", "items", "list", "records", "details", "logs"):
                            if isinstance(data.get(key), list):
                                candidate_nodes.extend(data[key])
                    if candidate_nodes:
                        total_during = 0
                        for node in candidate_nodes:
                            if not isinstance(node, dict):
                                continue
                            d = self._first_int(
                                node,
                                ["during", "duration", "time", "seconds", "readingTime",
                                 "readingSeconds", "readSeconds", "totalTime"],
                            )
                            if d is not None:
                                total_during += d
                        if total_during > 0:
                            today_sec = total_during
                    if today_sec is None:
                        today_sec = self._sum_seconds(data)
                elif name == "readtime":
                    # 常见：{"readingTime":7321,...} 或 {date_str: {"time":...}}
                    today_sec = self._first_int(data, ["readingTime", "time", "readingSeconds"])
                    if today_sec is None and date_compact in data and isinstance(data.get(date_compact), dict):
                        today_sec = self._first_int(data[date_compact], ["time", "readingTime", "seconds"])
                elif name == "readtime_month":
                    # {YYYYMMDD: {readingTime/time}, ...}
                    if date_compact in data and isinstance(data.get(date_compact), dict):
                        today_sec = self._first_int(data[date_compact], ["readingTime", "time", "seconds"])
                elif name == "readtimes_v2":
                    # 各种可能：顶层 / data 层 / today 层
                    for anchor in (data, data.get("data"), data.get("today"), data.get("summary")):
                        if isinstance(anchor, dict) and today_sec is None:
                            today_sec = self._first_int(
                                anchor,
                                ["readingTime", "time", "readingSeconds", "todayTime",
                                 "todaySeconds", "seconds", "todayReadingSeconds",
                                 "totalTime", "readSeconds"],
                            )
                    if today_sec is None:
                        today_sec = self._sum_seconds(data)
                elif name == "readrankinfo":
                    # 周/月榜：{"totalTime": 123456, "week":{"time":...}}
                    today_sec = self._first_int(data, ["todayTime", "todaySeconds", "todayReadingSeconds"])
                    # 周累计 & 月累计
                    if isinstance(data.get("week"), dict):
                        collected_week = collected_week or self._first_int(
                            data["week"], ["time", "readingTime", "totalTime", "seconds"]
                        )
                    if isinstance(data.get("month"), dict):
                        collected_month = collected_month or self._first_int(
                            data["month"], ["time", "readingTime", "totalTime", "seconds"]
                        )
                    if isinstance(data.get("total"), dict):
                        collected_total = collected_total or self._first_int(
                            data["total"], ["time", "readingTime", "totalTime", "seconds"]
                        )
                elif name == "readtimeSummary":
                    today_sec = self._first_int(
                        data,
                        ["todayTime", "todaySeconds", "todayReadingSeconds",
                         "today", "today_reading_seconds"],
                    )
                    if today_sec is None and isinstance(data.get("today"), dict):
                        today_sec = self._first_int(
                            data["today"],
                            ["time", "readingTime", "readingSeconds", "seconds", "totalTime"],
                        )
                    # 汇总字段
                    for k, target in (("week", "w"), ("month", "m"), ("total", "t")):
                        if isinstance(data.get(k), dict):
                            sec = self._first_int(data[k], ["time", "readingTime", "seconds", "totalTime"])
                            if sec is not None:
                                if k == "week":
                                    collected_week = collected_week or sec
                                elif k == "month":
                                    collected_month = collected_month or sec
                                elif k == "total":
                                    collected_total = collected_total or sec
                # 通用深汇总兜底：today_sec 仍没有就扫全树
                if today_sec is None:
                    today_sec = self._sum_seconds(data)
                # 过滤明显异常（单天 > 24h 但 < 1000 年这种，单位可能是毫秒再除一次）
                if isinstance(today_sec, int) and today_sec > 24 * 3600 * 10:
                    today_sec = today_sec // 1000 if today_sec % 1000 == 0 else today_sec

                candidate = {
                    "today_seconds": today_sec,
                    "today_hm": self._format_hm(today_sec),
                    "source": name,
                    "fetched_at": int(time.time()),
                    "raw_keys": list(data.keys())[:16],
                    "week_seconds": collected_week,
                    "month_seconds": collected_month,
                    "total_seconds": collected_total,
                }
                if best is None or _score(candidate) > _score(best):
                    best = candidate
            except requests.RequestException as exc:
                log.debug("reading_summary %s 请求异常：%s", name, exc)
                continue
            except Exception as exc:  # noqa: BLE001
                log.warning("reading_summary %s 解析异常：%s", name, exc)
                continue

        if best is None:
            # 终极兜底：直接用书架 booklist 的"本用户今日 readingSeconds / totalReadingSeconds"字段
            # 如果官方真的没有任何 readtime 接口返回（少数"空账号"），至少给用户一个"我们知道你书架上进度最高的书读了多少"的下限估算
            fb_sec = 0
            fb_week_sec = 0
            total_books = 0   # ← 必须前置初始化：否则任何导致"跳过 len(books_list)"的分支都会 UnboundLocalError
            books_list: list[Any] = []
            try:
                with self._lock:
                    resp = self._session.get(BOOKLIST_URL, headers={
                        "Referer": "https://weread.qq.com/web/shelf",
                        "User-Agent": ua,
                    }, timeout=timeout)
                if resp.status_code == 200:
                    payload = resp.json() if resp.content else {}
                    if isinstance(payload, list):
                        books_list = list(payload)
                    elif isinstance(payload, dict):
                        for k in ("books", "booklist", "shelfBookIds", "data"):
                            v = payload.get(k)
                            if isinstance(v, list):
                                books_list = list(v)
                                break
                    total_books = len(books_list)
                    for b in books_list:
                        if not isinstance(b, dict):
                            continue
                        for fld in ("readingSeconds", "todaySeconds", "todayReadingSeconds",
                                    "totalSeconds", "readingTime"):
                            iv = self._coerce_int(b.get(fld))
                            if iv is not None:
                                fb_sec += iv if iv < 10_000_000 else iv // 1000
                    # 顶层/合计字段
                    if isinstance(payload, dict):
                        for k in ("todaySeconds", "todayReadingSeconds", "readingSeconds",
                                  "weekSeconds", "weekReadingSeconds", "totalReadingSeconds"):
                            iv = self._coerce_int(payload.get(k))
                            if iv is None:
                                continue
                            sec = iv if iv < 10_000_000 else iv // 1000
                            if "week" in k.lower():
                                fb_week_sec += sec
                            else:
                                fb_sec += sec
            except Exception as exc:  # noqa: BLE001
                log.debug("reading_summary fallback booklist fallback failed：%s", exc)
            if fb_sec > 0 or best is None:
                best = {
                    "today_seconds": fb_sec if fb_sec > 0 else None,
                    "today_hm": self._format_hm(fb_sec if fb_sec > 0 else None),
                    "source": "shelf_booklist_fallback",
                    "fetched_at": int(time.time()),
                    "raw_keys": ["books(n=%d)" % int(total_books or 0)],
                    "week_seconds": fb_week_sec if fb_week_sec > 0 else None,
                    "month_seconds": None,
                    "total_seconds": None,
                }

        if best is None or (not isinstance(best.get("today_seconds"), int)):
            result = {
                "today_seconds": best.get("today_seconds") if best else None,
                "today_hm": (best or {}).get("today_hm") or "-",
                "source": (best or {}).get("source"),
                "fetched_at": int(time.time()),
                "raw_keys": (best or {}).get("raw_keys") or [],
                "error": "all_failed",
            }
            self.reading_summary.emit(result)
            return result
        result = dict(best)
        result["week_hm"] = self._format_hm(result.get("week_seconds"))
        result["month_hm"] = self._format_hm(result.get("month_seconds"))
        result["total_hm"] = self._format_hm(result.get("total_seconds"))
        self.reading_summary.emit(result)
        log.info(
            "reading_summary 命中：source=%s today=%s week=%s month=%s total=%s keys=%s",
            result.get("source"), result.get("today_hm"),
            result.get("week_hm"), result.get("month_hm"),
            result.get("total_hm"), result.get("raw_keys"),
        )
        return result
