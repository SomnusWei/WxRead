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
import os as _os
import random
import re as _re
import secrets
import threading
import time
import urllib.parse
from datetime import date as _Date  # noqa: F401
from pathlib import Path as _Path
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
BOOKLIST_URL = "https://weread.qq.com/web/shelf/booklist"            # 书架：含 title / bookId / readingProgress / cover

KEY = "3c5c8717f3daf09iop3423zafeqoi"

# —— 章节缓存文件：与 config.json 同目录，生命周期绑定当前登录 Cookie 指纹 ——
_WXREAD_ASSISTANT_DIRNAME = "WxReadAssistant"

def _resolve_appdata_dir() -> _Path:
    """返回 %APPDATA%/WxReadAssistant（项目打包后运行的正式目录），不存在就创建。"""
    appdata = _os.environ.get("APPDATA") or str(_Path.home() / "AppData" / "Roaming")
    d = _Path(appdata) / _WXREAD_ASSISTANT_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d

_CHAPTER_CACHE_NAME = "chapter_cache.json"
_READING_TIME_CACHE_NAME = "reading_time.json"
_SKILL_CACHE_NAME = "wxread-skill-cache.json"
# 开发模式：项目根（app/core/weread_api.py 上三级 = e:\item\wxread\）
_PROJECT_ROOT = _Path(__file__).resolve().parent.parent.parent
_CHAPTER_CACHE_DEV = _PROJECT_ROOT / _CHAPTER_CACHE_NAME
# 最终路径优先级：dev 存在就用 dev；否则用 APPDATA
CHAPTER_CACHE_PATH = _CHAPTER_CACHE_DEV if _CHAPTER_CACHE_DEV.exists() else (
    _resolve_appdata_dir() / _CHAPTER_CACHE_NAME
)
# 阅读累计本地持久化路径
_READING_TIME_DEV = _PROJECT_ROOT / _READING_TIME_CACHE_NAME
READING_TIME_PATH = _READING_TIME_DEV if _READING_TIME_DEV.exists() else (
    _resolve_appdata_dir() / _READING_TIME_CACHE_NAME
)
# Skill 网关缓存路径（last_summary 阅读统计）
_SKILL_CACHE_DEV = _PROJECT_ROOT / _SKILL_CACHE_NAME
SKILL_CACHE_PATH = _SKILL_CACHE_DEV if _SKILL_CACHE_DEV.exists() else (
    _resolve_appdata_dir() / _SKILL_CACHE_NAME
)
# Skill 网关入口
SKILL_GATEWAY_URL = "https://i.weread.qq.com/api/agent/gateway"

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

# ===== 方案A 默认固定模板（参考 findmover/wxread config.py 的 data 字段）=====
# 三体的固定模板：b 是三体 book_id，ps/pc/appId 是抓包样本中的固定值
DEFAULT_BOOK_ID: str = "ce032b305a9bc1ce0b0dd2a"  # 三体
DEFAULT_APP_ID: str = "wb182564874603h266381671"
DEFAULT_PS: str = "4ee326507a65a465g015fae"
DEFAULT_PC: str = "aab32e207a65a466g010615"


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
        # —— 需求：按 book_id 分桶的章节池（scoped）——
        #   解决：book=842609 这种非默认书，从 DEFAULT_CHAPTERS 拿伪造 chapterUid → 200 body 空
        #   refresh_chapters_for_book(bid) 成功时写入 self.scoped_chapters[bid]
        #   结构变更（v1）：从 list[str] → dict{uids:list, count:int, chapters:list, synckey, chapterUpdateTime, persisted_at, source}
        self.scoped_chapters: dict[str, dict] = {}
        # —— hex_short_id → numeric_bookId 映射（拦截器捕获时写入）——
        # 因为 reader URL 里的 hex id 不能直接给 chapterInfos 用，
        # 必须用数字 bookId；拦截器在浏览器请求 info?bookId=XXX 时
        # 捕获到数字 bookId，同时记录当前 reader URL 的 hex id 映射。
        self._hex_to_numeric_book_id: dict[str, str] = {}
        # —— cookie 指纹 & 章节持久化内部状态 ——
        self._chapter_cache_fp: str | None = None   # 本次启动/最近一次 set_session 重算的 16 位指纹
        self._last_read_empty_body: bool = False    # 最近一次 read_once 是否命中"HTTP 200 空 body"，供 scheduler 识别
        # —— 当前"正在读的书"持久化字段，供状态页/登录页/调度器统一真相
        self._book_lock = threading.RLock()
        self._current_book: dict | None = None
        self._last_read_ts: int = 0
        self._book_progress_override: float | None = None
        # —— 本地阅读累计（web 端无统计接口时的兜底方案）——
        # key=日期字符串 "YYYY-MM-DD", value=当日累计秒数
        self._daily_read_seconds: dict[str, int] = self._load_daily_read_seconds()
        self._daily_read_lock = threading.Lock()
        # —— JS 捕获的真实请求数据（用于构造真实 payload）——
        # 存储最近一次从浏览器捕获的 /web/book/read 请求 payload
        self._last_captured_read: dict | None = None
        # JS 捕获锁：一旦 JS 捕获到 /web/book/read，锁定 _current_book 与捕获模板保持一致，
        # 防止 refresh_current_book_from_shelf / login_browser_nav 等来源覆盖用户正在阅读的书。
        self._js_capture_locked: bool = False
        # 存储从浏览器捕获的章节列表（chapterInfos 响应）
        self._captured_chapter_list: list[dict] = []
        # 存储从浏览器捕获的所有章节 ID（按书籍分桶）
        self._captured_chapter_ids: dict[str, list[str]] = {}
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
        # —— 启动时载入章节缓存（按 cookie 指纹校验；不匹配就视为空）——
        self.load_chapter_cache()

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
        # —— 用户重新登录后：重算指纹 + 重新载入缓存（指纹变化时会自动丢弃老 bucket）——
        self.load_chapter_cache()

    # -------- 章节池持久化 · 指纹 & 磁盘读写 --------
    def calc_cookie_fingerprint(self, cookies_dict: dict[str, str] | None = None) -> str:
        """按方案 §2.3 计算 16 位 cookie 短指纹。
        anchor_keys=sorted([wr_skey, RK, ptcz, pac_uid, wr_vid])，缺失填 empty，
        值 strip 后去 \\s+，按 k=v& 拼接，UTF-8 → SHA-1 → 前 16 hex。"""
        if cookies_dict is None:
            # 优先从 session cookies 取；fallback 到 config
            cd: dict[str, str] = {}
            try:
                jar = getattr(self._session, "cookies", None)
                if jar is not None:
                    for c in jar:
                        try:
                            cd[c.name] = c.value or ""
                        except Exception:
                            pass
            except Exception:
                pass
            if not cd:
                cd = dict(self._cfg.get("cookies", {}) or {})
            cookies_dict = cd
        anchor_keys = sorted(["wr_skey", "RK", "ptcz", "pac_uid", "wr_vid"])
        parts: list[str] = []
        for k in anchor_keys:
            v = cookies_dict.get(k)
            if v is None:
                vs = "empty"
            else:
                vs = str(v).strip()
                vs = _re.sub(r"\s+", "", vs) if vs else "empty"
                if not vs:
                    vs = "empty"
            parts.append(f"{k}={vs}")
        concat = "&".join(parts)
        return hashlib.sha1(concat.encode("utf-8")).hexdigest()[:16]

    def load_chapter_cache(self) -> None:
        """启动/登录变更时载入 chapter_cache.json。
        步骤：算新 fp → 读盘 → 指纹匹配则载入 scoped_chapters；不匹配则保留空。
        任何读取异常 → log.warning 后继续（视为空），绝不抛错。"""
        # 1) 算新指纹
        new_fp = self.calc_cookie_fingerprint()
        # 2) 读盘
        raw: dict[str, Any] = {}
        try:
            path_val = (
                _CHAPTER_CACHE_DEV if _CHAPTER_CACHE_DEV.exists()
                else _resolve_appdata_dir() / _CHAPTER_CACHE_NAME
            )
            if path_val.exists():
                try:
                    with open(path_val, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    if not isinstance(raw, dict):
                        raw = {}
                except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                    log.warning("章节缓存 JSON 读取失败，视为空缓存：%s (%s)", path_val.name, exc)
                    raw = {}
        except Exception as exc:  # noqa: BLE001
            log.warning("章节缓存载入异常（忽略，继续）：%s", exc)
            raw = {}
        # 3) 指纹比对
        disk_fp = raw.get("cookie_fingerprint") or ""
        buckets = raw.get("chapter_buckets")
        if not isinstance(buckets, dict):
            buckets = {}
        if disk_fp and disk_fp == new_fp:
            # 指纹一致 → 载入内存（兼容老 list 结构）
            new_buckets: dict[str, dict] = {}
            for bid_raw, val in buckets.items():
                bid_n = str(bid_raw).strip()
                if not bid_n:
                    continue
                if isinstance(val, list):
                    # legacy：老结构 list[str] → 包成 dict
                    uids = [str(x) for x in val if str(x).strip()]
                    new_buckets[bid_n] = {
                        "uids": uids,
                        "count": len(uids),
                        "source": "legacy_list",
                        "persisted_at": 0,
                        "chapters": [],
                        "synckey": 0,
                        "chapterUpdateTime": 0,
                    }
                elif isinstance(val, dict):
                    # 兼容：uids 为空但 chapters 非空 → 从 chapterUid 动态拼
                    v = dict(val)
                    if not isinstance(v.get("uids"), list):
                        v["uids"] = []
                    if not v["uids"] and isinstance(v.get("chapters"), list):
                        uids_dyn: list[str] = []
                        for ch in v["chapters"]:
                            if isinstance(ch, dict):
                                u = ch.get("chapterUid")
                                if u is not None and str(u).strip():
                                    uids_dyn.append(str(u).strip())
                        v["uids"] = uids_dyn
                        v["count"] = len(uids_dyn)
                    if not isinstance(v.get("count"), int):
                        v["count"] = len(v.get("uids") or [])
                    new_buckets[bid_n] = v
            with self._book_lock:
                self.scoped_chapters = new_buckets
            log.info(
                "章节缓存载入成功：指纹匹配（fp=%s 前8位），共 %d 本书",
                new_fp[:8], len(new_buckets),
            )
        else:
            if disk_fp:
                log.info(
                    "Cookie 指纹变化，章节缓存整份丢弃（disk=%s new=%s），等待后续重拉",
                    (disk_fp or "")[:8], new_fp[:8],
                )
            else:
                log.info("章节缓存为空或首次运行（fp=%s 前8位），等待锁书时拉取", new_fp[:8])
            with self._book_lock:
                self.scoped_chapters = {}
        # 4) 保存当前指纹，供 save 时用
        self._chapter_cache_fp = new_fp

    def save_chapter_cache(self, book_id: str | None = None) -> None:
        """原子写章节缓存到磁盘。book_id=None 写整份 scoped_chapters；
        否则只局部覆盖该书 bucket（其它保留原盘内容）。"""
        try:
            # 1) 决定输出路径
            if _CHAPTER_CACHE_DEV.exists():
                out_path = _CHAPTER_CACHE_DEV
            else:
                out_path = _resolve_appdata_dir() / _CHAPTER_CACHE_NAME
            # 2) 组装 top-level
            fp = self._chapter_cache_fp or self.calc_cookie_fingerprint()
            data_out: dict[str, Any] = {
                "cookie_fingerprint": fp,
                "schema_version": 1,
                "chapter_buckets": {},
            }
            with self._book_lock:
                if book_id is None:
                    data_out["chapter_buckets"] = {
                        k: v for k, v in self.scoped_chapters.items() if isinstance(v, dict)
                    }
                else:
                    bid_n = str(book_id).strip()
                    # 局部写：先读盘 → 覆盖 bid → 写回
                    try:
                        if out_path.exists():
                            with open(out_path, "r", encoding="utf-8") as f:
                                old = json.load(f)
                            if isinstance(old, dict) and isinstance(old.get("chapter_buckets"), dict):
                                data_out["chapter_buckets"] = dict(old["chapter_buckets"])
                    except Exception:  # noqa: BLE001
                        pass
                    if bid_n in self.scoped_chapters and isinstance(self.scoped_chapters[bid_n], dict):
                        data_out["chapter_buckets"][bid_n] = dict(self.scoped_chapters[bid_n])
            # 3) 原子写：tmp + os.replace
            tmp_path = out_path.parent / (out_path.name + ".tmp")
            try:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data_out, f, ensure_ascii=False, indent=2)
                _os.replace(str(tmp_path), str(out_path))
            finally:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:
                    pass
            log.debug(
                "章节缓存写盘成功：book_id=%s books=%d size=%s",
                book_id, len(data_out["chapter_buckets"]),
                f"{out_path.stat().st_size}B" if out_path.exists() else "-",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("章节缓存写盘失败（非致命，忽略）：book_id=%s err=%s", book_id, exc)

    # ================================================================
    # 本地阅读累计（web 端无统计接口时的兜底方案）
    # ================================================================

    @staticmethod
    def _load_daily_read_seconds() -> dict[str, int]:
        """从 reading_time.json 加载历史累计。"""
        try:
            if READING_TIME_PATH.exists():
                with open(READING_TIME_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("daily"), dict):
                    cleaned: dict[str, int] = {}
                    for k, v in data["daily"].items():
                        if isinstance(v, (int, float)):
                            cleaned[str(k)] = int(v)
                    log.info("阅读累计加载成功：%d 天", len(cleaned))
                    return cleaned
        except Exception as exc:
            log.debug("阅读累计加载失败：%s", exc)
        return {}

    def _save_daily_read_seconds(self) -> None:
        """将累计持久化到 reading_time.json。"""
        try:
            data = {
                "daily": dict(self._daily_read_seconds),
                "saved_at": int(time.time()),
            }
            tmp = READING_TIME_PATH.parent / (READING_TIME_PATH.name + ".tmp")
            READING_TIME_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            _os.replace(str(tmp), str(READING_TIME_PATH))
        except Exception as exc:  # noqa: BLE001
            log.debug("阅读累计写盘失败：%s", exc)

    def add_read_seconds(self, seconds: int) -> None:
        """每次阅读成功后调用，累加当日秒数。"""
        if not isinstance(seconds, (int, float)) or seconds <= 0:
            return
        import datetime as _dt
        BEIJING = _dt.timezone(_dt.timedelta(hours=8))
        today = _dt.datetime.now(BEIJING).strftime("%Y-%m-%d")
        with self._daily_read_lock:
            self._daily_read_seconds[today] = self._daily_read_seconds.get(today, 0) + int(seconds)
        # 每 60 秒或累计 > 3600s 自动保存
        now_ts = int(time.time())
        if not hasattr(self, "_last_reading_time_save"):
            self._last_reading_time_save = 0
        delta = self._daily_read_seconds.get(today, 0)
        if now_ts - self._last_reading_time_save > 60 or delta > 3600:
            self._save_daily_read_seconds()
            self._last_reading_time_save = now_ts

    def get_local_reading_stats(self) -> dict:
        """从本地累计计算 4 项统计（今日/本周/本月/总累计）。"""
        import datetime as _dt
        BEIJING = _dt.timezone(_dt.timedelta(hours=8))
        now = _dt.datetime.now(BEIJING)
        today_str = now.strftime("%Y-%m-%d")

        with self._daily_read_lock:
            daily = dict(self._daily_read_seconds)

        # 今日
        today_seconds = daily.get(today_str, 0)

        # 本周（最近 7 天）
        week_seconds = 0
        for i in range(7):
            d = now - _dt.timedelta(days=i)
            key = d.strftime("%Y-%m-%d")
            week_seconds += daily.get(key, 0)

        # 本月（最近 30 天）
        month_seconds = 0
        for i in range(30):
            d = now - _dt.timedelta(days=i)
            key = d.strftime("%Y-%m-%d")
            month_seconds += daily.get(key, 0)

        # 总累计
        total_seconds = sum(daily.values())

        return {
            "today_seconds": today_seconds or None,
            "today_hm": self._format_hm(today_seconds or None),
            "week_seconds": week_seconds or None,
            "week_hm": self._format_hm(week_seconds or None),
            "month_seconds": month_seconds or None,
            "month_hm": self._format_hm(month_seconds or None),
            "total_seconds": total_seconds or None,
            "total_hm": self._format_hm(total_seconds or None),
            "source": "local_accumulated",
        }

    def refresh_web_chapters_for_book(self, book_id: str) -> bool:
        """走 POST /web/book/chapterInfos（web 原生）签名化拉章节，写入内存 + 立即落盘。
        返回 True 表示至少拿到 1 个章节；失败 / 空章节 → 返回 False（Fail-Fast），
        绝不写 DEFAULT_CHAPTERS。"""
        if not book_id:
            return False
        bid_norm = str(book_id).strip()
        try:
            self._augment_headers_baggage()
            # 1) 拿之前的 synckey
            prev_synckey = 0
            with self._book_lock:
                prev = self.scoped_chapters.get(bid_norm)
                if isinstance(prev, dict):
                    s = prev.get("synckey")
                    try:
                        prev_synckey = int(s) if s else 0
                    except (TypeError, ValueError):
                        prev_synckey = 0
            # 2) 构造 body + 签名（和 /web/book/read 完全相同的 sg + s 算法）
            now = int(time.time())
            ts = int(now * 1000) + random.randint(0, 999)
            rn = random.randint(0, 9999)
            app_id = "wb182564874603h266381671"
            req_body: dict[str, Any] = {
                "bookIds": [bid_norm],
                "appId": app_id,
                "synckey": prev_synckey,
                "onlyBookshelf": 0,
                "ts": ts,
                "rn": rn,
                "sg": hashlib.sha256(f"{ts}{rn}{KEY}".encode()).hexdigest(),
                "ct": now,
            }
            req_body["s"] = cal_hash(encode_data(req_body))
            headers = {
                "Content-Type": "application/json;charset=UTF-8",
                "Origin": "https://weread.qq.com",
                "Referer": "https://weread.qq.com/",
                "Accept": "application/json, text/plain, */*",
            }
            # 3) 发请求 + 失败重试 1 次
            def _post_once() -> tuple[int, dict | None, str]:
                try:
                    with self._lock:
                        resp = self._session.post(
                            CHAPTER_SYNC_URL,
                            headers=headers,
                            data=json.dumps(req_body, separators=(",", ":")),
                            timeout=(5, 15),
                        )
                    sc = resp.status_code
                    try:
                        dj = resp.json()
                        return sc, dj if isinstance(dj, dict) else {"_raw": dj}, ""
                    except ValueError:
                        return sc, None, resp.text[:400]
                except requests.RequestException as exc:
                    log.warning("refresh_web_chapters_for_book 请求异常（book=%s）：%s", bid_norm, exc)
                    return 0, None, str(exc)

            status, body, txt = _post_once()
            if status != 200:
                # 重试 1 次
                log.info("refresh_web_chapters_for_book HTTP=%s 非 200，再试 1 次（book=%s）", status, bid_norm)
                status, body, txt = _post_once()
            if status != 200 or not isinstance(body, dict):
                log.error(
                    "refresh_web_chapters_for_book 失败（Fail-Fast）：book=%s HTTP=%s body=%s",
                    bid_norm, status, (str(body) if body else txt)[:250],
                )
                return False
            # 4) 解析响应（兼容多种结构）
            log.debug("refresh_web_chapters_for_book 响应 keys=%s 前300字=%s",
                      list(body.keys())[:10], str(body)[:300])
            books_list: list[dict] = []
            # 4a) 结构 A：{"books": [ {bookId, chapters:[...]}, ... ], "synckey": x}
            if isinstance(body.get("books"), list):
                books_list = [b for b in body["books"] if isinstance(b, dict)]
            # 4b) 结构 B：{"data": [ {bookId, updated:[...] 或 chapters:[...]}, ... ]}
            if not books_list and isinstance(body.get("data"), list):
                for e in body["data"]:
                    if isinstance(e, dict):
                        # 支持 updated（增量/全量章节，synckey=0 时为全量）或 chapters
                        if "updated" in e or "chapters" in e:
                            books_list.append(e)
            # 4c) 单本书结构 B 退化：顶层直接有 chapters 或 updated
            if not books_list and isinstance(body.get("chapters"), list):
                books_list = [{"bookId": bid_norm, "chapters": body["chapters"]}]
            if not books_list and isinstance(body.get("updated"), list):
                books_list = [{"bookId": bid_norm, "updated": body["updated"]}]
            if not books_list:
                log.error(
                    "refresh_web_chapters_for_book books[0] 缺失（Fail-Fast）：book=%s keys=%s body前200=%s",
                    bid_norm, list(body.keys())[:10], str(body)[:200],
                )
                return False
            book_obj = books_list[0]
            # 4d) 章节列表可能在 chapters 或 updated 字段
            ch_raw = book_obj.get("chapters")
            if not isinstance(ch_raw, list) or len(ch_raw) == 0:
                ch_raw = book_obj.get("updated")
            if not isinstance(ch_raw, dict):
                pass  # 已经是 list
            elif isinstance(ch_raw, dict):
                ch_raw = list(ch_raw.values())
            if not isinstance(ch_raw, list) or len(ch_raw) == 0:
                log.error(
                    "refresh_web_chapters_for_book chapters 为空（Fail-Fast）：book=%s keys=%s body前200=%s",
                    bid_norm, list(book_obj.keys())[:8], str(body)[:200],
                )
                return False
            # 5) 规范化 + 按 chapterIdx 稳定排序 + 去重
            chapters_valid: list[dict] = []
            for ch in ch_raw:
                if not isinstance(ch, dict):
                    continue
                uid_raw = ch.get("chapterUid")
                if uid_raw is None:
                    continue
                try:
                    uid_i = int(uid_raw)
                except (TypeError, ValueError):
                    continue
                idx_raw = ch.get("chapterIdx")
                try:
                    idx_i = int(idx_raw) if idx_raw is not None else len(chapters_valid)
                except (TypeError, ValueError):
                    idx_i = len(chapters_valid)
                chapters_valid.append({
                    "chapterUid": uid_i,
                    "chapterIdx": idx_i,
                    "title": str(ch.get("title") or ""),
                    "wordCount": int(ch.get("wordCount") or 0) if str(ch.get("wordCount") or "").isdigit() else 0,
                    "level": int(ch.get("level") or 0) if str(ch.get("level") or "").isdigit() else 0,
                    "updateTime": int(ch.get("updateTime") or 0) if str(ch.get("updateTime") or "").isdigit() else 0,
                })
            if not chapters_valid:
                log.error("refresh_web_chapters_for_book 解析后章节为空（Fail-Fast）：book=%s", bid_norm)
                return False
            chapters_valid.sort(key=lambda d: (int(d.get("chapterIdx") or 0), int(d.get("chapterUid") or 0)))
            uids_uniq: list[int] = []
            _seen_uids: set[int] = set()
            for ch in chapters_valid:
                ui = int(ch["chapterUid"])
                if ui in _seen_uids:
                    continue
                _seen_uids.add(ui)
                uids_uniq.append(ui)
            # 6) 顶层 synckey / chapterUpdateTime（秒/毫秒统一转秒）
            def _to_sec(v: Any) -> int:
                if v is None:
                    return 0
                try:
                    x = int(v)
                except (TypeError, ValueError):
                    return 0
                if x > 10_000_000_000:  # 毫秒级
                    return x // 1000
                return max(0, x)

            synckey_out = _to_sec(body.get("synckey"))
            ch_update = _to_sec(body.get("chapterUpdateTime"))
            if ch_update == 0 and chapters_valid:
                max_ut = max((int(c.get("updateTime") or 0) for c in chapters_valid), default=0)
                ch_update = _to_sec(max_ut)
            # 7) 写内存
            bucket: dict[str, Any] = {
                "count": len(uids_uniq),
                "uids": [str(x) for x in uids_uniq],
                "chapters": chapters_valid,
                "synckey": synckey_out,
                "chapterUpdateTime": ch_update,
                "persisted_at": time.time(),
                "source": "web_chapterInfos",
            }
            with self._book_lock:
                self.scoped_chapters[bid_norm] = bucket
                # 兼容：同步到全局 self.chapters（给默认书兜底用的旧字段，不影响分桶逻辑）
                for uid in reversed([str(x) for x in uids_uniq]):
                    if uid in self.chapters:
                        self.chapters.remove(uid)
                    self.chapters.insert(0, uid)
                if len(self.chapters) > 80:
                    self.chapters = self.chapters[:80]
            # 8) 立即写盘
            self.save_chapter_cache(bid_norm)
            log.info(
                "refresh_web_chapters_for_book 成功：book=%s count=%d synckey=%s source=%s",
                bid_norm, len(uids_uniq), synckey_out, "web_chapterInfos",
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("refresh_web_chapters_for_book 异常（Fail-Fast）：book=%s err=%s", bid_norm, exc)
            return False

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

    # -------- 当前阅读中的书（统一真相源）--------
    def _emit_book_changed_locked(self) -> None:
        if self._current_book is None:
            return
        # 简化版：不再打印"current_book 变更"日志，直接 emit
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
        "interceptor_numeric",  # 拦截器捕获数字 bookId → 主动更新
    })

    # PATCH 合并时，source 会被一直拼接，导致 UI 里出现 1000+ 字符的超长 source 标签
    # （2026-08-22 用户截图里可见 "login_browser_nav+sync_book_info+..." 挤爆一行）。
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
        """设置当前书籍。

        当 JS 已捕获阅读数据时（_last_captured_read 存在），
        对于 login_browser_nav 等来源的空 book_id/chapter_id 会保留已有值。
        只有 manual/clear 等明确用户操作才能完全覆盖。
        """
        if payload is None:
            with self._book_lock:
                self._current_book = None
                self._book_progress_override = None
            return None
        if not isinstance(payload, dict):
            return self.current_book()

        # —— 规范化字段 ——
        book_id: str = str(payload.get("book_id") or "").strip()
        title = str(payload.get("title") or "").strip()
        reader_id = str(payload.get("book_reader_id") or payload.get("reader_id") or "").strip()
        url = str(payload.get("url") or "").strip()
        chapter_id = str(payload.get("chapter_id") or "").strip()
        author = str(payload.get("author") or "").strip()
        progress_raw = payload.get("progress")
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

        # 从 URL 解析兜底
        if (not book_id or not reader_id) and url:
            parsed = self._parse_reader_url(url)
            if parsed:
                reader_id = reader_id or parsed.get("reader_id") or ""
                book_id = book_id or parsed.get("book_id") or ""
                chapter_id = chapter_id or parsed.get("chapter_id") or ""

        # —— 智能合并：当 JS 已捕获数据且新值为空时，保留已有值 ——
        with self._book_lock:
            existing = self._current_book

        # JS 捕获锁生效时，login_browser_nav / shelf_booklist 等非用户明确操作来源一律不覆盖
        protected_sources = {"login_browser_nav", "shelf_booklist", "interceptor_numeric", "last_session_memory"}
        if self._js_capture_locked and source in protected_sources and existing:
            # 保护现有 book_id / chapter_id，仅补全其它字段（title / url 等）
            if not book_id:
                book_id = str(existing.get("book_id") or "").strip()
            if not chapter_id:
                chapter_id = str(existing.get("chapter_id") or "").strip()
            if not reader_id:
                reader_id = str(existing.get("book_reader_id") or "").strip()
            if not title:
                title = str(existing.get("title") or "").strip()
            if not url:
                url = str(existing.get("url") or "").strip()
            # 限频日志（每 30 秒最多一条）
            now_ts = int(time.time())
            last_log = getattr(self, "_js_protect_log_ts", 0)
            if now_ts - last_log >= 30 or last_log == 0:
                self._js_protect_log_ts = now_ts
                log.info(
                    "📚 set_current_book (JS 锁保护): source=%s, 保留已有 book_id=%s",
                    source, book_id[:16] if book_id else "(空)",
                )
        elif existing and self._last_captured_read:
            # JS 已捕获数据但锁未完全启用：保护 book_id / chapter_id 不被空值覆盖
            if not book_id:
                book_id = str(existing.get("book_id") or "").strip()
            if not chapter_id:
                chapter_id = str(existing.get("chapter_id") or "").strip()
            if not reader_id:
                reader_id = str(existing.get("book_reader_id") or "").strip()

        # 还没 url 就拼一个
        if not url and (reader_id or book_id):
            url = "https://weread.qq.com/web/reader/" + (reader_id or ("wb" + book_id))
            if chapter_id:
                url += f"?k{chapter_id}"

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
            self._current_book = final
            if progress is not None:
                self._book_progress_override = progress
            self._emit_book_changed_locked()
        log.info("📚 set_current_book: book_id=%s source=%s", book_id or "(空)", source)
        return dict(final)

    # -------- JS 捕获请求处理 --------
    def on_js_captured_request(self, url: str, payload: dict) -> None:
        """处理从浏览器 JS 劫持捕获的请求。

        主要处理：
        1. /web/book/read → 保存为模板，同时更新 _current_book
        2. /web/book/chapterInfos → 章节列表（请求体）
        3. /web/book/chapter/* → 提取章节 ID 列表
        """
        url_lower = url.lower()

        # 1) /web/book/read → 保存为模板，同时更新当前书籍 + 启用 JS 捕获锁
        if "/web/book/read" in url_lower:
            if isinstance(payload, dict) and payload.get("b") and payload.get("c"):
                self._last_captured_read = payload
                book_hex = str(payload.get("b", ""))
                chapter_hex = str(payload.get("c", ""))

                if book_hex:
                    # 尝试将 hex book_id 转为 numeric，存储映射
                    numeric_id = self.resolve_book_id_to_numeric(book_hex)
                    if numeric_id != book_hex:
                        self._hex_to_numeric_book_id[book_hex] = numeric_id

                    # 同时存储章节 ID（用 hex 和 numeric 两个 key）
                    if chapter_hex:
                        for key in [book_hex, numeric_id]:
                            if key and key not in self._captured_chapter_ids:
                                self._captured_chapter_ids[key] = []
                            if chapter_hex in self._captured_chapter_ids.get(key, []):
                                pass  # 已存在
                            elif key in self._captured_chapter_ids:
                                self._captured_chapter_ids[key].append(chapter_hex)

                    # 更新 _current_book（保留 title 等其他字段）+ 启用 JS 捕获锁
                    with self._book_lock:
                        cb = self._current_book
                        # 首次捕获或与现有书不同 → 强制以捕获为准
                        if cb is None:
                            cb_copy = {}
                        else:
                            cb_copy = dict(cb)
                        cb_copy["book_id"] = book_hex
                        cb_copy["chapter_id"] = chapter_hex
                        cb_copy["source"] = "js_captured"
                        cb_copy["updated_at"] = int(time.time())
                        if numeric_id != book_hex:
                            cb_copy["book_id_numeric"] = numeric_id
                        self._current_book = cb_copy
                        self._emit_book_changed_locked()

                    # 启用 JS 捕获锁（保护后续 set_current_book 不被覆盖）
                    self._js_capture_locked = True

                    log.info(
                        "📚 JS 捕获 read 并锁定书籍：book=%s(numeric=%s) chapter=%s, 共 %d 章",
                        book_hex[:20], numeric_id, chapter_hex[:20] if chapter_hex else "?",
                        len(self._captured_chapter_ids.get(book_hex, []))
                    )

        # 2) /web/book/chapterInfos → 解析请求体中的 bookIds 占位，并尝试从响应体（payload.data）
        #    提取 chapterUid 填充 _captured_chapter_ids（以实际 web 浏览器响应为准）
        elif "/web/book/chapterInfos" in url_lower:
            self._handle_chapterinfos_payload(payload)

        # 3) /web/book/chapter/e_* → 提取章节 ID
        elif "/web/book/chapter/" in url_lower:
            if isinstance(payload, dict) and payload.get("b") and payload.get("c"):
                book_hex = str(payload.get("b", ""))
                chapter_hex = str(payload.get("c", ""))

                if book_hex and chapter_hex:
                    # 同时存储到 hex 和 numeric key
                    numeric_id = self.resolve_book_id_to_numeric(book_hex)
                    keys_to_store = [book_hex]
                    if numeric_id != book_hex:
                        keys_to_store.append(numeric_id)
                        self._hex_to_numeric_book_id[book_hex] = numeric_id

                    for key in keys_to_store:
                        if key not in self._captured_chapter_ids:
                            self._captured_chapter_ids[key] = []
                        if chapter_hex not in self._captured_chapter_ids[key]:
                            self._captured_chapter_ids[key].append(chapter_hex)

                    log.debug(
                        "📚 JS 捕获章节: book=%s chapter=%s (共 %d 章)",
                        book_hex[:16], chapter_hex[:16],
                        len(self._captured_chapter_ids.get(book_hex, []))
                    )

    def get_captured_chapter_ids(self, book_id: str) -> list[str]:
        """获取指定书籍的已捕获章节 ID 列表（支持 hex/numeric 匹配）。"""
        bid = str(book_id or "").strip()
        if not bid:
            return []

        # 直接匹配
        if bid in self._captured_chapter_ids:
            return list(self._captured_chapter_ids[bid])

        # 通过映射查找
        numeric = self.resolve_book_id_to_numeric(bid)
        if numeric in self._captured_chapter_ids:
            return list(self._captured_chapter_ids[numeric])

        # 反向查找：如果 bid 是 numeric，找对应的 hex
        for hex_id, num_id in self._hex_to_numeric_book_id.items():
            if num_id == bid and hex_id in self._captured_chapter_ids:
                return list(self._captured_chapter_ids[hex_id])

        # 模糊匹配（包含关系）
        for key, chapters in self._captured_chapter_ids.items():
            if bid in key or key in bid:
                return list(chapters)

        return []

    def _handle_chapterinfos_payload(self, payload: Any) -> None:
        """处理 /web/book/chapterInfos 的请求体或响应体，提取 chapterUid。

        JS 注入捕获的通常是请求体 `{"bookIds": ["35821223"]}`，
        但如果未来捕获到响应体 `{"data": [{"bookId":..., "updated": [{"chapterUid":..., ...}]}]}`，
        也一并解析并填充 `_captured_chapter_ids` 和 `scoped_chapters`。
        """
        if not isinstance(payload, dict):
            return
        # 情况 A：请求体 —— 只记录一下，无章节可提取
        if "bookIds" in payload:
            log.info("JS 捕获 chapterInfos 请求，bookIds=%s（等待响应体以提取 chapterUid）",
                     payload.get("bookIds"))
            return
        # 情况 B：响应体 —— 提取 updated[*].chapterUid
        data = payload.get("data")
        if not isinstance(data, list):
            log.debug("JS 捕获 chapterInfos 响应但无 data 字段：keys=%s", list(payload.keys()))
            return
        extracted_any = False
        for item in data:
            if not isinstance(item, dict):
                continue
            bid = str(item.get("bookId") or "").strip()
            if not bid:
                continue
            # 从 updated 列表提取 chapterUid（web 端新增章节列表）
            updated = item.get("updated") or []
            chapters: list[str] = []
            if isinstance(updated, list):
                for ch in updated:
                    if isinstance(ch, dict):
                        cu = str(ch.get("chapterUid") or "").strip()
                        if cu:
                            chapters.append(cu)
            # 也兼容旧字段 chapters
            if not chapters:
                old_chapters = item.get("chapters") or []
                if isinstance(old_chapters, list):
                    for ch in old_chapters:
                        if isinstance(ch, dict):
                            cu = str(ch.get("chapterUid") or "").strip()
                            if cu:
                                chapters.append(cu)
            if not chapters:
                continue
            extracted_any = True
            # 填充 _captured_chapter_ids（用 bid 本身作为 key）
            if bid not in self._captured_chapter_ids:
                self._captured_chapter_ids[bid] = []
            for c in chapters:
                if c not in self._captured_chapter_ids[bid]:
                    self._captured_chapter_ids[bid].append(c)
            # 若 bid 是 numeric，同步到所有已知 hex 映射
            if bid.isdigit():
                for h, n in list(self._hex_to_numeric_book_id.items()):
                    if n == bid:
                        if h not in self._captured_chapter_ids:
                            self._captured_chapter_ids[h] = []
                        for c in chapters:
                            if c not in self._captured_chapter_ids[h]:
                                self._captured_chapter_ids[h].append(c)
            # 同时写入 scoped_chapters，便于持久化
            with self._book_lock:
                bucket = self.scoped_chapters.get(bid)
                if not isinstance(bucket, dict):
                    bucket = {}
                old_uids = bucket.get("uids")
                if not isinstance(old_uids, list):
                    old_uids = []
                merged = list(old_uids)
                for c in chapters:
                    if c not in merged:
                        merged.append(c)
                bucket["uids"] = merged
                bucket["count"] = len(merged)
                bucket["chapters"] = updated if isinstance(updated, list) else bucket.get("chapters", [])
                self.scoped_chapters[bid] = bucket
            log.info("JS 解析 chapterInfos 响应：book=%s 提取 %d 章（累计 %d 章）",
                     bid, len(chapters), len(merged))
        if not extracted_any:
            log.debug("JS 捕获 chapterInfos 响应但无有效 chapterUid：%s", str(payload)[:200])

    def get_captured_read_template(self) -> dict | None:
        """获取最近一次捕获的 read 请求模板。"""
        return self._last_captured_read.copy() if self._last_captured_read else None

    def resolve_book_id_to_numeric(self, book_id: str) -> str:
        """将任意 book_id（hex 或 numeric）解析为 numeric。

        策略：
          1) 如果已经是纯数字 → 直接返回
          2) 如果在 _hex_to_numeric_book_id 映射中 → 返回映射值
          3) 否则 → 返回原值（可能是 hex，调用方需要处理）
        """
        bid = str(book_id or "").strip()
        if not bid:
            return ""
        if bid.isdigit():
            return bid
        numeric = self._hex_to_numeric_book_id.get(bid)
        if numeric and str(numeric).strip().isdigit():
            return str(numeric).strip()
        return bid

    def register_book_id_mapping(self, hex_id: str, numeric_id: str) -> None:
        """注册 hex → numeric bookId 映射（由拦截器调用）。"""
        h = str(hex_id or "").strip()
        n = str(numeric_id or "").strip()
        if h and n and n.isdigit():
            self._hex_to_numeric_book_id[h] = n
            log.debug("注册 bookId 映射：hex=%s → numeric=%s", h[:16] + "..." if len(h) > 16 else h, n)

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

        方案A（参考 findmover/wxread）：
        - 默认走三体固定池：b = DEFAULT_BOOKS[12]（三体）, c = random.choice(DEFAULT_CHAPTERS)
        - 当 JS 捕获到完整 /web/book/read 模板（_last_captured_read 非空，含 b 和 c）
          且有足够章节池（>=3 章）时，走 JS 捕获路径（真实书+真实章节）
        - 否则一律走三体固定池（不校验 b 和 c 的从属关系，服务端只验签名）

        删除的旧逻辑：scoped_chapters / refresh_chapters_for_book / fail-fast。
        服务端验证规则：只要 sg = SHA256(ts+rn+KEY) 和 s = cal_hash(encode_data(data)) 正确即可。
        """
        # ===== 路径 1：JS 捕获到完整模板 + 章节池充足 → 用真实数据 =====
        captured_template = self._last_captured_read
        template_book_id = str(captured_template.get("b", "")).strip() if captured_template else ""
        captured_chapters = self.get_captured_chapter_ids(template_book_id) if template_book_id else []

        if captured_template and template_book_id and len(captured_chapters) >= 1:
            log.info(
                "_build_payload: 走 JS 捕获路径（book=%s, %d 章）",
                template_book_id[:20], len(captured_chapters)
            )
            last_c = str(captured_template.get("c", "")).strip()
            available = [c for c in captured_chapters if c != last_c]
            if not available:
                available = captured_chapters
            new_c = random.choice(available)

            now = int(time.time())
            ts = int(now * 1000) + random.randint(0, 999)
            rn = random.randint(0, 9999)
            rt = 30 if last_time is None else max(20, min(90, now - last_time))

            data: dict[str, Any] = {
                "appId": captured_template.get("appId", DEFAULT_APP_ID),
                "b": template_book_id,
                "c": new_c,
                "ci": captured_template.get("ci", random.randint(1, 200)),
                "co": captured_template.get("co", random.randint(100, 800)),
                "sm": captured_template.get("sm", random.choice(DEFAULT_SM_SNIPPETS)),
                "pr": captured_template.get("pr", random.randint(1, 200)),
                "rt": rt,
                "ts": ts,
                "rn": rn,
                "sg": hashlib.sha256(f"{ts}{rn}{KEY}".encode()).hexdigest(),
                "ct": now,
                "ps": captured_template.get("ps", DEFAULT_PS),
                "pc": captured_template.get("pc", DEFAULT_PC),
            }
            data["s"] = cal_hash(encode_data(data))
            self._last_read_ts = int(now)
            return data

        # ===== 路径 2：默认走三体固定池（参考 findmover/wxread main.py）=====
        # 服务端不校验 b 和 c 的从属关系，只校验签名 s 和 sg 是否正确。
        # b 固定为三体，c 从 DEFAULT_CHAPTERS（三体章节池）随机选取。
        if captured_template and not template_book_id:
            log.debug("_build_payload: 有捕获模板但无 b 字段，走三体固定池")
        elif captured_template and len(captured_chapters) < 1:
            log.info(
                "_build_payload: JS 捕获章节为空，走三体固定池（book=%s）",
                template_book_id[:20] if template_book_id else "(空)"
            )
        else:
            log.debug("_build_payload: 无 JS 捕获数据，走三体固定池")

        now = int(time.time())
        ts = int(now * 1000) + random.randint(0, 999)
        rn = random.randint(0, 9999)
        rt = 30 if last_time is None else max(20, min(90, now - last_time))

        # 若有捕获模板，复用其 appId/ps/pc/sm/ci/co/pr（仍是真实书数据，但 b/c 用三体固定池）
        # 这样比纯随机更接近真实阅读模式
        app_id = captured_template.get("appId", DEFAULT_APP_ID) if captured_template else DEFAULT_APP_ID
        ps = captured_template.get("ps", DEFAULT_PS) if captured_template else DEFAULT_PS
        pc = captured_template.get("pc", DEFAULT_PC) if captured_template else DEFAULT_PC
        sm = captured_template.get("sm", random.choice(DEFAULT_SM_SNIPPETS)) if captured_template else random.choice(DEFAULT_SM_SNIPPETS)
        ci = captured_template.get("ci", random.randint(1, 200)) if captured_template else random.randint(1, 200)
        co = captured_template.get("co", random.randint(100, 800)) if captured_template else random.randint(100, 800)
        pr = captured_template.get("pr", random.randint(1, 200)) if captured_template else random.randint(1, 200)

        data: dict[str, Any] = {
            "appId": app_id,
            "b": DEFAULT_BOOK_ID,  # 三体
            "c": random.choice(DEFAULT_CHAPTERS),  # 三体章节
            "ci": ci,
            "co": co,
            "sm": sm,
            "pr": pr,
            "rt": rt,
            "ts": ts,
            "rn": rn,
            "sg": hashlib.sha256(f"{ts}{rn}{KEY}".encode()).hexdigest(),
            "ct": now,
            "ps": ps,
            "pc": pc,
        }
        data["s"] = cal_hash(encode_data(data))
        self._last_read_ts = int(now)
        return data

    # -------- 对外：单次阅读上报 --------
    def read_once(self, *, last_time: int | None = None) -> bool:
        """执行一次 /web/book/read。返回 True 代表成功（succ=1）。

        额外副作用：每次调用后更新 self._last_read_empty_body（布尔），
        供 scheduler 判断"连续 2 次 200 空 body 触发章节重刷"。
        """
        # 每次调用先重置空 body 标记
        self._last_read_empty_body = False
        if not self.ensure_session():
            return False
        self._augment_headers_baggage()
        try:
            payload = self._build_payload(last_time=last_time)
        except RuntimeError as exc:
            # fail-fast：章节刷新失败，不发请求
            self.warning.emit(str(exc))
            self.error.emit(str(exc))
            log.warning("read_once _build_payload fail-fast: %s", exc)
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("read_once _build_payload 异常：%s", exc)
            self.warning.emit(f"read_once payload 构造异常：{exc}")
            return False
        json_ct = {"Content-Type": "application/json;charset=UTF-8"}

        def _is_empty(sc: int, bd: dict | None, tx: str) -> bool:
            """判断是否命中 HTTP 200 空 body（用于 scheduler 计数）。"""
            if sc != 200:
                return False
            if isinstance(bd, dict) and len(bd) == 0:
                return True
            if bd is None:
                stripped = (tx or "").strip()
                if stripped in ("", "{}", "null", "[]"):
                    return True
            return False

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
        if _is_empty(status, body, txt):
            log.warning(
                "read 返回空 body：status=%s payload(b=%s c=%s source=%s url=%s)",
                status,
                str(payload.get("b"))[:40],
                str(payload.get("c"))[:40],
                str((self._current_book or {}).get("source") or ""),
                str((self._current_book or {}).get("url") or "")[:200],
            )
            # 方案A：空 body 时直接用三体固定池重试（不调用 refresh_chapters_for_book）
            self.warning.emit("⚠️ read 返回空，用三体固定池重试...")
            retry_payload = dict(payload)
            # 强制用三体固定池
            retry_payload["b"] = DEFAULT_BOOK_ID
            retry_payload["c"] = random.choice(DEFAULT_CHAPTERS)
            retry_payload["ps"] = DEFAULT_PS
            retry_payload["pc"] = DEFAULT_PC
            retry_payload["appId"] = DEFAULT_APP_ID
            # 重新生成签名
            now_i = int(time.time())
            retry_payload["ts"] = int(now_i * 1000) + random.randint(0, 999)
            retry_payload["rn"] = random.randint(0, 9999)
            retry_payload["ct"] = now_i
            retry_payload["rt"] = 30
            retry_payload["sg"] = hashlib.sha256(
                f"{retry_payload['ts']}{retry_payload['rn']}{KEY}".encode()
            ).hexdigest()
            retry_payload["s"] = cal_hash(encode_data(retry_payload))
            status2, body2, _txt2 = _do_post(retry_payload)
            # 只有重试 body 非空时才覆盖；否则保留空状态让 scheduler 计数
            if isinstance(body2, dict) and body2:
                status, body, txt = status2, body2, _txt2
            elif not _is_empty(status2, body2, _txt2):
                status, body, txt = status2, body2, _txt2

        # 在返回之前：设置 _last_read_empty_body（反映最终结果）
        if _is_empty(status, body, txt):
            self._last_read_empty_body = True

        if isinstance(body, dict) and body.get("succ") == 1 and "synckey" in body:
            log.info("read 成功：succ=1 synckey=%s...", str(body.get("synckey"))[:16])
            # ★ 本地累计：每次成功 read 累加阅读时长（默认 1 次 ≈ 45 秒）
            self.add_read_seconds(45)
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

    def refresh_chapters_for_book(self, book_id: str | int, *, timeout: int = 12) -> bool:
        """对外统一入口：封装 refresh_web_chapters_for_book + 30 天兜底检测。
        30 天兜底：如果内存里已有该书但 persisted_at > 30 天，强制重拉。
        """
        del timeout  # 保留签名兼容；实际内部超时由 refresh_web_chapters_for_book 自行控制
        if not book_id:
            return False
        bid_raw = str(book_id).strip()
        # ★ 关键：hex→numeric 解析（如果有映射就用映射值）
        bid = self.resolve_book_id_to_numeric(bid_raw)
        if bid != bid_raw:
            log.info("refresh_chapters_for_book: hex→numeric 解析 %s → %s", bid_raw[:16] + "...", bid)
        # 30 天兜底：如果内存里已有该书但 persisted_at > 30 天，强制重拉
        existing = self.scoped_chapters.get(bid) if isinstance(self.scoped_chapters, dict) else None
        if isinstance(existing, dict):
            pa = existing.get("persisted_at") or 0
            try:
                pa_f = float(pa) if pa else 0.0
            except (TypeError, ValueError):
                pa_f = 0.0
            if pa_f and (time.time() - pa_f) > 30 * 86400:
                log.info("书籍 %s 章节缓存超过 30 天（persisted_at=%s），强制重拉", bid, pa)
                self.scoped_chapters.pop(bid, None)
        return self.refresh_web_chapters_for_book(bid)

    # -------- 官方阅读统计（每日累计时长） --------
    @staticmethod
    def _format_hm(total_seconds: int | None) -> str:
        """秒 → "X 小时 Y 分钟"中文格式。0 秒显示"0 分钟"；None 返回空串占位 "-"。"""
        if total_seconds is None:
            return "-"
        sec = max(0, int(total_seconds))
        h, rem = divmod(sec, 3600)
        m, _ = divmod(rem, 60)
        if h <= 0:
            return f"{m} 分钟"
        return f"{h} 小时 {m:02d} 分钟"

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
        """拉取 4 项阅读统计（今日/本周/本月/总累计）。

        策略：调微信读书官方 Skill 网关（/api/agent/gateway /readdata/detail）。
        需要 config.skill.api_key（wrk-xxxxxxxx）。
        - 无 API Key → 返回空数据，UI 显示 "—"
        - 调用失败 → 返回空数据，UI 显示 "—"
        - 成功 → 3 次 mode=overall/weekly/monthly 请求，提取对应字段
        """
        api_key = str(self._cfg.get("skill.api_key") or "").strip()
        if not api_key:
            log.warning("Skill 阅读统计：未配置 api_key，UI 显示 —（申请地址：https://weread.qq.com/r/weread-skills）")
            result = self._empty_summary()
            self.reading_summary.emit(result)
            return result

        # 检查缓存
        cache_ttl = int(self._cfg.get("skill.summary_cache_ttl") or 180)
        cached = self._load_skill_summary_cache()
        if cached and (int(time.time()) - int(cached.get("fetched_at", 0)) < cache_ttl):
            log.info("Skill 阅读统计命中缓存（TTL=%ss，fetched_at=%s）",
                     cache_ttl, cached.get("fetched_at"))
            cached["source"] = "skill_cache"
            self.reading_summary.emit(cached)
            return cached

        # 调 Skill 网关
        try:
            result = self._fetch_skill_reading_summary(api_key, timeout)
            if result:
                result["source"] = "skill_gateway"
                self._cache_skill_summary(result)
                self.reading_summary.emit(result)
                log.info(
                    "reading_summary 命中：source=skill_gateway today=%s week=%s month=%s total=%s",
                    result.get("today_hm"), result.get("week_hm"),
                    result.get("month_hm"), result.get("total_hm"),
                )
                return result
        except Exception as exc:  # noqa: BLE001
            log.warning("Skill 网关调用失败：%s", exc)

        # 失败兜底：返回空数据
        log.warning("Skill 阅读统计：调用失败，UI 显示 —")
        result = self._empty_summary()
        self.reading_summary.emit(result)
        return result

    @staticmethod
    def _empty_summary() -> dict:
        """返回空统计结果，UI 全显示 "—"。"""
        return {
            "today_seconds": None,
            "today_hm": "-",
            "week_seconds": None,
            "week_hm": "-",
            "month_seconds": None,
            "month_hm": "-",
            "total_seconds": None,
            "total_hm": "-",
            "fetched_at": int(time.time()),
            "source": "skill_failed",
        }

    def _fetch_skill_reading_summary(self, api_key: str, timeout: int) -> dict | None:
        """3 次 /readdata/detail 请求 + 700ms 节流，按 readTimes 分桶累加提取 4 项。

        API 实际返回结构（直接返回，无 data 包装）：
          overall → {readTimes: {year_ts: seconds, ...}, readDays, readLongest}
          weekly  → {readTimes: {day_ts: seconds, ...}, readDays, readLongest}
          monthly → {readTimes: {day_ts: seconds, ...}, readDays, readLongest}

        计算方式：
          总累计 = sum(overall.readTimes.values())
          本周   = sum(weekly.readTimes.values())
          本月   = sum(monthly.readTimes.values())
          今日   = weekly.readTimes[今日CST0点ts] 或 monthly.readTimes[今日CST0点ts]
        """
        import datetime as _dt
        BEIJING = _dt.timezone(_dt.timedelta(hours=8))
        now = _dt.datetime.now(BEIJING)
        today_bj_00 = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

        skill_version = str(self._cfg.get("skill.version") or "1.0.4")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # 第 1 次：overall（总累计，按年份分桶）
        overall_data = self._call_skill_readdata(headers, skill_version, "overall", timeout)
        time.sleep(0.7)
        # 第 2 次：weekly（本周，按天分桶）
        weekly_data = self._call_skill_readdata(headers, skill_version, "weekly", timeout)
        time.sleep(0.7)
        # 第 3 次：monthly（本月，按天分桶）
        monthly_data = self._call_skill_readdata(headers, skill_version, "monthly", timeout)

        # 解析 readTimes 分桶
        overall_buckets = self._sum_readTimes(overall_data)
        weekly_buckets = self._sum_readTimes(weekly_data)
        monthly_buckets = self._sum_readTimes(monthly_data)

        log.info(
            "Skill 原始数据：overall=%d (桶数=%d) weekly=%d (桶数=%d) monthly=%d (桶数=%d)",
            overall_buckets["total"], overall_buckets["count"],
            weekly_buckets["total"], weekly_buckets["count"],
            monthly_buckets["total"], monthly_buckets["count"],
        )

        # 提取 today_seconds（P1: weekly，P2: monthly）
        today_sec = self._extract_today(weekly_data, today_bj_00)
        if today_sec is None:
            today_sec = self._extract_today(monthly_data, today_bj_00)

        week_sec = weekly_buckets["total"] if weekly_buckets["count"] > 0 else None
        month_sec = monthly_buckets["total"] if monthly_buckets["count"] > 0 else None
        total_sec = overall_buckets["total"] if overall_buckets["count"] > 0 else None

        # 合理性校验
        week_sec = self._validate_range(week_sec, 0, 604800)  # 7d 上限
        month_sec = self._validate_range(month_sec, 0, 2678400)  # 31d 上限

        return {
            "today_seconds": today_sec,
            "today_hm": self._format_hm(today_sec),
            "week_seconds": week_sec,
            "week_hm": self._format_hm(week_sec),
            "month_seconds": month_sec,
            "month_hm": self._format_hm(month_sec),
            "total_seconds": total_sec,
            "total_hm": self._format_hm(total_sec),
            "fetched_at": int(time.time()),
        }

    @staticmethod
    def _sum_readTimes(data: dict) -> dict:
        """累加 readTimes 分桶值，返回 {total, count}。"""
        if not isinstance(data, dict):
            return {"total": 0, "count": 0}
        read_times = data.get("readTimes")
        if not isinstance(read_times, dict):
            return {"total": 0, "count": 0}
        total = 0
        count = 0
        for v in read_times.values():
            iv = int(v) if isinstance(v, (int, float)) else 0
            total += iv
            count += 1
        return {"total": total, "count": count}

    @staticmethod
    def _extract_today(data: dict, today_bj_00: int) -> int | None:
        """从 readTimes 分桶中提取今日 0 点 CST 时间戳对应的值。"""
        if not isinstance(data, dict):
            return None
        read_times = data.get("readTimes")
        if not isinstance(read_times, dict):
            return None
        # key 可能是字符串或整数
        for key in (str(today_bj_00), today_bj_00):
            if key in read_times:
                v = read_times[key]
                if isinstance(v, (int, float)):
                    return int(v)
        return None

    def _call_skill_readdata(
        self, headers: dict, skill_version: str, mode: str, timeout: int
    ) -> dict:
        """单次 /readdata/detail 调用，含 499 重试 1 次。"""
        payload = {
            "api_name": "/readdata/detail",
            "skill_version": skill_version,
            "mode": mode,
            "baseTime": 0,
        }
        for attempt in range(2):
            try:
                r = self._session.post(SKILL_GATEWAY_URL, headers=headers, json=payload, timeout=timeout)
                if r.status_code == 499 and attempt == 0:
                    log.warning("Skill %s 触发 499 限流，800ms 后重试 1 次", mode)
                    time.sleep(0.8)
                    continue
                r.raise_for_status()
                data = r.json()
                if isinstance(data, dict) and data.get("errcode", 0) != 0:
                    log.warning("Skill %s 调用失败：errcode=%s msg=%s",
                                mode, data.get("errcode"), data.get("errmsg"))
                    return {}
                # upgrade_info 检查
                if isinstance(data, dict) and "upgrade_info" in data:
                    log.warning("⚠️ Skill 有新版：%s", data.get("upgrade_info"))
                # 注意：/readdata/detail 接口直接返回数据，不是 {"data": {...}} 格式
                # 即 data 本身就是 {readTimes, readDays, readLongest}
                return data if isinstance(data, dict) else {}
            except Exception as exc:  # noqa: BLE001
                log.warning("Skill %s 请求异常（attempt=%d）：%s", mode, attempt, exc)
                if attempt == 0:
                    time.sleep(0.8)
                    continue
                return {}
        return {}

    @staticmethod
    def _validate_range(value: int | None, lo: int, hi: int) -> int | None:
        """合理性校验：超出 [lo, hi] 范围返回 None。"""
        if value is None:
            return None
        if value < lo or value > hi:
            log.warning("Skill 数据合理性校验失败：value=%s 不在 [%s, %s]", value, lo, hi)
            return None
        return value

    def _load_skill_summary_cache(self) -> dict | None:
        """读 wxread-skill-cache.json 的 last_summary 字段。

        有效性校验：4 项全为 None / "-" 的缓存视为失效（坏缓存）。
        """
        try:
            if SKILL_CACHE_PATH.exists():
                with open(SKILL_CACHE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    return None
                summary = data.get("last_summary")
                if not isinstance(summary, dict):
                    return None
                # 有效性校验：4 项全为 None / "-" → 视为坏缓存
                if self._is_empty_summary(summary):
                    log.info("Skill 缓存为空数据，视为失效，跳过")
                    return None
                return summary
        except Exception as exc:  # noqa: BLE001
            log.warning("读 Skill 缓存失败：%s", exc)
        return None

    @staticmethod
    def _is_empty_summary(summary: dict) -> bool:
        """判断 summary 是否为全空数据（4 项均为 None 或 "-"）。"""
        keys = ["today_seconds", "week_seconds", "month_seconds", "total_seconds"]
        for k in keys:
            v = summary.get(k)
            if v is not None and v != "-":
                return False
        return True

    def _cache_skill_summary(self, result: dict) -> None:
        """写缓存到 wxread-skill-cache.json 的 last_summary 字段。"""
        try:
            existing: dict = {}
            if SKILL_CACHE_PATH.exists():
                try:
                    with open(SKILL_CACHE_PATH, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                        if isinstance(loaded, dict):
                            existing = loaded
                except Exception:  # noqa: BLE001
                    pass
            existing["last_summary"] = result
            SKILL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(SKILL_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            log.warning("写 Skill 缓存失败：%s", exc)

    def verify_skill_api_key(self, api_key: str, timeout: int = 8) -> tuple[bool, str]:
        """验证 API Key 是否有效（供 settings_page 验证按钮调用）。

        Returns:
            (ok, msg) — ok=True 表示验证通过；msg 是中文说明。
        """
        api_key = (api_key or "").strip()
        if not api_key:
            return False, "API Key 不能为空"
        if not api_key.startswith("wrk-"):
            return False, "API Key 格式错误，应以 wrk- 开头"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        skill_version = str(self._cfg.get("skill.version") or "1.0.4")
        try:
            r = self._session.post(
                SKILL_GATEWAY_URL,
                headers=headers,
                json={
                    "api_name": "/readdata/detail",
                    "skill_version": skill_version,
                    "mode": "overall",
                    "baseTime": 0,
                },
                timeout=timeout,
            )
            if r.status_code == 499:
                return False, "触发限流（499），请稍后再试"
            if r.status_code == 401 or r.status_code == 403:
                return False, f"鉴权失败（HTTP {r.status_code}），API Key 无效或已过期"
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict):
                return False, "返回格式异常"
            errcode = data.get("errcode", 0)
            if errcode != 0:
                return False, f"接口错误：errcode={errcode} msg={data.get('errmsg')}"
            # 验证成功 → 清空旧缓存，确保下次刷新拉取真实数据
            self._clear_skill_summary_cache()
            # /readdata/detail 直接返回数据，不是 {"data": {...}} 格式
            inner = data if isinstance(data, dict) else {}
            buckets = self._sum_readTimes(inner)
            total_reading = buckets["total"]
            if total_reading > 0:
                return True, f"验证通过 ✅ 账号累计阅读：{self._format_hm(total_reading)}"
            return True, "验证通过 ✅"
        except Exception as exc:  # noqa: BLE001
            return False, f"请求异常：{exc}"

    def _clear_skill_summary_cache(self) -> None:
        """清空 Skill 阅读统计缓存（last_summary）。"""
        try:
            if not SKILL_CACHE_PATH.exists():
                return
            with open(SKILL_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.pop("last_summary", None)
                with open(SKILL_CACHE_PATH, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                log.info("已清空 Skill 阅读统计缓存")
        except Exception as exc:  # noqa: BLE001
            log.warning("清空 Skill 缓存失败：%s", exc)
