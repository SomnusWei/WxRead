"""微信读书官方 Skill（weread-skills）网关封装。

文档：项目根目录 "用微信阅读skills获取阅读时长数据.txt"

关键约定（摘自文档）：
    统一网关：POST https://i.weread.qq.com/api/agent/gateway
    鉴权：    Authorization: Bearer {API Key}  （API Key 形如 wrk-xxxxxxxx，需先去
              https://weread.qq.com/r/weread-skills 扫码获取）
    请求体：  {
                "api_name": "/readdata/detail",
                "skill_version": "1.0.3",
                ...业务参数放顶层，不要嵌 params
              }
    目前 Skill 是"只读"：可以拉阅读统计/书架/书籍详情/搜索/笔记/书评，
    不能做自动加书架/同步笔记等写入操作。
"""
from __future__ import annotations

import json
import time
from typing import Any

import requests
from PySide6.QtCore import QObject, Signal

from .config import ConfigStore
from app.utils.logger import get_logger

log = get_logger(__name__)

GATEWAY_URL = "https://i.weread.qq.com/api/agent/gateway"
SKILL_VERSION = "1.0.3"
DEFAULT_TIMEOUT = 15


class WeReadSkills(QObject):
    """封装 weread-skills 网关调用：鉴权、列表、阅读统计、书架、书籍详情、搜索。

    当前对外暴露两类能力：
      * 拉取阅读统计 & 当前书进度（返回后 emit 给 StatusPage + WeReadApi.current_book）
      * 提供 10 个 api_name 中实现最常用的 5 个（/_list /readdata/detail /shelf/sync /book/info /store/search），
        其它按需再加。
    """

    # 一次"成功阅读 N 次"阈值触发的全量同步结果，payload 见 fetch_all() 返回
    sync_completed = Signal(dict)
    # 纯文本日志，供 UI 日志窗口追加
    message = Signal(str)
    warning = Signal(str)
    error = Signal(str)

    def __init__(self, config: ConfigStore | None = None) -> None:
        super().__init__()
        self._cfg = config or ConfigStore()
        self._session = requests.Session()
        self._last_sync_ts: int = 0
        self._last_sync: dict | None = None

    # ---------------- 公共属性 ----------------
    @property
    def enabled(self) -> bool:
        return bool(self.api_key())

    def api_key(self) -> str:
        return str((self._cfg.get("weread_skills", {}) or {}).get("api_key") or "").strip()

    def refresh_every_n_reads(self) -> int:
        v = int((self._cfg.get("weread_skills", {}) or {}).get("refresh_every_n_reads") or 10)
        return max(1, min(1000, v))

    def last_sync(self) -> dict | None:
        return dict(self._last_sync) if self._last_sync else None

    # ---------------- 通用网关 ----------------
    @staticmethod
    def effective_timeout(*, user_timeout: int, skill_cfg: dict | None = None) -> tuple[int, int]:
        """返回 (connect_timeout, read_timeout) 分离式超时，防止 requests 在 DNS/connect 阶段卡住。

        Windows + requests 2.x 下，单一 float timeout 对 connect/DNS 阶段的硬约束较弱；
        用 (conn, read) 二元组能更可靠的在 5s 内结束 connect 阶段。
        """
        base = 15
        try:
            base = int(user_timeout or DEFAULT_TIMEOUT)
        except Exception:  # noqa: BLE001
            base = 15
        try:
            override = (skill_cfg or {}).get("post_timeout_sec")
            if isinstance(override, int) and 3 <= override <= 120:
                base = int(override)
        except Exception:  # noqa: BLE001
            pass
        total = max(3, min(20, base))  # 单次请求最多 20s 总上限
        # connect 固定 5s（DNS + TCP + TLS handshake），剩下给 read
        connect_sec = 5 if total > 6 else max(2, total - 1)
        read_sec = max(1, total - connect_sec)
        return (connect_sec, read_sec)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key()}",
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
            ),
            "Origin": "https://weread.qq.com",
            "Referer": "https://weread.qq.com/",
        }

    def call(self, api_name: str, params: dict | None = None, *, timeout: int = DEFAULT_TIMEOUT) -> dict:
        """调一次 weread-skills 网关。请求前后都会发 message 信号（日志窗口可见）。

        返回：
            总是 dict；结构：
            {
              "ok": bool,            # 网关 HTTP=200 且 无顶层 err
              "http": int,           # 响应 HTTP 状态码
              "api_name": str,
              "raw": dict | list,    # 完整 JSON（顶层一般含 data / errCode / skill_version）
              "data": Any,           # 常用快捷：data 节点；找不到等于 raw
              "error": str | None,   # 失败原因（code 级别）
              "errCode": int | None,
              "errMsg": str | None,
            }
        """
        key = self.api_key()
        if not key:
            self.warning.emit(f"[Skill] ← {api_name}：未配置 wrk-* API Key，请先到「设置 → 微信读书官方 Skill」粘贴。")
            return {
                "ok": False, "http": 0, "api_name": api_name,
                "raw": {}, "data": {},
                "error": "no_api_key", "errCode": None, "errMsg": None,
            }
        payload: dict[str, Any] = {"api_name": api_name, "skill_version": SKILL_VERSION}
        if isinstance(params, dict):
            for k, v in params.items():
                # 文档：业务参数必须与 api_name 同级，不可嵌套在 params 对象中
                if k not in payload:
                    payload[k] = v
        import time as _t
        t0 = _t.time()
        self.message.emit(f"[Skill] → 请求 {api_name} 开始（timeout=(connect,read)）")
        try:
            to = self.effective_timeout(
                user_timeout=timeout,
                skill_cfg=(self._cfg.get("weread_skills", {}) if self._cfg else None),
            )
            resp = self._session.post(
                GATEWAY_URL,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=self._headers(),
                timeout=to,
            )
        except requests.Timeout as exc:
            cost = round(_t.time() - t0, 1)
            msg = f"[Skill] ✗ {api_name} 超时（{cost}s，connect≤{to[0]}s read≤{to[1]}s）：{exc}"
            log.warning("Skill 调用超时 %s (cost=%ss): %s", api_name, cost, exc)
            self.warning.emit(msg)
            return {
                "ok": False, "http": 0, "api_name": api_name,
                "raw": {}, "data": {},
                "error": "timeout", "errCode": None, "errMsg": None,
            }
        except requests.RequestException as exc:
            cost = round(_t.time() - t0, 1)
            msg = f"[Skill] ✗ {api_name} 网络异常（{cost}s）：{exc}"
            log.warning("Skill 调用失败 %s 网络异常（cost=%ss）：%s", api_name, cost, exc)
            self.warning.emit(msg)
            return {
                "ok": False, "http": 0, "api_name": api_name,
                "raw": {}, "data": {},
                "error": f"network:{exc}", "errCode": None, "errMsg": None,
            }

        http = int(resp.status_code)
        data: Any = {}
        cost = round(_t.time() - t0, 1)
        try:
            data = resp.json()
        except ValueError:
            txt = resp.text or ""
            log.warning("Skill %s 非 JSON（HTTP=%s，cost=%ss）前200=%s", api_name, http, cost, txt[:200])
            self.warning.emit(f"[Skill] ✗ {api_name} 返回非 JSON（HTTP={http}，{cost}s）")
            return {
                "ok": False, "http": http, "api_name": api_name,
                "raw": txt, "data": txt,
                "error": "invalid_json", "errCode": None, "errMsg": None,
            }

        # Skill 错误可能放在顶层：{errCode, errMsg} / {code, message}
        errc = None
        errm = None
        if isinstance(data, dict):
            errc = data.get("errCode") if isinstance(data.get("errCode"), int) else data.get("code")
            errm = data.get("errMsg") or data.get("message") or data.get("error")
        ok = (http == 200) and not (isinstance(errc, int) and errc < 0) and not errm
        node = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), (dict, list)) else data
        status_line = (
            f"[Skill] ✓ {api_name} OK（HTTP={http}，{cost}s）" if ok
            else f"[Skill] ! {api_name} 返回错误（HTTP={http}，{cost}s，errCode={errc} errMsg={(str(errm)[:80] if errm else '')}）"
        )
        (self.message if ok else self.warning).emit(status_line)
        return {
            "ok": bool(ok), "http": http, "api_name": api_name,
            "raw": data, "data": node,
            "error": (None if ok else (f"errCode={errc}" if isinstance(errc, int) else (str(errm) or "http_non_200"))),
            "errCode": (int(errc) if isinstance(errc, int) else None),
            "errMsg": (str(errm) if errm else None),
        }

    # ---------------- 常用包装 ----------------
    def list_capabilities(self) -> dict:
        """/_list：验证 API Key 有效性，并返回当前 Skill 支持的能力列表。"""
        return self.call("/_list")

    def readdata_detail(self, mode: str = "overall", **extra: Any) -> dict:
        """/readdata/detail：阅读统计（总体/今日/习惯/…）。官方文档示例 mode="overall"。"""
        params = {"mode": mode}
        params.update({k: v for k, v in extra.items() if v is not None})
        return self.call("/readdata/detail", params)

    def shelf_sync(self) -> dict:
        """/shelf/sync：书架全量（电子书/有声/文章收藏 + 在读/已读/未开启）。"""
        return self.call("/shelf/sync")

    def book_info(self, book_id: str) -> dict:
        """/book/info：书名/作者/评分/进度/章节目录/…（需要真实 bookId）。"""
        return self.call("/book/info", {"bookId": book_id})

    def store_search(self, keyword: str, *, count: int = 10, scope: int = 10) -> dict:
        """/store/search：书籍搜索。"""
        return self.call(
            "/store/search",
            {"keyword": keyword, "count": int(count), "scope": int(scope)},
        )

    # ---------------- 聚合一次同步（调度器每 N 次成功读触发一次） ----------------
    # 单次全量同步的"总墙钟超时"：超过 N 秒视为 fetch_all 挂住，直接返回 timeout（防止 UI 按钮永远禁用）
    FETCH_ALL_TOTAL_TIMEOUT_SEC = 20

    def _fetch_all_inner(self, *, current_book_id: str | None,
                         current_reader_id: str | None) -> dict:
        """fetch_all 实际业务逻辑：多 mode 拉统计 + 书架 + 当前书 串行请求 + 汇总。
        被 ThreadPoolExecutor 包总超时。"""
        now = int(time.time())
        if not self.enabled:
            return {
                "ok": False, "fetched_at": now,
                "error": "disabled: no wrk- API Key",
                "readdata": {}, "shelf": {}, "book_info": None,
                "summary": self._empty_summary("skill_disabled"),
                "current_book": None,
            }
        self.message.emit("[Skill] ▶ fetch_all 开始：多模式 /readdata/detail + /shelf/sync + （当前书）/book/info")

        # —— 1) 多模式 /readdata/detail 串行收集（overall / daily / habit / weekly / monthly）
        # 各 mode 返回字段完全不同；官方网关偶尔对并发/快连发 HTTP=499（客户端请求被取消或限流），
        # 所以加 80ms 间隔 + 遇到 499 时自动降级"继续查下一个 mode"，绝不阻塞。
        readdata_modes: list[tuple[str, dict]] = []
        for mode in ("overall", "daily", "habit", "weekly", "monthly"):
            if mode != "overall":
                time.sleep(0.08)
            try:
                r = self.readdata_detail(mode)
            except Exception as exc:  # noqa: BLE001
                self.warning.emit(f"[Skill] ! /readdata/detail mode={mode} 抛异常：{exc}")
                continue
            http = int((r or {}).get("http") or 0)
            if http == 499:
                self.message.emit(f"[Skill] ⓘ /readdata/detail(mode={mode}) HTTP=499（网关对快速连续请求的轻度限流，跳过此 mode，继续其它）")
                continue
            readdata_modes.append((mode, r))
            # 关键诊断：HTTP=200 但 extract 老失败时，我们需要知道完整字段清单（只列 key 名，不含任何值）
            if r.get("ok"):
                raw = r.get("raw")
                struct = self._describe_structure(raw, depth=2)
                self.message.emit(f"[Skill] ⓘ /readdata/detail(mode={mode}) 结构 = {struct[:800]}")
                if isinstance(raw, dict):
                    all_keys = list(raw.keys())
                    self.message.emit(f"[Skill] ⓘ /readdata/detail(mode={mode}) 完整 top keys({len(all_keys)}) = {all_keys}")
                elif isinstance(raw, list) and raw:
                    self.message.emit(f"[Skill] ⓘ /readdata/detail(mode={mode}) root=list len={len(raw)} 首项 keys = {list(raw[0].keys()) if isinstance(raw[0], dict) else type(raw[0]).__name__}")
        # 取第一个 ok 的当"主响应"，但 extract 会合并所有 mode 的 data
        r_readdata: dict = readdata_modes[0][1] if readdata_modes else {
            "ok": False, "http": 0, "api_name": "/readdata/detail", "raw": None, "data": None,
            "error": "no_readdata_modes_succeeded",
        }

        # —— 2) /shelf/sync
        r_shelf = self.shelf_sync()
        if r_shelf.get("ok"):
            sdata = r_shelf.get("data")
            shelf_desc = self._describe_structure(sdata, depth=2)
            self.message.emit(f"[Skill] ⓘ /shelf/sync data 结构 = {shelf_desc[:700]}")
            if isinstance(sdata, dict):
                self.message.emit(f"[Skill] ⓘ /shelf/sync data 完整 top keys({len(sdata)}) = {list(sdata.keys())}")
                # 书架 364 本书中抽一本样本：完整 12 字段名 + 1 本 top 级值类型
                books = sdata.get("books")
                if isinstance(books, list) and books:
                    sample = books[0]
                    if isinstance(sample, dict):
                        self.message.emit(f"[Skill] ⓘ /shelf/sync books[0] 完整 keys({len(sample)}) = {list(sample.keys())}")
                        self.message.emit(f"[Skill] ⓘ /shelf/sync books[0] 结构 = {self._describe_structure(sample, depth=2)}")
                # mp.book 字段
                mp = sdata.get("mp")
                if isinstance(mp, dict) and isinstance(mp.get("book"), dict):
                    mb = mp["book"]
                    self.message.emit(f"[Skill] ⓘ /shelf/sync mp.book 完整 keys({len(mb)}) = {list(mb.keys())}")
            elif isinstance(sdata, list) and sdata and isinstance(sdata[0], dict):
                self.message.emit(f"[Skill] ⓘ /shelf/sync root=list len={len(sdata)} 首项完整 keys = {list(sdata[0].keys())}")
            # 先算 shelf_best 一次并同时诊断——把最佳书的完整 key 也打出来
            shelf_best_before = self._pick_best_book_from_shelf(r_shelf.get("data"))
            if isinstance(shelf_best_before, dict):
                self.message.emit(
                    f"[Skill] ⓘ /shelf/sync 最佳书 完整 keys({len(shelf_best_before)}) = {list(shelf_best_before.keys())}"
                )
                # 诊断：是否有进度相关字段
                pkeys_found = [k for k in shelf_best_before.keys() if any(
                    t in str(k).lower()
                    for t in ("progress", "percent", "ratio", "status", "finish", "readupdate", "position", "page")
                )]
                if pkeys_found:
                    self.message.emit(f"[Skill] ⓘ /shelf/sync 最佳书 疑似进度/状态字段 = {pkeys_found} 样本值 = { {k:self._describe_structure(shelf_best_before.get(k), depth=1) for k in pkeys_found} }")

        # —— 3) 当前书 book_info
        r_book: dict | None = None
        resolved_book_id = ""
        shelf_best = self._pick_best_book_from_shelf(r_shelf.get("data")) if r_shelf else None
        if current_book_id:
            resolved_book_id = str(current_book_id)
        elif shelf_best is not None:
            resolved_book_id = str(shelf_best.get("bookId") or "").strip()
        if resolved_book_id:
            self.message.emit(f"[Skill]   · 当前书 bookId={resolved_book_id[:20]}…，请求 /book/info")
            r_book = self.book_info(resolved_book_id)
            if isinstance(r_book, dict) and r_book.get("ok"):
                raw = r_book.get("raw")
                if raw is not None:
                    bdesc = self._describe_structure(raw, depth=2)
                    self.message.emit(f"[Skill] ⓘ /book/info 结构 = {bdesc[:700]}")
                    if isinstance(raw, dict):
                        self.message.emit(f"[Skill] ⓘ /book/info 完整 top keys({len(raw)}) = {list(raw.keys())}")
                        pkeys_found = [k for k in raw.keys() if any(
                            t in str(k).lower()
                            for t in ("progress", "percent", "ratio", "read", "position", "chapter", "page", "remain")
                        )]
                        if pkeys_found:
                            self.message.emit(f"[Skill] ⓘ /book/info 疑似进度/阅读相关字段 = {pkeys_found} 结构 = { {k:self._describe_structure(raw.get(k), depth=2) for k in pkeys_found} }")

        # —— 4) 拼 summary（给每个节点附带 mode 标签，这样 extract 可以根据 mode 用真实契约）——
        #    tagged: list[tuple[mode, node]] — node 是 data/raw 两个节点之一
        tagged_readdata_nodes: list[tuple[str, Any]] = []
        for _mode, _rr in readdata_modes:
            if _rr.get("ok"):
                added = False
                for key in ("data", "raw"):
                    n = _rr.get(key)
                    if isinstance(n, (dict, list)):
                        tagged_readdata_nodes.append((_mode, n))
                        added = True
                if not added:
                    # ok=True 但 data/raw 都不是 struct → 把整个 _rr 放进去（兜底）
                    tagged_readdata_nodes.append((_mode, _rr))
        shelf_node = r_shelf.get("data") if r_shelf.get("ok") else None
        summary = self._extract_readdata_summary(
            r_readdata,
            tagged_nodes=tagged_readdata_nodes,
            shelf_data=shelf_node,
        )
        # 诊断日志：若任一桶仍为 None，贴出已解析值便于定位
        if any(summary.get(k) is None for k in ("today_seconds", "week_seconds", "month_seconds", "total_seconds")):
            self.warning.emit(
                "[Skill] ⓘ 时长提取有缺口："
                f"today={summary.get('today_seconds')}s week={summary.get('week_seconds')}s "
                f"month={summary.get('month_seconds')}s total={summary.get('total_seconds')}s "
                f"source={summary.get('source')}；UI 将继续用 cookie 会话 API 做兜底填充。"
            )

        # —— 5) 拼 current_book（优先级：book_info > shelf_best > 入参 reader_id 兜底）——
        current_book = self._merge_current_book(
            r_book_info=r_book,
            shelf_best=shelf_best,
            fallback_book_id=resolved_book_id,
            fallback_reader_id=current_reader_id,
        )
        # 额外诊断：当 current_book 里 progress 没取到时提示
        if isinstance(current_book, dict) and not current_book.get("progress"):
            reasons = []
            if isinstance(r_book, dict) and r_book.get("ok"):
                bd = r_book.get("data") if isinstance(r_book.get("data"), dict) else r_book.get("raw")
                if isinstance(bd, dict):
                    pkeys = [k for k in ("readingProgress", "readPercent", "progress", "readRatio",
                                          "lastReadPercent", "percent", "readingStatus")
                             if bd.get(k) is not None]
                    reasons.append(f"/book/info 里相关字段 = {pkeys}")
            if shelf_best is not None:
                spkeys = [k for k in ("readingProgress", "readPercent", "progress", "readRatio")
                          if shelf_best.get(k) is not None]
                reasons.append(f"/shelf/sync 最佳书里相关字段 = {spkeys}")
            if reasons:
                self.message.emit("[Skill] ⓘ 进度未取到：" + "；".join(reasons))

        ok_overall = any(_rr.get("ok") for (_m, _rr) in readdata_modes) or bool(r_shelf.get("ok"))
        result = {
            "ok": ok_overall,
            "fetched_at": now,
            "error": (None if ok_overall else (r_readdata.get("error") or r_shelf.get("error") or "skill_all_failed")),
            "readdata": {
                "ok": any(_rr.get("ok") for (_m, _rr) in readdata_modes) if readdata_modes else False,
                "http": max(
                    [int(_rr.get("http") or 0) for (_m, _rr) in readdata_modes] + [0]
                ),
                "error": r_readdata.get("error"),
                "raw_keys": (
                    list(r_readdata.get("raw").keys())[:16] if isinstance(r_readdata.get("raw"), dict) else []
                ),
                "modes_checked": [m for (m, _rr) in readdata_modes if _rr.get("ok")],
            },
            "shelf": {
                "ok": r_shelf.get("ok"), "http": r_shelf.get("http"),
                "error": r_shelf.get("error"),
            },
            "book_info": ({
                "ok": r_book.get("ok"), "http": r_book.get("http"), "error": r_book.get("error")
            } if isinstance(r_book, dict) else None),
            "summary": summary,
            "current_book": current_book,
        }
        self._last_sync_ts = now
        self._last_sync = result
        self.sync_completed.emit(result)
        any_ok = lambda lst: sum(1 for (_, x) in lst if x.get("ok"))
        self.message.emit(
            f"[Skill] ■ fetch_all 结束：readdata({any_ok(readdata_modes)}/{len(readdata_modes)} modes)="
            f"{'✓' if any_ok(readdata_modes) > 0 else '✗'} "
            f"shelf={'✓' if r_shelf.get('ok') else '✗'} "
            f"book_info={'✓' if isinstance(r_book, dict) and r_book.get('ok') else ('—' if r_book is None else '✗')}"
        )
        return result

    def fetch_all(self, *, current_book_id: str | None = None,
                  current_reader_id: str | None = None) -> dict:
        """对外入口：用 ThreadPoolExecutor 包 _fetch_all_inner，保证 FETCH_ALL_TOTAL_TIMEOUT_SEC 必返回。

        这样无论 requests 内部 timeout 是否生效（某些 requests/urllib3 + Windows 组合下 connect 阶段
        可能突破 timeout），整体 fetch_all 一定在 20 秒内 return，UI 侧看门狗 + 调度器 watchdog 有机会解锁。
        """
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutTimeout

        def _empty_timeout_result(now_ts: int) -> dict:
            return {
                "ok": False,
                "fetched_at": now_ts,
                "error": f"fetch_all_total_timeout:{self.FETCH_ALL_TOTAL_TIMEOUT_SEC}s",
                "readdata": {"ok": False, "http": 0, "error": "fetch_all_timeout", "raw_keys": []},
                "shelf": {"ok": False, "http": 0, "error": "fetch_all_timeout"},
                "book_info": None,
                "summary": self._empty_summary("skill_fetch_all_timeout"),
                "current_book": None,
            }

        now = int(time.time())
        if not self.enabled:
            # 没 key 直接走 inner 里的 no_key 分支（更快，不走线程池）
            return self._fetch_all_inner(
                current_book_id=current_book_id, current_reader_id=current_reader_id
            )
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="SkillFetchAll")
        try:
            fut = executor.submit(
                self._fetch_all_inner,
                current_book_id=current_book_id,
                current_reader_id=current_reader_id,
            )
            try:
                return fut.result(timeout=self.FETCH_ALL_TOTAL_TIMEOUT_SEC)
            except _FutTimeout:
                msg = (
                    f"[Skill] ✗✗✗ fetch_all 总体超过 {self.FETCH_ALL_TOTAL_TIMEOUT_SEC}s 未返回，"
                    "已强制中断（可能是 requests/DNS 卡住）。日志已写入，UI 将自动解锁按钮。"
                )
                log.warning("Skill fetch_all total timeout (%ss) hit", self.FETCH_ALL_TOTAL_TIMEOUT_SEC)
                self.error.emit(msg)
                res = _empty_timeout_result(now)
                self.sync_completed.emit(res)
                return res
        finally:
            # shutdown(wait=False) 直接丢弃还没启动的任务；已启动的线程会继续跑到 requests 自己 timeout
            # 但主线程已经拿到了 empty_timeout_result 返回了
            try:
                executor.shutdown(wait=False, cancel_futures=True)  # type: ignore[call-arg]
            except TypeError:
                executor.shutdown(wait=False)

    # ---------------- 私有工具：结构诊断 + 字段提取 + 格式 ----------------
    def _describe_structure(self, obj: Any, *, depth: int = 2, _cur: int = 0, _top: bool = True) -> str:
        """返回 JSON 结构的紧凑型人类可读描述（只看字段名+类型+数量，不含敏感值），
        用于诊断"HTTP=200 但字段不匹配"时快速知道真实契约。"""
        if _cur > depth:
            return "…"
        if obj is None:
            return "null"
        if isinstance(obj, bool):
            return "bool"
        if isinstance(obj, int):
            # 避免敏感值：用范围代替
            if obj == 0:
                return "int=0"
            if abs(obj) < 1000:
                return f"int~{len(str(abs(obj)))}d"
            if abs(obj) > 10 ** 10:
                return "int(ms级或ID)"
            return f"int~{len(str(abs(obj)))}d"
        if isinstance(obj, float):
            return "float"
        if isinstance(obj, str):
            L = len(obj)
            if L == 0:
                return "str=0"
            # 疑似 时间字符串 / URL / 短文本 / 超长 JSON 文本
            sample = obj.replace("\n", " ")[:40]
            sample_esc = sample.replace("|", "/").replace("{", "<").replace("}", ">")
            if L < 60:
                return f'str({L})"{sample_esc}"'
            return f'str({L})"{sample_esc}…"'
        if isinstance(obj, list):
            if not obj:
                return "list[0]"
            if len(obj) == 1:
                return f"list[1]=[{self._describe_structure(obj[0], depth=depth, _cur=_cur+1, _top=False)}]"
            # 看前两个元素的类型是否一致
            a = self._describe_structure(obj[0], depth=depth, _cur=_cur+1, _top=False)
            b = self._describe_structure(obj[1], depth=depth, _cur=_cur+1, _top=False)
            if a == b:
                return f"list[{len(obj)}] of {a}"
            return f"list[{len(obj)}]=[{a}, {b}, …]"
        if isinstance(obj, dict):
            if not obj:
                return "dict[0]"
            keys = list(obj.keys())
            # 2~5 个 key 深度内展开；更多则只列名称+类型
            items = []
            show_keys = keys[:8]
            for k in show_keys:
                v = obj.get(k)
                vt = self._describe_structure(v, depth=depth, _cur=_cur + 1, _top=False)
                items.append(f"{k}:{vt}")
            more = "" if len(keys) <= 8 else f" (+{len(keys) - 8} keys)"
            return "dict[" + str(len(keys)) + "]{" + "; ".join(items) + more + "}"
        return type(obj).__name__

    @staticmethod
    def _empty_summary(source: str) -> dict:
        return {
            "today_seconds": None, "today_hm": "—",
            "week_seconds": None, "week_hm": "—",
            "month_seconds": None, "month_hm": "—",
            "total_seconds": None, "total_hm": "—",
            "source": source,
        }

    @staticmethod
    def _coerce_int(v: Any) -> int | None:
        try:
            if isinstance(v, bool):
                return None
            if v is None:
                return None
            if isinstance(v, (int, float)):
                return int(v)
            s = str(v).strip()
            if not s:
                return None
            return int(float(s))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _format_hm(seconds: int | None) -> str:
        if not isinstance(seconds, int) or seconds <= 0:
            return "—"
        h, s = divmod(max(0, seconds), 3600)
        m, s = divmod(s, 60)
        if h > 0:
            return f"{h}h{m:02d}m"
        return f"{m}m{s:02d}s" if m else f"{s}s"

    def _extract_readdata_summary(self, r_readdata: dict, *,
                                  tagged_nodes: list[tuple[str, Any]] | None = None,
                                  extra_nodes: list | None = None,
                                  shelf_data: Any = None) -> dict:
        """从多组 readdata 响应 + 可选 shelf_data 里抠出 今日/周/月/累计 秒数。

        真实契约（基于用户 21:28 运行时结构）：
          - /readdata/detail 的 top-level 有一个 `readTimes: dict[timestamp_seconds] = seconds_int`
            * overall：keys = 每年的年初 UTC 时间戳，value = 该年累计阅读秒数
            * weekly/monthly：keys = 连续 N 天的 UTC 00:00 分桶，value = 那天的阅读秒数
          - weekly/monthly 还有直接的 top-level：`totalReadTime`（本周/本月秒数）、
            `dayAverageReadTime`（日均秒）、`readDays`（有效阅读天数）
          - overall 的 top-level + 15 截断 keys 很可能有 `totalReadTime / continueDays / readDays`

        参数：
          tagged_nodes: list[(mode, node)] — 每个节点明确所属 mode；可精准按 mode 解释字段。
          extra_nodes: list[Any] — 未打 mode 标签的节点（向后兼容）。
          shelf_data: /shelf/sync data 节点（兜底聚合每本书阅读时长）。
        """
        source_tag = "readdata_detail_multi"
        empty = self._empty_summary(source_tag)
        ok_root_found = False

        # —— 准备 Beijing-time 今天/本周/本月 的分桶边界 ——
        import datetime as _dt
        # WeChat 全平台按"北京时间（UTC+8）"统计日/周/月
        BEIJING = _dt.timezone(_dt.timedelta(hours=8))
        now_bj = _dt.datetime.now(BEIJING)
        today_bj_midnight_bj = now_bj.replace(hour=0, minute=0, second=0, microsecond=0)
        today_bucket_ts_utc = int(today_bj_midnight_bj.timestamp())  # UTC 时间戳 = Beijing 00:00 的 Unix 时间戳
        # 本周：周一 00:00（Beijing week 从周一开始）
        monday_bj = today_bj_midnight_bj - _dt.timedelta(days=today_bj_midnight_bj.weekday())
        week_start_bucket = int(monday_bj.timestamp())
        # 本月：1 号 00:00
        month1_bj = today_bj_midnight_bj.replace(day=1)
        month_start_bucket = int(month1_bj.timestamp())
        # 下个月初
        if month1_bj.month == 12:
            next_month1 = month1_bj.replace(year=month1_bj.year + 1, month=1, day=1)
        else:
            next_month1 = month1_bj.replace(month=month1_bj.month + 1, day=1)
        month_end_bucket_excl = int(next_month1.timestamp())
        next_week_bucket_excl = week_start_bucket + 7 * 86400
        tomorrow_bucket_excl = today_bucket_ts_utc + 86400

        today_sec: int | None = None
        week_sec: int | None = None
        month_sec: int | None = None
        total_sec: int | None = None

        # 收集所有 roots（+ 模式标签）
        all_tagged: list[tuple[str, Any]] = []
        if isinstance(r_readdata, dict) and r_readdata.get("ok"):
            for key in ("data", "raw"):
                n = r_readdata.get(key)
                if isinstance(n, (dict, list)):
                    all_tagged.append(("_primary", n))
                    ok_root_found = True
        for mode, node in tagged_nodes or []:
            if isinstance(node, (dict, list)):
                all_tagged.append((mode, node))
                ok_root_found = True
        for n in extra_nodes or []:
            if isinstance(n, (dict, list)):
                all_tagged.append(("unknown", n))
                ok_root_found = True

        # —— 分钟→秒转换辅助（嵌套感知）
        def _pick_minutes(node: Any, keys: list[str]) -> int | None:
            if not isinstance(node, dict):
                return None
            for k in keys:
                mv = node.get(k)
                mi = self._coerce_int(mv)
                if isinstance(mi, int):
                    return mi * 60
            for child in ("data", "detail", "summary", "today", "week", "month", "total",
                          "readingSummary", "summaryData"):
                v = node.get(child)
                if isinstance(v, dict):
                    got = _pick_minutes(v, keys)
                    if got is not None:
                        return got
            return None

        def _assign_max(bucket: str, val: int) -> None:
            nonlocal today_sec, week_sec, month_sec, total_sec
            if val <= 0:
                return
            if bucket == "today" and (today_sec is None or val > today_sec):
                today_sec = val
            elif bucket == "week" and (week_sec is None or val > week_sec):
                week_sec = val
            elif bucket == "month" and (month_sec is None or val > month_sec):
                month_sec = val
            elif bucket == "total" and (total_sec is None or val > total_sec):
                total_sec = val

        # —— 逐节点按 mode 提取 ——
        for mode, root in all_tagged:
            if not isinstance(root, (dict, list)):
                continue

            # A) 先读 readTimes 分桶（最重要的真实合同来源）
            read_times: dict | None = None
            if isinstance(root, dict):
                if isinstance(root.get("readTimes"), dict):
                    read_times = root["readTimes"]
            if isinstance(read_times, dict):
                today_sum = 0
                week_sum = 0
                month_sum = 0
                year_now_sum = 0
                year_keys_sum = 0
                had_today_bucket = False
                had_week_bucket = False
                had_month_bucket = False
                for ts_key, val in read_times.items():
                    ts_int = self._coerce_int(ts_key)
                    sec_int = self._coerce_int(val)
                    if not isinstance(ts_int, int) or not isinstance(sec_int, int):
                        continue
                    # 毫秒时间戳兼容（13 位）
                    if ts_int > 10 ** 12:
                        ts_int = ts_int // 1000
                    if sec_int > 10 ** 8:
                        continue  # 秒数过大 → 非时长
                    if sec_int <= 0:
                        continue
                    # 日归属
                    if today_bucket_ts_utc <= ts_int < tomorrow_bucket_excl:
                        today_sum += sec_int
                        had_today_bucket = True
                    if week_start_bucket <= ts_int < next_week_bucket_excl:
                        week_sum += sec_int
                        had_week_bucket = True
                    if month_start_bucket <= ts_int < month_end_bucket_excl:
                        month_sum += sec_int
                        had_month_bucket = True
                    # overall 模式：按年初分桶 → 全部加起来 = 账号累计（年度子合计当累计估算）
                    # 当 key 正好是 1 月 1 日 00:00:00 UTC 时，按年度累加到 year_keys_sum
                    try:
                        d = _dt.datetime.fromtimestamp(ts_int, tz=_dt.timezone.utc)
                        if d.month == 1 and d.day == 1 and d.hour == 0 and d.minute == 0 and d.second == 0:
                            year_keys_sum += sec_int
                            # 今年（Beijing calendar year）的单独一个子桶
                            beijing_now_year = now_bj.year
                            if d.year == beijing_now_year:
                                year_now_sum += sec_int
                    except (OverflowError, OSError, ValueError):
                        pass
                if had_today_bucket:
                    _assign_max("today", today_sum)
                if had_week_bucket:
                    _assign_max("week", week_sum)
                if had_month_bucket:
                    _assign_max("month", month_sum)
                if year_keys_sum > 0:
                    _assign_max("total", year_keys_sum)

            # B) 按"显式 top-level 秒级/分钟级字段"精确取（每个 mode 都扫一遍）
            if isinstance(root, dict):
                # —— total / weekly / monthly 都可能有直接的 totalReadTime / totalSeconds
                for anchors, bucket in (
                    (
                        [
                            "todayTotalSeconds", "todaySeconds", "todayReadingSeconds", "todayTime",
                            "todayReadSeconds", "readingSeconds", "today", "readSeconds",
                            "time", "readingTime", "readTime",
                            "todayReadTime", "today_read_time", "todaySecondsRead",
                        ],
                        "today",
                    ),
                    (
                        [
                            "weekTotalSeconds", "weekSeconds", "weekReadingSeconds", "weekTime",
                            "weekReadSeconds", "weeklySeconds", "week",
                        ],
                        "week",
                    ),
                    (
                        [
                            "monthTotalSeconds", "monthSeconds", "monthReadingSeconds", "monthTime",
                            "monthReadSeconds", "monthlySeconds", "month",
                        ],
                        "month",
                    ),
                    (
                        [
                            "totalSeconds", "totalTime", "totalReadSeconds", "totalReadingSeconds",
                            "allTime", "sumSeconds", "total", "sum", "all", "totalReadTime",
                        ],
                        "total",
                    ),
                ):
                    got = self._pick_seconds(root, anchors)
                    if isinstance(got, int) and got > 0:
                        _assign_max(bucket, got)
                # —— weekly 特殊：totalReadTime 顶层字段的语义就是"本周总秒"（非累计）
                if mode == "weekly":
                    trt = self._coerce_int(root.get("totalReadTime"))
                    if isinstance(trt, int) and trt > 0 and trt < 10 ** 7:  # 本周合理上限 ~ 115 天 / 实际上限 7 天（~60 万秒）
                        _assign_max("week", trt if trt < 10 ** 9 else trt // 1000)
                    # 日均 × 天数
                    davg = self._coerce_int(root.get("dayAverageReadTime"))
                    rdays = self._coerce_int(root.get("readDays"))
                    if isinstance(davg, int) and isinstance(rdays, int) and davg > 0 and rdays > 0:
                        est = davg * rdays
                        if est < 7 * 24 * 3600:
                            _assign_max("week", est)
                        # 顺便估算月（如果是 monthly mode 也有）
                # —— monthly 直接字段：totalReadTime / dayAverage*days
                if mode == "monthly":
                    trt = self._coerce_int(root.get("totalReadTime"))
                    if isinstance(trt, int) and trt > 0 and trt < 10 ** 8:  # 本月合理上限 ~ 1157 天 / 实际上限 31 天（~268 万秒）
                        _assign_max("month", trt if trt < 10 ** 9 else trt // 1000)
                    davg = self._coerce_int(root.get("dayAverageReadTime"))
                    rdays = self._coerce_int(root.get("readDays"))
                    if isinstance(davg, int) and isinstance(rdays, int) and davg > 0 and rdays > 0:
                        est = davg * rdays
                        if 0 < est < 32 * 24 * 3600:
                            _assign_max("month", est)
                # —— overall：totalReadTime 顶层字段极可能直接是 ACCOUNT TOTAL SECONDS（官方统计页就是它）
                if mode in ("overall", "_primary", "unknown"):
                    for top_total_key in ("totalReadTime", "totalSeconds", "allReadSeconds",
                                           "accountTotalSeconds", "totalReadSeconds",
                                           "sumReadSeconds", "cumulativeReadSeconds"):
                        tv = self._coerce_int(root.get(top_total_key))
                        if isinstance(tv, int) and tv > 1000:  # 账号累计至少 > ~17 分钟才合理
                            _assign_max("total", tv if tv < 10 ** 9 else tv // 1000)
                # 分钟类字段 × 60
                if today_sec is None:
                    today_sec = _pick_minutes(
                        root,
                        ["todayMinutes", "todayReadingMinutes", "readMinutes", "readingMinutes", "minutes"],
                    )
                if week_sec is None:
                    week_sec = _pick_minutes(root, ["weekMinutes", "weeklyMinutes"])
                if month_sec is None:
                    month_sec = _pick_minutes(root, ["monthMinutes", "monthlyMinutes"])
                if total_sec is None:
                    total_sec = _pick_minutes(
                        root,
                        ["totalMinutes", "sumMinutes", "allMinutes", "accumulateMinutes"],
                    )

            # C) 老的泛化深扫兜底（root 级一次深扫），保证任何漏网格式至少有一个估算
            if today_sec is None or week_sec is None or month_sec is None or total_sec is None:
                deep = self._deep_sum_time_buckets(root)
                if today_sec is None and isinstance(deep.get("today"), int) and deep["today"] > 0:
                    today_sec = deep["today"]
                if week_sec is None and isinstance(deep.get("week"), int) and deep["week"] > 0:
                    week_sec = deep["week"]
                if month_sec is None and isinstance(deep.get("month"), int) and deep["month"] > 0:
                    month_sec = deep["month"]
                if total_sec is None and isinstance(deep.get("total"), int) and deep["total"] > 0:
                    total_sec = deep["total"]

        # D) 书架兜底：若仍缺 today/total，按 shelf_data 里每本书 readingDuration 聚合
        if (today_sec is None or total_sec is None) and shelf_data is not None:
            books_list: list[dict] = []
            if isinstance(shelf_data, list):
                books_list = [x for x in shelf_data if isinstance(x, dict)]
            elif isinstance(shelf_data, dict):
                for key in ("books", "booklist", "items", "list", "data"):
                    v = shelf_data.get(key)
                    if isinstance(v, list):
                        cand = [x for x in v if isinstance(x, dict)]
                        if cand:
                            books_list = cand
                            break
            if books_list:
                agg_total = 0
                for b in books_list:
                    for durk in (
                        "readingDuration", "readSeconds", "readingSeconds", "readTime",
                        "lastReadSeconds", "totalReadingSeconds", "readingTime",
                        "duration",
                    ):
                        dv = self._coerce_int(b.get(durk))
                        if isinstance(dv, int) and dv > 0:
                            if dv > 10 ** 8 and dv % 1000 == 0:
                                dv = dv // 1000
                            if 5 <= dv <= 3650 * 24 * 3600:
                                agg_total += dv
                                break
                if agg_total > 0 and total_sec is None:
                    total_sec = agg_total

        if not ok_root_found and today_sec is None:
            return empty

        empty["today_seconds"] = today_sec
        empty["today_hm"] = self._format_hm(today_sec)
        empty["week_seconds"] = week_sec
        empty["week_hm"] = self._format_hm(week_sec)
        empty["month_seconds"] = month_sec
        empty["month_hm"] = self._format_hm(month_sec)
        empty["total_seconds"] = total_sec
        empty["total_hm"] = self._format_hm(total_sec)
        return empty

    def _pick_seconds(self, node: Any, keys: list[str]) -> int | None:
        """在字典（或已知嵌套层）里按 key 列表挑一个秒数字段。

        兼容形如：
          {"today":{"seconds": 7200}}
          {"summary":{"todayTotalSeconds": 7200}}
          [{"readingSeconds": 7200}]
        """
        # 1. 顶层 dict
        if isinstance(node, dict):
            # 先直接命中：{key: value}
            for k in keys:
                iv = self._coerce_int(node.get(k))
                if iv is not None:
                    return iv if iv < 10 ** 9 else iv // 1000
            # 再嵌套 data / detail / summary / today / week / month / total
            for child in ("data", "detail", "summary", "today", "week", "month", "total", "statistics", "stats"):
                v = node.get(child)
                if isinstance(v, dict):
                    got = self._pick_seconds(v, keys)
                    if got is not None:
                        return got
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict):
                            got = self._pick_seconds(item, keys)
                            if got is not None:
                                return got
            return None
        if isinstance(node, list):
            for item in node:
                if isinstance(item, dict):
                    got = self._pick_seconds(item, keys)
                    if got is not None:
                        return got
        return None

    def _deep_sum_time_buckets(self, node: Any) -> dict[str, int | None]:
        """深遍历，按字段名/父键命名近似，把数值归到 today/week/month/total 桶。"""
        result: dict[str, int | None] = {"today": None, "week": None, "month": None, "total": None}
        stack: list[tuple[Any, str]] = [(node, "")]
        while stack:
            cur, parent_key = stack.pop()
            if isinstance(cur, dict):
                for k, v in cur.items():
                    k_str = str(k)
                    stack.append((v, k_str.lower()))
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        iv = self._coerce_int(v)
                        if iv is None:
                            continue
                        # 毫秒过滤：>10^8 且能被 1000 整除的认为是毫秒
                        secs = iv
                        if iv > 10 ** 8 and iv % 1000 == 0:
                            secs = iv // 1000
                        # 只保留合理范围（单桶 > 5 秒 且 < 3650 天）
                        if secs < 5 or secs > 3650 * 24 * 3600:
                            continue
                        bucket: str | None = None
                        if any(x in k_str for x in ("today", "daily")):
                            bucket = "today"
                        elif any(x in k_str for x in ("week", "weekly", "7day")):
                            bucket = "week"
                        elif any(x in k_str for x in ("month", "monthly", "30day")):
                            bucket = "month"
                        elif any(x in k_str for x in ("total", "sum", "all", "accumulate")):
                            bucket = "total"
                        if bucket is None and any(x in parent_key for x in ("today", "daily")):
                            bucket = "today"
                        elif bucket is None and any(x in parent_key for x in ("week", "weekly")):
                            bucket = "week"
                        elif bucket is None and any(x in parent_key for x in ("month", "monthly")):
                            bucket = "month"
                        elif bucket is None and any(x in parent_key for x in ("total", "sum", "all")):
                            bucket = "total"
                        if bucket:
                            prev = result.get(bucket)
                            if prev is None or secs > prev:
                                # 取最大值，避免把"单本书"的秒数错误地覆盖为更小值
                                result[bucket] = secs
            elif isinstance(cur, list):
                for i, item in enumerate(cur):
                    stack.append((item, parent_key))
        return result

    def _extract_progress(self, src: dict | None) -> float | None:
        """从单本书 dict（/book/info data 或 shelf 单条 book 或 raw 嵌套）里提取 0~1 进度值。

        深扫：readingProgress / readPercent / progress / readRatio / percent / readingInfo.progress，
        兼容 0-1、0-100、0-10000 三种编码。找不到返回 None。
        """
        if not isinstance(src, dict):
            return None
        # 所有可能出现进度的字段名（key/嵌套 key），顺序优先度从上到下
        direct_keys = [
            "readingProgress", "readPercent", "progress", "readRatio", "percent",
            "lastReadPercent", "readingPercent", "reading_progress", "p",
        ]
        # 嵌套字典：readingInfo / reading / info / currentChapterInfo / summary / state 下的同名键
        nested_parents = ["readingInfo", "reading", "info", "summary", "state", "data", "bookInfo",
                          "current", "lastReadInfo", "book"]

        def _val_to_float(v: Any) -> float | None:
            if isinstance(v, bool):
                return None
            if isinstance(v, (int, float)):
                fv = float(v)
                # 常见分档：0-1 原生 / 1-100 百分比 / 101-10000 万分比；再大的当 0-100 截断
                if 0.0 <= fv <= 1.0:
                    return fv
                if 1.0 < fv <= 100.0:
                    return fv / 100.0
                if 100.0 < fv <= 10_000.0:
                    return fv / 10_000.0
                # 异常大的可能是毫秒/其他语义：忽略
                return None
            if isinstance(v, str):
                s = v.strip().rstrip("%")
                try:
                    fv = float(s)
                except (TypeError, ValueError):
                    return None
                if 0.0 <= fv <= 1.0:
                    return fv
                if 1.0 < fv <= 100.0:
                    return fv / 100.0
                if 100.0 < fv <= 10_000.0:
                    return fv / 10_000.0
                return None
            return None

        # A. 顶层直接键
        for k in direct_keys:
            if k in src:
                got = _val_to_float(src.get(k))
                if got is not None:
                    return got
        # B. 嵌套父级键下的直接键
        for parent in nested_parents:
            node = src.get(parent)
            if isinstance(node, dict):
                for k in direct_keys:
                    if k in node:
                        got = _val_to_float(node.get(k))
                        if got is not None:
                            return got
        # C. 完全深扫两层内任何键命中（作为兜底）
        for _k0, v0 in src.items():
            if isinstance(v0, dict):
                for _k1, v1 in v0.items():
                    k1l = str(_k1).lower()
                    if any(tok in k1l for tok in ("progress", "percent", "ratio")):
                        got = _val_to_float(v1)
                        if got is not None:
                            return got
        return None

    def _pick_best_book_from_shelf(self, shelf_data: Any) -> dict | None:
        """从 /shelf/sync 的 data 中挑出"正在读 + 进度最高"的那本书。兼容 list/dict。"""
        books: list[dict] = []
        if isinstance(shelf_data, list):
            books = [x for x in shelf_data if isinstance(x, dict)]
        elif isinstance(shelf_data, dict):
            # 文档没写死字段，常见：books / shelfBookIds 只是 ids / data
            for key in ("books", "booklist", "items", "list", "data"):
                v = shelf_data.get(key)
                if isinstance(v, list):
                    books = [x for x in v if isinstance(x, dict)]
                    if books:
                        break
            if not books and not isinstance(shelf_data.get("shelfBookIds"), (list, type(None))):
                # 有些格式是 {shelfBookIds: [...id...], books: {...id...}}
                # 尝试从 books: {id: {details}} 展开
                bmap = shelf_data.get("books")
                if isinstance(bmap, dict):
                    books = [dict(v, **{"bookId": str(k)}) for k, v in bmap.items() if isinstance(v, dict)]
        if not books:
            return None

        def progress_of(b: dict) -> float:
            got = self._extract_progress(b)
            return got if got is not None else 0.0

        best: dict | None = None
        best_score = -1.0
        for b in books:
            rp = progress_of(b)
            bid = str(b.get("bookId") or b.get("book_id") or "").strip()
            if not bid:
                continue
            # 在读 > 已读 > 未开启
            state = b.get("readingStatus") or b.get("status") or ""
            state_score = 100 if state in (1, "reading", "在读") else (50 if state in (2, "finished", "已读") else 0)
            finished = bool(b.get("finished")) or rp >= 0.999
            score = rp * 1000.0 - (1000 if finished else 0) + state_score
            if score > best_score:
                best_score = score
                best = b
        return best

    def _merge_current_book(self, *, r_book_info: dict | None, shelf_best: dict | None,
                            fallback_book_id: str, fallback_reader_id: str | None) -> dict | None:
        """综合 book_info / shelf_best，返回一个 WeReadApi.set_current_book 可直接用的 payload。

        新逻辑：/book/info 路径先填字段；然后用 shelf_best **补足缺失字段**（而不是二选一替代），
        这样即使 /book/info 只返回 title 而没进度，也能从 shelf 里拿到 readingProgress。
        """
        final: dict | None = None
        source = ""

        # 1) /book/info（字段最权威：title/author/进度/章节）
        book_info_progress: float | None = None
        book_info_title = ""
        book_info_bid = ""
        if isinstance(r_book_info, dict) and r_book_info.get("ok"):
            for data_node in (
                r_book_info.get("data"),
                r_book_info.get("raw"),
            ):
                if not isinstance(data_node, dict):
                    continue
                title = str(data_node.get("title") or data_node.get("bookTitle") or "").strip()
                author = str(data_node.get("author") or "").strip()
                bid = str(data_node.get("bookId") or data_node.get("book_id") or "").strip()
                progress: float | None = self._extract_progress(data_node)
                # readerBookId / readerId / encBookId / reader_id
                reader_id = str(
                    data_node.get("readerBookId") or data_node.get("readerId")
                    or data_node.get("reader_book_id") or data_node.get("encBookId")
                    or ""
                ).strip()
                chapter_id = str(
                    data_node.get("chapterId") or data_node.get("chapterUid")
                    or data_node.get("currentChapter") or data_node.get("lastChapterUid")
                    or ""
                ).strip()
                if not bid and not title and not reader_id:
                    continue
                if bid:
                    book_info_bid = bid
                if title:
                    book_info_title = title
                if progress is not None:
                    book_info_progress = progress
                if not reader_id and fallback_reader_id:
                    reader_id = str(fallback_reader_id)
                if not reader_id and (bid or fallback_book_id):
                    reader_id = "wb" + (bid or str(fallback_book_id))
                final = {
                    "book_id": bid or str(fallback_book_id or ""),
                    "book_reader_id": reader_id,
                    "title": title,
                    "author": author,
                    "url": f"https://weread.qq.com/web/reader/{reader_id}" if reader_id else "",
                    "progress": progress,
                    "progress_text": (f"{progress * 100:.2f}%" if progress is not None else ""),
                    "chapter_id": chapter_id,
                    "source": "skill_book_info",
                }
                source = "skill_book_info"
                break  # 取第一个 ok 的 data/raw

        # 2) shelf_best 补齐 /book/info 缺失的字段（或者当 /book/info 完全没数据时兜底）
        if shelf_best is not None:
            shelf_progress = self._extract_progress(shelf_best)
            shelf_title = str(shelf_best.get("title") or shelf_best.get("bookTitle") or "").strip()
            shelf_author = str(shelf_best.get("author") or "").strip()
            shelf_bid = str(shelf_best.get("bookId") or shelf_best.get("book_id") or "").strip()
            shelf_reader_id = str(
                shelf_best.get("readerBookId") or shelf_best.get("readerId")
                or shelf_best.get("reader_book_id") or shelf_best.get("encBookId")
                or ""
            ).strip()
            shelf_chapter = str(
                shelf_best.get("chapterUid") or shelf_best.get("chapterId")
                or shelf_best.get("lastChapterUid") or ""
            ).strip()
            if final is None:
                # 完全没 /book/info → 用 shelf 全量打底
                if not shelf_reader_id and fallback_reader_id:
                    shelf_reader_id = str(fallback_reader_id)
                if not shelf_reader_id and (shelf_bid or fallback_book_id):
                    shelf_reader_id = "wb" + (shelf_bid or str(fallback_book_id or ""))
                if shelf_bid or shelf_title or shelf_reader_id:
                    final = {
                        "book_id": shelf_bid or str(fallback_book_id or ""),
                        "book_reader_id": shelf_reader_id,
                        "title": shelf_title,
                        "author": shelf_author,
                        "url": (f"https://weread.qq.com/web/reader/{shelf_reader_id}" if shelf_reader_id else ""),
                        "progress": shelf_progress,
                        "progress_text": (f"{shelf_progress * 100:.2f}%" if shelf_progress is not None else ""),
                        "chapter_id": shelf_chapter,
                        "source": "skill_shelf_sync",
                    }
                    source = "skill_shelf_sync"
            else:
                # /book/info 有数据 → shelf 补齐缺失
                merged_source_parts = [source or "skill_book_info"]
                if not final.get("title") and shelf_title:
                    final["title"] = shelf_title
                    merged_source_parts.append("shelf:title")
                if not final.get("book_id") and shelf_bid:
                    final["book_id"] = shelf_bid
                    merged_source_parts.append("shelf:book_id")
                if not final.get("author") and shelf_author:
                    final["author"] = shelf_author
                if not final.get("book_reader_id"):
                    if shelf_reader_id:
                        final["book_reader_id"] = shelf_reader_id
                        merged_source_parts.append("shelf:reader_id")
                    elif fallback_reader_id:
                        final["book_reader_id"] = str(fallback_reader_id)
                if final.get("progress") is None and shelf_progress is not None:
                    final["progress"] = shelf_progress
                    final["progress_text"] = f"{shelf_progress * 100:.2f}%"
                    merged_source_parts.append("shelf:progress")
                if not final.get("chapter_id") and shelf_chapter:
                    final["chapter_id"] = shelf_chapter
                if not final.get("url") and final.get("book_reader_id"):
                    final["url"] = f"https://weread.qq.com/web/reader/{final['book_reader_id']}"
                source = "+".join(merged_source_parts)
                final["source"] = source
        # 3) 最后 fallback：有 reader_id 就至少构造一个"能打开链接"的对象
        if final is None and (fallback_book_id or fallback_reader_id):
            reader_id = str(fallback_reader_id or ("wb" + str(fallback_book_id or "")))
            final = {
                "book_id": str(fallback_book_id or ""),
                "book_reader_id": reader_id,
                "title": book_info_title,
                "author": "",
                "url": f"https://weread.qq.com/web/reader/{reader_id}" if reader_id else "",
                "progress": book_info_progress,
                "progress_text": (f"{book_info_progress * 100:.2f}%" if book_info_progress is not None else ""),
                "chapter_id": "",
                "source": "fallback_reader_id",
            }
            source = "fallback_reader_id"
        if final is not None:
            final["source"] = source
            final["updated_at"] = int(time.time())
        return final
