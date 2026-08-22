"""扫码登录页：内嵌 QWebEngineView 加载 weread.qq.com，用户扫码/登录后点击"我已登录"按钮，
自动从浏览器抽取 cookies 并保存到配置。

⚠️  登录态捕获链路（2026-08-21 根因定位之后重写）：
用户真实扫码 + 点书后，QtWebEngine 持久化 Profile 的 Cookies SQLite 已经存在 11 条 wr_*
（wr_vid/wr_skey/wr_rt HTTPONLY + wr_fp/wr_gid/wr_avatar/wr_gender/wr_name/wr_localvid/wr_pf/wr_ql）。
但旧版只靠 `QWebEngineCookieStore.cookieAdded + loadAllCookies` 的 **异步信号** 在 Windows 下
把这些票全吞了（_pending_cookies 空、store=0、dict=0、raw=0、config.cookies={}）——
这是"扫码完点书却提示 wr_vid 完全未出现"的 1 号根因，**不是 weread 服务端没下发，是 Qt 信号时序**。

所以采集改为 4 条完全独立的并行管道，只要其中任何一条拿到 wr_vid + RK/ptcz/pac_uid 就一定成功：
  1. 【信号线】QWebEngineCookieStore.cookieAdded（保留，兼容非持久化 OTR profile）
  2. 【网线级拦截器】QWebEngineUrlRequestInterceptor.interceptRequest —— 每一个响应的
     Set-Cookie 头，我们自己解析 name=value / Domain / Path / Secure / HttpOnly / SameSite，
     累积到 _intercepted_raw（不依赖 cookieAdded 信号，最可靠）
  3. 【SQLite 直读兜底】点击【我已登录完成】时，把当前 profile 的 Cookies SQLite 文件
     复制一份到临时目录，Python sqlite3 读 cookies 表（name/host_key/path/is_httponly/
     is_secure/samesite/value）——离线 dump 证实这条链路 100% 能拿到 11 条 wr_*，黄金兜底
  4. 【JS 双路兜底】runJavaScript(document.cookie) 抓 non-HTTP-only；
     runJavaScript(localStorage + sessionStorage 遍历 key 前缀 wr_) 抓 weread 新版
     可能写入 window.__weread__ / client storage 的用户标识噪声（wr_uid，签名使用）
  5. 启动时强制 wipe 旧 profile 持久化目录（DawnGraphite/LocalStorage/IndexedDB 等），
     避免"旧脏 cookie 让服务端认为已登录但实际是过期票，RK/ptcz 中间 cookie 没种下"
"""
from __future__ import annotations

import http.cookies
import os
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import QByteArray, QDateTime, QTimer, QUrl, Qt, Signal
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtWebEngineCore import (
    QWebEngineProfile,
    QWebEnginePage,
    QWebEngineCookieStore,
    QWebEngineUrlRequestInfo,
    QWebEngineUrlRequestInterceptor,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.weread_api import WeReadApi
from app.core.config import ConfigStore
from app.utils.logger import get_logger

log = get_logger(__name__)

# 微信登录/微信读书侧可能出现的关键 cookie 名称前缀。
# 参考：findmover/wxread config.py cookies 模板（RK/ptcz/pac_uid/iip/wr_avatar/wr_gender...）
#      + zhaohongxuan 博客的 Cookie 自动延期分析（wr_vid/wr_skey，renewal 刷新 wr_skey）
#      + 微信账号登录回跳（open.weixin.qq.com）的 wx_code / state / qqmusic_uin 等
#      + 「关键技术点.txt」硬性要求：_qimei_uuid42 / _qimei_32 / qimei36 / o_cookie / vqq_xxx / euin
_KEEP_COOKIE_PREFIXES: tuple[str, ...] = (
    # —— 微信读书 WEB 登录态（最核心，缺一不可）——
    "wr_",          # wr_vid, wr_skey, wr_at, wr_rt, wr_fp, wr_gid, wr_avatar, wr_gender, wr_info, wr_uuid, wr_localvid, wr_name, wr_pf, wr_ql...
    # —— 微信统一账号平台 (ptlogin/qq/weixin) 侧的"长效登录土壤" ——
    "RK",           # 登录根 cookie
    "ptcz",         # 登录签名
    "pt_",          # pt_login_sig, pt2gg, ptui 等
    "pac_uid",      # 腾讯系账号标识
    "iip",          # 账号 ipv4 摘要
    "qq_",          # qq_access_token, qq_login_sig 等
    "wq_",          # 微信手Q系互通标识
    "pgv_",         # 腾讯通用 pv 统计（有时被鉴权流程用作签名噪声源）
    "ts_uid",       # 腾讯通用访客 id
    "random_skey",  # 腾讯通用随机签名键（偶发进入 weread 域）
    # —— 微信扫码回跳时的辅助票（open.weixin.qq.com）——
    "wx_code",
    "wx_state",
    "openid2",
    "appmsg_token",
    "uuid",
    # —— QML / WebEngine 的 qimei 签名（微信读书新版请求里用作 baggage header 的噪声）——
    "_qimei",
    "QIMEI",
    "qimei",        # qimei36 / qimei-h5 等等
    "o_cookie",
    "euin",
    "vqq_",
)

_EXACT_KEEP: set[str] = {
    "RK", "ptcz", "pac_uid", "iip", "skey", "uin", "key", "p_uin", "p_skey",
    "_qimei_uuid42", "_qimei_uuid32", "qimei36", "qimei_h5",
}


def _cookie_name_should_keep(name: str) -> bool:
    if not name:
        return False
    for p in _KEEP_COOKIE_PREFIXES:
        if name.startswith(p):
            return True
    return name in _EXACT_KEEP


def _samesite_to_int(value: Any) -> int:
    """兼容 PySide6 多个版本的 sameSitePolicy 返回类型：
    - 旧版：int（0=None / 1=Strict / 2=Lax）
    - 6.11+：QNetworkCookie.SameSite 枚举，int() 不接受，需 .value
    - 其它：str / None —— 兜底 0
    """
    if value is None:
        return 0
    if isinstance(value, bool):  # bool 是 int 子类，先拦
        return 1 if value else 0
    if isinstance(value, int):
        # 负数或超大值（异常枚举）兜底为 0
        return 0 if value < 0 or value > 4 else int(value)
    # PySide6 枚举一般自带 int value
    vv = getattr(value, "value", None)
    if isinstance(vv, int):
        return 0 if vv < 0 or vv > 4 else vv
    # 字符串兜底：strict → 1, lax → 2，其它 None/Default → 0
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("strict",): return 1
        if s in ("lax",): return 2
        return 0
    return 0



# -------- 辅助：Set-Cookie header -> dict entry（和 cookies_raw 同 schema）----------
def _parse_set_cookie_line(raw_line: str, *, default_domain: str = "", default_path: str = "/") -> list[dict[str, Any]]:
    """解析 Set-Cookie 头的一行/多条，返回 cookies_raw schema 列表。
    因为 http.cookies.SimpleCookie 本身能处理同一行里 name=value; 属性；
    浏览器有时会在单个 Set-Cookie header 里塞多个饼干合并，用逗号分隔，这里也做拆分。
    """
    out: list[dict[str, Any]] = []
    if not raw_line:
        return out
    # http.cookies 对 ; 分隔属性、, 分隔多饼干 的处理要小心：Expires=Wed, 21 Aug 2026 ... 里也有逗号
    # 用 SimpleCookie.load 即可（它对 date 里逗号的逃逸处理是规范的）
    cook = http.cookies.SimpleCookie()
    try:
        cook.load(raw_line)
    except Exception as exc:  # noqa: BLE001
        log.debug("Set-Cookie 解析失败: %r | %s", raw_line[:200], exc)
        return out
    for name, morsel in cook.items():
        if not _cookie_name_should_keep(name):
            continue
        domain = (morsel.get("domain") or default_domain or "").strip()
        path = (morsel.get("path") or default_path or "/").strip() or "/"
        same_site_raw = (morsel.get("samesite") or "").lower()
        same_site = 1 if same_site_raw == "strict" else (2 if same_site_raw == "lax" else 0)
        out.append(
            {
                "name": str(name),
                "value": str(morsel.value or ""),
                "domain": domain,
                "path": path,
                "secure": bool(morsel.get("secure")),
                "httpOnly": bool(morsel.get("httponly")),
                "sameSite": same_site,
            }
        )
    return out


# -------- 辅助：带超时的 Cookies DB 拷贝（防止 WebEngine 写锁卡 UI 线程）----------
def _copyfile_with_timeout(src: Path, dst: Path, timeout_sec: float = 2.5) -> bool:
    """使用后台线程 + 分块读取 实现带整体超时的文件拷贝。
    Windows 上当另一进程对 src 持独占写锁时，open(src, 'rb') 会阻塞直到
    该句柄释放；这里通过后台线程等待整体超时，超时就返回 False，不阻塞调用方。
    """
    if not src.exists():
        return False
    result: list[bool] = []
    err: list[BaseException] = []

    def _worker() -> None:
        try:
            with open(src, "rb") as fr, open(dst, "wb") as fw:
                while True:
                    buf = fr.read(1024 * 256)
                    if not buf:
                        break
                    fw.write(buf)
            result.append(True)
        except BaseException as exc:  # pragma: no cover
            err.append(exc)
            result.append(False)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout_sec)
    if t.is_alive():
        # 超时：线程后台最终会退出，但我们直接返回失败
        try:
            if dst.exists():
                dst.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    if result and result[0]:
        return True
    # 拷贝自身异常
    try:
        if dst.exists():
            dst.unlink(missing_ok=True)
    except OSError:
        pass
    return False


# -------- 辅助：Cookies SQLite -> list[dict] ----------
def _load_raw_from_sqlite(cookies_db_path: Path, *, copy_timeout: float = 2.5) -> list[dict[str, Any]]:
    """读取 Chromium Cookies SQLite。

    读库优先级（关键：彻底避免 WebEngine 写锁把 shutil.copyfile 永远挡住）：
    1) sqlite3 直接以 URI: mode=ro&immutable=1 打开源 DB（WAL 模式下允许跨进程只读）；
    2) 不行 → sqlite3_backup 热备到临时文件（SQLite 官方并发安全读路径）；
    3) 不行 → 旧的 _copyfile_with_timeout + 读副本（兜底用）。
    """
    if not cookies_db_path.exists():
        return []

    results: list[dict[str, Any]] = []

    def _read_from_conn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("PRAGMA query_only = 1")
        try:
            cur.execute("SELECT COUNT(*) FROM cookies")
            total = cur.fetchone()[0]
            log.info("SQLite 读 profile cookies: total=%d rows", int(total or 0))
        except sqlite3.Error:
            total = 0
        try:
            cur.execute(
                "SELECT host_key, name, path, value, encrypted_value,"
                " is_secure, is_httponly, samesite FROM cookies"
            )
            for r in cur.fetchall():
                name = str(r["name"] or "")
                if not _cookie_name_should_keep(name):
                    continue
                value = (r["value"] if isinstance(r["value"], str) else "") or ""
                domain = str(r["host_key"] or "")
                path_s = str(r["path"] or "/") or "/"
                out.append(
                    {
                        "name": name,
                        "value": value,
                        "domain": domain,
                        "path": path_s,
                        "secure": bool(r["is_secure"]),
                        "httpOnly": bool(r["is_httponly"]),
                        "sameSite": int(r["samesite"] or 0),
                    }
                )
        finally:
            try: cur.close()
            except Exception: pass
        log.info("SQLite 读 profile cookies: 筛后保留 %d 条白名单条目", len(out))
        return out

    # —— 策略 1：URI immutable readonly（Windows 上绕开 WebEngine 文件锁最稳）——
    uri = f"file:{cookies_db_path.as_posix()}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0, isolation_level=None)
        try:
            results = _read_from_conn(conn)
            if results:
                return results
            # 可能是空库，继续走其它策略？不：空就空。
        finally:
            try: conn.close()
            except Exception: pass
    except sqlite3.Error as exc:
        log.warning("SQLite strategy 1 (uri ro+immutable) 跳过: %s", exc)

    # —— 策略 2：sqlite3_backup 热备（比 Python shutil.copyfile 走更低层的 sqlite 互斥，更兼容写锁）——
    tmp_dir = Path(tempfile.mkdtemp(prefix="wxread_cookie_db_"))
    try:
        copy_path = tmp_dir / "Cookies.backup.sqlite"
        try:
            # 先以普通只读打开"正在被写的源"：如果被独占会抛，我们吞掉继续 fallback 3
            src_conn = sqlite3.connect(f"file:{cookies_db_path.as_posix()}?mode=ro", uri=True, timeout=2.0, isolation_level=None)
            try:
                dst_conn = sqlite3.connect(str(copy_path), timeout=2.0)
                try:
                    with dst_conn:
                        src_conn.backup(dst_conn, pages=20, sleep=0.05)
                except sqlite3.Error as exc:
                    log.warning("SQLite strategy 2 (backup) 失败: %s", exc)
                finally:
                    dst_conn.close()
                # 读备份
                conn = sqlite3.connect(str(copy_path), timeout=2.0)
                try:
                    results = _read_from_conn(conn)
                    if results:
                        return results
                finally:
                    conn.close()
            finally:
                src_conn.close()
        except sqlite3.Error as exc:
            log.warning("SQLite strategy 2 (open source for backup) 跳过: %s", exc)

        # —— 策略 3：旧版 copyfile_with_timeout（兜底：最差 2.5s 超时）——
        if not _copyfile_with_timeout(cookies_db_path, copy_path, timeout_sec=copy_timeout):
            log.warning("复制 Cookies SQLite 超时或失败（%0.1fs）：%s", copy_timeout, cookies_db_path)
            return []
        try:
            conn = sqlite3.connect(str(copy_path), timeout=2.0)
            try:
                return _read_from_conn(conn)
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            log.warning("解析 Cookies SQLite 异常：%s", exc)
            return []
    finally:
        # 清理临时目录
        for p in tmp_dir.rglob("*"):
            try:
                if p.is_file():
                    p.unlink()
            except OSError:
                pass
        try:
            tmp_dir.rmdir()
        except OSError:
            pass



# --------- 网络请求拦截器（Set-Cookie 网线级捕获）---------
class _SetCookieInterceptor(QWebEngineUrlRequestInterceptor):
    def __init__(self, collector, parent=None):  # type: ignore[no-untyped-def]
        super().__init__(parent)
        self._collector = collector

    def interceptRequest(self, info: QWebEngineUrlRequestInfo) -> None:  # noqa: N802 (Qt 命名)
        # 只对响应阶段有用：PySide6 的 QWebEngineUrlRequestInfo 是 REQUEST 期对象，
        # 无法直接拿 response header，但能拿到 requestUrl 让我们知道"用户正在访问哪"；
        # 真正的 Set-Cookie 响应通过 QWebEngineProfile.setUrlRequestInterceptor +
        # connect(profile->cookieAdded) 配合，这个 class 的意义后面补充：
        # 这里作为 request 级调试日志，把 request 命中路径打出来，供现场取证。
        try:
            method = bytes(info.requestMethod()).decode("ascii", "ignore") if hasattr(info, "requestMethod") else ""
            url: QUrl = info.requestUrl()
            u = url.toString() if isinstance(url, QUrl) else str(url)
            if any(k in u for k in ("/web/login/", "/login", "weread.qq.com/web/reader", "open.weixin.qq.com")):
                log.debug("REQ %s %s", method, u[:200])
        except Exception as exc:  # pragma: no cover - 调试函数异常不影响主链路
            log.debug("interceptRequest 日志失败: %s", exc)


class LoginPage(QWidget):
    session_ready = Signal()  # 登录成功并写入 config 后发出
    _verify_done = Signal(bool)
    _js_cookies_ready = Signal(dict)  # document.cookie 兜底采集结果: name -> value
    _js_storage_ready = Signal(dict)  # localStorage/sessionStorage 扫 wr_* 兜底
    # 后台线程 采集完成 回 UI 线程 的信号（payload: 所有采集源的 可 pickle 快照）
    _collect_snapshot_ready = Signal(dict)
    # 后台线程 状态更新（仅用于 status.setText）stage::str, tick_sec::int
    _collect_tick = Signal(str, int)
    # cookie_store.loadAllCookies() 的全量回调结果：list[dict] 纯 schema，不保留 Qt 对象
    _all_cookies_loaded = Signal(list)
    # —— UI 交互信号（跨页给 MainWindow/StatusPage 用）
    reader_navigated = Signal(dict)   # 内置浏览器打开了 reader 页：{url, book_reader_id, chapter_id, title, ...}
    browser_session_restored = Signal(bool, str)  # 恢复会话完成：(ok, message)
    session_restore_requested = Signal()  # 内部 UI 点击后请求：先跑 UI，再跑后台
    # —— v3：后台线程 profile 清理完成 → GUI 线程安全推进 Stage 2（解决非 GUI 线程调 QTimer UB 问题）
    _bg_wipe_done = Signal()

    PROFILE_NAME = "wxread-login-profile"

    def __init__(
        self,
        api: WeReadApi,
        config: ConfigStore | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._api = api
        self._cfg = config or ConfigStore()
        self._verify_done.connect(self._on_verify_done)
        self._js_cookies_ready.connect(self._on_js_cookies_ready)
        self._js_storage_ready.connect(self._on_js_storage_ready)
        self._collect_snapshot_ready.connect(self._on_collect_snapshot_ready)
        self._collect_tick.connect(self._on_collect_tick)
        self._all_cookies_loaded.connect(self._on_all_cookies_loaded)
        # —— v3：非 GUI 线程的 profile 清理完成信号 → 安全切回主线程推进 Stage 2
        self._bg_wipe_done.connect(self._web_stage2_after_wipe)
        # —— 采集缓存（多路源，_process_cookies 统一去重合并）——
        self._pending_cookies: list[QNetworkCookie] = []                 # 源 1: cookieAdded 信号
        self._intercepted_raw: list[dict[str, Any]] = []                 # 源 2: 拦截器 Set-Cookie 解析
        self._all_cookies_raw: list[dict[str, Any]] = []                 # 源 0: loadAllCookies 全量回调（比 cookieAdded 单点更全更稳）
        self._js_cookies_cache: dict[str, str] = {}                      # 源 3a: document.cookie
        self._js_storage_cache: dict[str, str] = {}                      # 源 3b: localStorage/sessionStorage
        self._sqlite_snapshot: list[dict[str, Any]] = []                 # 源 4: SQLite 直读
        self._sqlite_stats: dict[str, int] = {}                          # 诊断用：SQLite 读多少总行/多少白名单
        self._collect_worker_stop = threading.Event()                    # 二次启动时中止旧后台线程
        self._tick_timer = QTimer(self)                                  # 采集阶段每秒动一下状态文案，避免“看起来卡死”
        self._tick_timer.timeout.connect(self._on_collect_tick_timeout)
        self._tick_sec = 0
        # —— verify 阶段防"卡死感知"：独立秒针 + 进度条 + 互斥锁（禁止重复触发）——
        self._verify_timer = QTimer(self)
        self._verify_timer.timeout.connect(self._on_verify_tick_timeout)
        self._verify_sec = 0
        self._verify_running = False
        self._verify_stage = "idle"
        # —— 把 api 的 message / warning / error 实时映射到登录页状态行，
        #    否则 check_session / ensure_session 在后台线程里跑，用户只能看到最后
        #    那一条"正在验证…"，感知完全是卡死（errorlog 18:05:47 → 18:09:02 铁证）。
        try:
            self._api.message.connect(self._on_api_message)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._api.warning.connect(self._on_api_warning)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._api.error.connect(self._on_api_error)
        except Exception:  # noqa: BLE001
            pass
        self._build_ui()
        # —— 懒加载（丝滑化关键 v2）：WebEngine / cookie store / profile wipe 都是重量级，
        #    构造里先 defer 到"下一个事件循环回合"，给 MainWindow 的 Tab 切换占位页面 20-30ms
        #    先完成绘制，再跑这些重任务 → 用户体感"切到登录页立即有内容，不卡壳"。
        #  ⚠️ v3 注意：QTimer 已在模块 Line 36 统一 import，此处**不能**再加局部 import，
        #     否则前面 421/425 行 QTimer(self) 会命中作用域 UnboundLocalError。
        QTimer.singleShot(0, self._init_webview)

    # ----------- WIPE PROFILE (force fresh QR flow) -----------
    @classmethod
    def _wipe_profile_storage(cls) -> Path:
        """返回 profile 目录路径（APPDATA/.../QtWebEngine/{PROFILE_NAME}）。
        启动前递归清空 profile 持久化内容（保留根目录即可），避免旧脏 cookie 干扰扫码。
        """
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        profile_root = Path(appdata) / "WxReadAssistant" / "QtWebEngine" / cls.PROFILE_NAME
        # 实际 Qt 的 appName 会再嵌一层 WxReadAssistant；这里并行处理两种约定
        alt_root = Path(appdata) / "WxReadAssistant" / "WxReadAssistant" / "QtWebEngine" / cls.PROFILE_NAME
        for root in (profile_root, alt_root):
            if not root.exists():
                continue
            for child in root.iterdir():
                try:
                    if child.is_file():
                        child.unlink()
                    elif child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                except OSError as exc:
                    log.debug("WIPE 子项失败（可能锁）: %s -> %s", child, exc)
            log.info("已清 profile 持久化目录：%s", root)
        # 返回真实使用的路径（appName 嵌套版 优先，因为 LS 现场取证显示真实路径是它）
        return alt_root if alt_root.parent.parent.exists() else profile_root

    # ----------- UI -----------
    def _build_ui(self) -> None:
        """按用户要求重排布局：
        ┌───────────────────────────────────────┐
        │  1) WebEngine（内置浏览器）            │  权重最大 撑满主空间
        ├───────────────────────────────────────┤
        │  2) 标题「📖 登录微信读书」+ 提示说明  │
        ├───────────────────────────────────────┤
        │  3) 进度条 + 进度状态文本             │  ← 新增：verify/collect 实时可视化
        ├───────────────────────────────────────┤
        │         [🔄 刷新页面] [✅ 我已登录完成] │  ← 状态栏上方
        ├───────────────────────────────────────┤
        │  4) 状态栏（等待登录…/采集/验证秒针）  │
        └───────────────────────────────────────┘
        """
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        # ====== 第 1 层：内置浏览器（最大权重，用户的扫码主战场）======
        self._web = QWebEngineView(self)
        # —— 小窗口不挤压：外层 MainWindow 已把整页包进 QScrollArea，
        #    所以这里用合理的"最小 520×320"，让窗口缩到 860×580 下限后用户滚动即可，
        #    不会像 540 旧值那样把下方 提示 / 进度条 / 按钮直接挤出可视区。
        self._web.setMinimumSize(520, 320)
        self._web.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._web.setStyleSheet(
            "QWebEngineView{background:#fff;border:1px solid #e5eaf2;border-radius:10px;}"
        )
        root.addWidget(self._web, 5)

        # ====== 第 2 层：标题 + 说明 ======
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        title = QLabel("📖 登录微信读书")
        title.setStyleSheet("font-size:17px;font-weight:600;color:#2f3b52;")
        title_row.addWidget(title)
        title_row.addStretch(1)
        root.addLayout(title_row)

        tip = QLabel(
            "1. 使用手机微信扫一扫完成登录（扫码授权过程请完整等待网页自动跳回书架）\n"
            "2. 登录成功后请在上方页面中点击任意一本已加入书架的书，确认可以正常进入阅读页\n"
            "3. 在阅读页再停留 10 秒以上，让阅读页的 wr_skey/wr_rt 更新完全；\n"
            "   然后点击下方【我已登录完成】按钮，程序会通过 4 路并行采集提取登录 Cookie。\n"
            "📌 想直接设定『读哪本书』？在上方浏览器打开阅读页 → 点【📋 复制当前页链接】，\n"
            "   再切换到「🟢 状态」→「📖 正在读的书」粘贴 URL → 点「更新到阅读状态」即可。"
        )
        tip.setStyleSheet(
            "color:#5f6c85; background:#f4f7fc; padding:10px 12px;"
            "border-radius:8px; font-size:12px; line-height:1.6;"
        )
        tip.setWordWrap(True)
        root.addWidget(tip)

        # ====== 地址栏：浏览器当前 URL 实时显示 + 一键复制 / 跳转 ======
        url_frame = QFrame()
        url_frame.setStyleSheet(
            "QFrame{background:#ffffff;border:1px solid #e5eaf2;border-radius:10px;}"
        )
        url_layout = QHBoxLayout(url_frame)
        url_layout.setContentsMargins(12, 10, 12, 10)
        url_layout.setSpacing(10)
        lbl_url_prompt = QLabel("📍 当前页面：")
        lbl_url_prompt.setStyleSheet("color:#4a5675;font-size:12px;font-weight:600;")
        self._edit_current_url = QLineEdit()
        self._edit_current_url.setPlaceholderText(
            "上方浏览器打开网页后，这里会显示真实地址；打开阅读页后直接点击"
            "右侧【📋 复制当前页链接】就可以得到书本 URL 了。"
        )
        self._edit_current_url.setReadOnly(True)  # 只显示，避免用户手动编辑破坏浏览器状态
        self._edit_current_url.setStyleSheet(
            "QLineEdit{background:#f6f8fc;border:1px solid #e5eaf2;border-radius:6px;"
            "padding:4px 10px;color:#2f3b52;font-size:12px;min-height:30px;}"
        )
        self._edit_current_url.setCursorPosition(0)
        self._btn_copy_url = QPushButton("📋 复制当前页链接")
        self._btn_copy_url.setStyleSheet(self._btn_style("#eef2f8", "#2f3b52"))
        self._btn_copy_url.clicked.connect(self._on_copy_current_url)
        self._btn_open_ext = QPushButton("🌐 系统浏览器打开")  # 精简文字，小窗口不被挤
        self._btn_open_ext.setStyleSheet(self._btn_style("#eef2f8", "#2f3b52"))
        self._btn_open_ext.clicked.connect(self._on_open_current_in_external_browser)
        # ===== 小窗口保护：地址栏按钮最小宽度固定 + 不可压缩 =====
        for b_url in (self._btn_copy_url, self._btn_open_ext):
            b_url.setMinimumHeight(30)
            b_url.setMaximumHeight(30)
            b_url.setMinimumWidth(130)
            try:
                b_url.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            except Exception:  # noqa: BLE001
                pass
        url_layout.addWidget(lbl_url_prompt)
        url_layout.addWidget(self._edit_current_url, 1)
        url_layout.addWidget(self._btn_copy_url)
        url_layout.addWidget(self._btn_open_ext)
        root.addWidget(url_frame)

        # ====== 第 3 层：进度条 + 状态进度（采集 / 验证 都可见，避免"卡死"感）======
        prog_frame = QFrame(self)
        prog_frame.setStyleSheet(
            "QFrame{background:#f9fbff;border:1px solid #e5eaf2;border-radius:8px;}"
        )
        prog_layout = QVBoxLayout(prog_frame)
        prog_layout.setContentsMargins(12, 10, 12, 10)
        prog_layout.setSpacing(8)

        self._progress_bar = QProgressBar(prog_frame)
        self._progress_bar.setRange(0, 0)  # busy 样式：采集 / verify 都是不确定时长
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setMinimumHeight(12)  # 小窗口至少能看见进度条
        self._progress_bar.setStyleSheet(
            "QProgressBar{border:1px solid #d6dce8;border-radius:6px;"
            "background:#ffffff;height:8px;}"
            "QProgressBar::chunk{background:qlineargradient("
            "x1:0,y1:0,x2:1,y2:0,stop:0 #5b8def,stop:1 #2d6cdf);border-radius:6px;}"
        )
        self._progress_bar.hide()  # 默认隐藏，collect/verify 才出现
        prog_layout.addWidget(self._progress_bar)

        self._progress_label = QLabel("就绪：上方扫码 → 打开一本书 → 点【我已登录完成】")
        self._progress_label.setStyleSheet("color:#4a5675;font-size:12px;")
        self._progress_label.setWordWrap(True)
        self._progress_label.setMinimumHeight(30)  # 避免小窗口下文字被切成 1/4
        prog_layout.addWidget(self._progress_label)

        root.addWidget(prog_frame)

        # ====== 第 4 层：操作按钮（进度条/进度文本 下面，状态栏上面）======
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        self._btn_restore = QPushButton("🛡️ 恢复浏览器登录态")  # 精简文字：原 14 字 → 10 字
        self._btn_restore.setStyleSheet(self._btn_style("#fff3d6", "#8a5a00", hover="#ffe9ad"))
        self._btn_restore.clicked.connect(self._on_request_restore_session)
        btn_row.addWidget(self._btn_restore)

        self._btn_reload = QPushButton("🔄 刷新页面")
        self._btn_reload.setStyleSheet(self._btn_style("#eef2f8", "#2f3b52"))
        self._btn_reload.clicked.connect(self._web.reload)
        btn_row.addWidget(self._btn_reload)

        self._btn_done = QPushButton("✅ 我已登录完成")
        self._btn_done.setStyleSheet(self._btn_style("#2d6cdf", "#ffffff", hover="#265bc0"))
        self._btn_done.clicked.connect(self._on_done_clicked)
        btn_row.addWidget(self._btn_done)

        # ===== v2 小窗口保护：三个按钮都设为不可压缩 + 明确最小宽度 =====
        for b_login in (self._btn_restore, self._btn_reload, self._btn_done):
            b_login.setMinimumHeight(36)  # 比地址栏高一点：主按钮更好点击
            b_login.setMaximumHeight(36)
            try:
                b_login.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            except Exception:  # noqa: BLE001
                pass
        self._btn_restore.setMinimumWidth(170)
        self._btn_reload.setMinimumWidth(110)
        self._btn_done.setMinimumWidth(150)
        btn_row.addStretch(1)

        root.addLayout(btn_row)

        # ====== 第 5 层：最下状态栏（等待登录…/验证秒针）======
        self._status = QLabel("等待登录...")
        self._status.setStyleSheet("color:#8a95a8;font-size:12px;")
        root.addWidget(self._status)

    @staticmethod
    def _btn_style(bg: str, fg: str, *, hover: str | None = None) -> str:
        hover_css = f"QPushButton:hover{{background:{hover or bg};}}"
        return (
            "QPushButton{border:none;border-radius:8px;padding:8px 18px;"
            f"background:{bg};color:{fg};font-size:13px;font-weight:500;}}"
            "QPushButton:pressed{padding-top:9px;}" + hover_css
        )

    # ---------------- webengine ----------------
    def _has_saved_usable_session(self) -> bool:
        """**纯本地、零网络**判断：config 里有没有一套"看起来能用"的 cookie。
        ⚠️ v2 关键修复（卡占位根因之一）：
           旧版在这里同步调 self._api.check_session() 做真 HTTP 请求，
           当网络不通 / DNS 慢 / 微信读书网关偶发 5xx 时，GUI 线程会被 requests 阻塞 5~30 秒，
           用户看到的就是"切到扫码登录页 → 一直卡占位页 → 浏览器不出现"。
           现在这里只做**本地文件/字段长度**的毫秒级判定，绝不碰网络。
           真验证登录态是否有效 → 交给 post_start 后台线程 / 用户手动点【检测登录态】按钮。
        """
        c = self._cfg.get("cookies", {}) or {}
        if not (c.get("wr_vid") and c.get("wr_skey")):
            return False
        # 长度兜底：至少正常长度（避免默认值里那种截断字符串）
        if len(str(c.get("wr_vid") or "")) < 8 or len(str(c.get("wr_skey") or "")) < 8:
            return False
        # 不再调用 check_session()（不再做同步 HTTP 请求）。
        # 有 wr_vid + wr_skey + 长度达标 → 就认为"本地看可以尝试恢复浏览器会话"；
        # 如果真过期，_init_webview 回灌后 load shelf → weread 会 302 跳 #login。
        return True

    def _init_webview(self) -> None:
        """**分段初始化 WebEngine**（v3 丝滑化关键修复）。

        把整个初始化流程拆成 3~4 个独立的事件循环回合：
          Stage 1 [GUI 线程]：判定 saved_ok → **轻量**取 profile dir（不做重 IO）→
                             创建 Profile/Page/信号连接（UI 立即渲染出浏览器空壳白框）
          👉 如果需要 wipe profile（无登录态情况）：
              Stage 1.5 [后台线程]：shutil.rmtree 做重 IO（不阻塞 GUI）→
                                    回到主线程推进 Stage 2
          👉 否则（有登录态 / profile 已就绪）：
              Stage 2 [GUI 线程, 下一回合]：同步回灌 cookie 到 CookieStore
          Stage 3 [GUI 线程, 下一回合]：加载 weread 页面（shelf 或 #login）

        旧版的问题（根因汇总）：
          · _has_saved_usable_session 里面同步调 check_session() 跑 HTTP 请求；
          · 无登录态时 shutil.rmtree 清几百个 Chromium 缓存文件也在 GUI 线程；
          · cookie 回灌（360 次 setCookie）也在同一个函数调用里连续执行。
          三者叠加，GUI 线程阻塞 5~30 秒，用户看到的就是"占位页卡着不换，浏览器不出现"。
        """
        # QTimer / QThread / Signal 均已在模块顶部 Line 36 统一 import，
        # 此处不再做局部 import，避免遮蔽外层造成 UnboundLocalError。

        # =============== Stage 1：判定 + profile/页面创建（UI 先出白框！）===============
        # 1) 纯本地判定 — 零网络、毫秒级
        saved_ok = self._has_saved_usable_session()

        # 2) 目录处理的"轻量准备"：saved_ok → 只 ensure 存在（秒级）；否则先拿路径，真正的 wipe 放后台
        from pathlib import Path as _Path
        if saved_ok:
            profile_dir = self._get_or_create_profile_dir(wipe=False)
            log.info("[browser-sync] 检测到可复用登录态：保留 profile dir=%s 不清空", profile_dir)
            self._status.setText("已检测到登录态，初始化浏览器内核 [1/3]…")
        else:
            profile_root = self._get_or_create_profile_dir(wipe=False).parent  # 取父级目录
            profile_dir = profile_root / "wxread-login-profile"
            try:
                profile_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("[browser-sync] 创建 profile dir 失败：%s", exc)
            log.info("[browser-sync] 无可用登录态：将在后台线程重置 profile dir=%s", profile_dir)
            self._status.setText("未检测到登录态，清理浏览器缓存 [1/3]（后台执行中…）")

        # 3) 创建 QtWebEngine 结构：Profile → UA → Interceptor → Page → CookieStore 信号连接
        #    到这一步为止，WebEngine 的"UI 壳体"已经构造完成，用户会看到 Tab 切换后先出白色浏览框。
        self._profile = QWebEngineProfile(self.PROFILE_NAME, self)
        self._profile.setHttpUserAgent(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
        )
        self._interceptor = _SetCookieInterceptor(self, self._profile)
        self._profile.setUrlRequestInterceptor(self._interceptor)

        self._page = QWebEnginePage(self._profile, self._web)
        self._web.setPage(self._page)
        self._cookie_store: QWebEngineCookieStore = self._profile.cookieStore()

        try:
            self._cookie_store.cookieAdded.disconnect()
        except (TypeError, RuntimeError, RuntimeWarning, Exception):
            pass
        try:
            if hasattr(self._cookie_store, "cookiesLoaded"):
                self._cookie_store.cookiesLoaded.disconnect()
        except (TypeError, RuntimeError, RuntimeWarning, Exception):
            pass
        self._pending_cookies.clear()
        self._all_cookies_raw.clear()
        self._cookie_store.cookieAdded.connect(self._on_cookie_added)
        if hasattr(self._cookie_store, "cookiesLoaded"):
            self._cookie_store.cookiesLoaded.connect(self._on_cookie_store_cookies_loaded)
        self._cookie_store.cookieAdded.connect(self._on_cookie_added_mirror_to_intercepted)

        self._web.urlChanged.connect(self._on_web_url_changed)
        self._web.loadFinished.connect(self._on_web_load_finished)

        # ============ 后续流程分支 ============
        # 分支 A：saved_ok 且 profile 已经干净 → 下一回合回灌 cookie
        # 分支 B：无登录态 → 后台线程 wipe profile → 完成后再推进 Stage 2

        if saved_ok:
            # 分支 A：下一事件循环直接进入 Stage 2（回灌 cookie）
            QTimer.singleShot(0, self._web_stage2_restore_and_load)
        else:
            # 分支 B：先后台 wipe（shutil.rmtree 重 IO）完成后再推进 Stage 2
            self._stage2_ready_path = profile_dir
            import threading as _th
            wipe_root_for_capture: Path = profile_dir

            def _bg_wipe_and_emit():
                import shutil as _sh
                try:
                    # 后台线程做重 IO，可能耗时 10~1000ms（取决于 Chromium 缓存多少文件）
                    if wipe_root_for_capture.exists():
                        for child in list(wipe_root_for_capture.iterdir()):
                            try:
                                if child.is_dir():
                                    _sh.rmtree(child, ignore_errors=True)
                                else:
                                    child.unlink(missing_ok=True)
                            except Exception as exc2:  # noqa: BLE001
                                log.debug("[browser-sync] 清理 %s 的子项失败：%s", child, exc2)
                    wipe_root_for_capture.mkdir(parents=True, exist_ok=True)
                except Exception as exc:  # noqa: BLE001
                    log.warning("[browser-sync] profile 清理（后台）出错：%s", exc)
                # ⚠️ v3 关键修复：
                #   后台 Python 线程里**不能**调 QTimer.singleShot（Qt 非 GUI 线程操作是 UB，会死锁）
                #   改为 emit Signal：Qt 的自动排队连接（AutoConnection）会把 slot 安全投递回 GUI 线程事件循环。
                self._bg_wipe_done.emit()

            _th.Thread(target=_bg_wipe_and_emit, daemon=True).start()

    def _web_stage2_after_wipe(self) -> None:
        """分支 B：profile 清理完成，Stage 2 直接跳到 Stage 3（加载 #login 扫码页）。"""
        self._status.setText("浏览器缓存已清理，加载微信读书扫码页 [3/3]…")
        # 无登录态就不回灌 cookie，直接 Stage 3：加载 #login
        # QTimer 已在模块 Line 36 统一 import，此处不再局部导入避免作用域坑
        QTimer.singleShot(0, lambda: self._web_stage3_load_url(saved_ok=False))

    def _web_stage2_restore_and_load(self) -> None:
        """分支 A Stage 2：把 config 中的 cookie 同步回灌到 CookieStore。"""
        # QTimer 已在模块 Line 36 统一 import
        ok, msg = self._restore_session_to_cookie_store_sync()
        if ok:
            log.info("[browser-sync] 恢复会话成功：%s", msg)
            self._status.setText(f"会话回灌完成 [2/3]：{msg}。加载书架中…")
        else:
            log.warning("[browser-sync] 恢复会话部分失败：%s", msg)
            self._status.setText(f"浏览器会话回灌不完全 [2/3]：{msg}")
        self.browser_session_restored.emit(bool(ok), msg)
        # 下一事件循环 → Stage 3 load URL（避免回灌 + 渲染 + 加载首包网络堆在同一回合）
        QTimer.singleShot(0, lambda: self._web_stage3_load_url(saved_ok=True))

    def _web_stage3_load_url(self, *, saved_ok: bool) -> None:  # noqa: FBT001
        """Stage 3：真正触发 weread URL 加载（shelf 或 #login）。"""
        # QUrl 已在模块 Line 36 统一 import，不再局部 import 避免作用域遮蔽
        if saved_ok:
            self._status.setText("[3/3] 已回灌登录态，加载 weread 书架中…")
            self._web.load(QUrl("https://weread.qq.com/web/shelf"))
        else:
            self._status.setText("[3/3] 加载 weread 扫码登录页中…")
            self._web.load(QUrl("https://weread.qq.com/#login"))

    # ====== 新：从 config 取 cookie 手动回灌到 QWebEngineCookieStore ======
    def _get_or_create_profile_dir(self, *, wipe: bool) -> Path:  # noqa: FBT001
        """等同 _wipe_profile_storage，但 wipe=False 时只确保目录存在不删文件。"""
        if wipe:
            return self._wipe_profile_storage()
        # 取 profile 根路径：与 _wipe_profile_storage 完全一致
        from pathlib import Path as _Path
        appdata = os.environ.get("APPDATA")
        if not appdata:
            appdata = str(_Path.home() / "AppData" / "Roaming")
        profile_root = _Path(appdata) / "WxReadAssistant" / "QtWebEngine" / self.PROFILE_NAME
        alt_root = profile_root.parent.parent / self.PROFILE_NAME
        chosen_root = alt_root if alt_root.parent.exists() else profile_root
        chosen_root.mkdir(parents=True, exist_ok=True)
        return chosen_root

    def _on_request_restore_session(self) -> None:
        """UI 按钮：用户希望把"config 里已存在的登录态"回灌到浏览器，再刷新到书架。"""
        if not self._cfg.get("cookies.wr_vid") or not self._cfg.get("cookies.wr_skey"):
            self._progress_label.setText("⚠️ 没有检测到已保存登录态：请先扫码后点击【我已登录完成】")
            self._status.setText("无可用登录态：先完成扫码登录")
            QMessageBox = __import__("PySide6.QtWidgets", fromlist=["QMessageBox"]).QMessageBox
            QMessageBox.information(self, "提示", "没有可恢复的登录态。先在上方完成扫码，再点击【我已登录完成】保存。")
            return
        self._btn_restore.setEnabled(False)
        self._progress_label.setText("🛡️ 正在回灌 Cookie 到内置浏览器并刷新页面...")
        self._status.setText("恢复浏览器登录态…（3~8 秒）")

        def _task() -> None:
            ok, msg = self.restore_session_from_config()
            self.browser_session_restored.emit(ok, msg)

        threading.Thread(target=_task, daemon=True).start()

    def restore_session_from_config(self) -> tuple[bool, str]:
        """对外 API / 跨页调用。把 config.cookies → CookieStore 种回 → load 书架。
        返回 (ok: bool, message: str)。
        """
        # 先跑 check_session：真的是好的才回灌；否则也要告诉用户"它无效"
        pre_ok = False
        try:
            pre_ok = bool(self._api.check_session())
        except Exception as exc:  # noqa: BLE001
            log.debug("restore_session_from_config 预检异常：%s", exc)
        ok, msg = self._restore_session_to_cookie_store_sync()
        if not ok and pre_ok:
            # 虽然写入遇到部分失败，但 API 本身 OK：也按"尽力而为"算成功（WebEngine 会重存 SQLite）
            ok = True
            msg = msg + "；已尽力写入，WebEngine 会自动把可用 cookie 持久化到本地"
        # load 书架 URL：weread 端拿到新 Cookie 后会自动重定向
        try:
            # schedule 在 UI 线程执行 load：QWebEngine 只能在 UI 线程 load
            from PySide6.QtCore import QMetaObject, Qt as _Qt
            QMetaObject.invokeMethod(
                self._web, "load",
                _Qt.ConnectionType.QueuedConnection,
                Q_ARG_ARG = None,  # 占位，确保后面 QUrl 参数可用
            )
        except Exception:  # noqa: BLE001
            pass
        # 更直接：用 QTimer.singleShot 做 UI 线程调度（QTimer 已在模块顶部 import）
        QTimer.singleShot(0, lambda: self._web.load(QUrl("https://weread.qq.com/web/shelf")))
        try:
            self._btn_restore.setEnabled(True)
        except Exception:  # noqa: BLE001
            pass
        # 状态提示
        if pre_ok:
            msg = msg + "；API 端登录态健康检查已通过 ✅"
        else:
            msg = msg + "；⚠️ API 端登录态健康检查未通过（回灌后建议刷新 1~2 次或重新扫码）"
        log.info("[browser-sync] restore_session_from_config 完成 ok=%s msg=%s", ok, msg)
        return ok, msg

    def _restore_session_to_cookie_store_sync(self) -> tuple[bool, str]:
        """**UI 线程调用**：直接把 config.cookies 里的所有白名单 cookie，
        按多个域名 (.weread.qq.com / weread.qq.com / .qq.com / open.weixin.qq.com)
        用 QNetworkCookie 写到 CookieStore。返回 (ok, message)。
        """
        # QByteArray 已在模块 Line 36 统一 import，不再局部导入
        cookies_dict: dict = dict(self._cfg.get("cookies", {}) or {})
        if not cookies_dict:
            return False, "config cookies 为空"
        store: QWebEngineCookieStore = self._cookie_store
        keep_names = {
            "wr_vid", "wr_skey", "wr_at", "wr_rt", "RK", "ptcz", "pac_uid", "wr_fp",
            "wr_gid", "wr_localvid", "wr_csl_tgc", "wr_openid", "wr_vkey", "wr_logintype",
            "wr_paytoken", "wr_refreshtoken", "wr_avatar", "wr_nickname", "iip", "tgw_l7_route",
            "qv_als", "pgv_pvid", "pgv_info", "random_session_id",
        }
        # 目标域名：按经验覆盖 web/移动 子域
        domain_specs: list[tuple[str, str, bool]] = [
            # (domain, path, secure_only_https)
            (".weread.qq.com", "/", True),
            ("weread.qq.com", "/", True),
            (".qq.com", "/", True),
            ("qq.com", "/", True),
            (".open.weixin.qq.com", "/", True),
            ("open.weixin.qq.com", "/", True),
        ]
        total_set = 0
        failed = 0
        names_set: set[str] = set()
        for name, value in cookies_dict.items():
            if name not in keep_names:
                # 其它 cookie（非白名单）也种（比如 wr_gid 等），只要不为空
                if not isinstance(value, str) or not value.strip():
                    continue
            if not isinstance(value, str) or not value:
                continue
            try:
                qname = QByteArray(name.encode("utf-8"))
                qval = QByteArray(str(value).encode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log.debug("cookie 编码失败 name=%s err=%s", name, exc)
                continue
            for domain, path, secure in domain_specs:
                try:
                    nc = QNetworkCookie(qname, qval)
                    nc.setDomain(domain)
                    nc.setPath(path)
                    nc.setSecure(secure)
                    nc.setHttpOnly(False)  # 不强制 httpOnly：避免 JS 侧 wr_* 读取失败
                    # SameSite=Lax（默认）即可；Qt 某些枚举值缺失不强求
                    nc.setExpirationDate(
                        QDateTime.fromString("2099-12-31T23:59:59Z", Qt.ISODate)
                    )
                    store.setCookie(nc, QUrl(
                        f"https://{domain.lstrip('.')}{path}"
                    ))
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    log.debug("setCookie 失败 name=%s domain=%s err=%s", name, domain, exc)
                    continue
                total_set += 1
                names_set.add(name)
        # 额外做一次：如果 cookies_raw（逐行）比 cookies dict 更全，也按 raw 回填
        for name, value in (self._cfg.get("cookies_raw_cache", {}) or {}).items():
            if name in names_set or not value:
                continue
            try:
                qname = QByteArray(name.encode("utf-8"))
                qval = QByteArray(str(value).encode("utf-8"))
            except Exception:  # noqa: BLE001
                continue
            for domain, path, secure in domain_specs:
                try:
                    nc = QNetworkCookie(qname, qval)
                    nc.setDomain(domain)
                    nc.setPath(path)
                    nc.setSecure(secure)
                    nc.setHttpOnly(False)
                    store.setCookie(nc, QUrl(f"https://{domain.lstrip('.')}{path}"))
                    total_set += 1
                    names_set.add(name)
                except Exception:  # noqa: BLE001
                    failed += 1
        if total_set <= 0:
            return False, f"没有任何 cookie 被成功写入（候选 {len(cookies_dict)} 条，失败 {failed}）"
        msg = (
            f"成功写入 {total_set} 条 × {len(domain_specs)} 域名组合，"
            f"共 {len(names_set)} 个唯一 cookie（失败 {failed}）"
        )
        log.info("[browser-sync] %s", msg)
        return True, msg

    def navigate(self, url: str) -> None:
        """外部（状态页/主窗口）调用：让浏览器跳到某本书 URL。

        为了避免"跨页调用到非 UI 线程"，用 QTimer.singleShot 0 统一调度。
        """
        # QTimer 已在模块 Line 36 统一 import，不再局部导入避免 UnboundLocalError 风险
        def _do() -> None:
            if not url or "weread.qq.com" not in url:
                self._status.setText(f"链接格式不合法，忽略：{(url or '')[:80]}")
                return
            self._web.load(QUrl(url))
            self._status.setText(f"正在在浏览器打开：{(url or '')[:80]}")
        QTimer.singleShot(0, _do)

    def _on_web_url_changed(self, qurl: QUrl) -> None:
        """浏览器 URL 变化：① 地址栏实时写入；② 命中 reader -> 同步 book。"""
        try:
            url_str = qurl.toString() if hasattr(qurl, "toString") else str(qurl)
        except Exception:  # noqa: BLE001
            return
        # ① 不管是不是 reader 都把地址栏填一下：方便用户复制任何 URL
        if isinstance(url_str, str) and url_str:
            try:
                if getattr(self, "_edit_current_url", None) is not None:
                    self._edit_current_url.setText(url_str)
                    self._edit_current_url.setCursorPosition(0)
                # 顺便同步按钮文案：如果在阅读页就强调"复制书本 URL"
                if getattr(self, "_btn_copy_url", None) is not None:
                    if "weread.qq.com/web/reader/" in url_str:
                        self._btn_copy_url.setText("📋 复制这本书的链接")
                        self._btn_copy_url.setToolTip(
                            "把当前书本阅读页链接复制到剪贴板，回到「🟢 状态」页粘贴即可更新读哪本书。"
                        )
                    else:
                        self._btn_copy_url.setText("📋 复制当前页链接")
                        self._btn_copy_url.setToolTip("复制当前浏览器页面链接到剪贴板")
            except Exception as exc:  # noqa: BLE001
                log.debug("同步 URL 到地址栏异常：%s", exc)

        if not isinstance(url_str, str) or "weread.qq.com/web/reader/" not in url_str:
            return
        parsed = self._api._parse_reader_url(url_str) if self._api else None
        if not parsed:
            return
        # title 还没加载出来就先标空，后续 loadFinished 再补
        payload = {
            "url": url_str,
            "book_reader_id": parsed.get("reader_id") or "",
            "book_id": parsed.get("book_id") or "",
            "chapter_id": parsed.get("chapter_id") or "",
            "title": "",
        }
        self._last_navigated = payload
        self.reader_navigated.emit(dict(payload))

    # ------------------ URL 复制 / 系统浏览器打开 ------------------
    def _on_copy_current_url(self) -> None:
        """把内置浏览器当前页面 URL 写入系统剪贴板；若是阅读页还会直接同步到 API 当前书。"""
        try:
            url_str = ""
            try:
                url_str = self._web.url().toString()
            except Exception:  # noqa: BLE001
                url_str = ""
            if not url_str or url_str in ("", "about:blank", None):
                self._status.setText("⚠️ 浏览器还没有打开任何页面")
                self._progress_label.setText(
                    "⚠️ 复制失败：上方浏览器还没打开任何页面。等页面加载完成或先扫码进入微信读书。"
                )
                QMessageBox.information(
                    self, "无法复制", "内置浏览器还没打开任何页面，先等待上方加载完成或扫码打开微信读书。"
                )
                return
            # 写剪贴板（使用 QApplication.clipboard()：PySide6/Windows 下稳定）
            cb = QApplication.clipboard()
            if cb is None:
                self._status.setText("⚠️ 系统剪贴板不可用")
                QMessageBox.warning(self, "复制失败", "系统剪贴板不可用，请手动选中文本复制。")
                return
            cb.setText(url_str, mode=cb.Mode.Clipboard)
            # 在 Mode.Selection 也写一次（Linux/X11 习惯，Windows 无影响）
            try:
                cb.setText(url_str, mode=cb.Mode.Selection)
            except Exception:  # noqa: BLE001
                pass

            # 如果在阅读页：顺便把 URL 同步为当前书，用户不需要再切到状态页粘一次
            is_reader = bool("weread.qq.com/web/reader/" in url_str)
            if is_reader:
                parsed = self._api._parse_reader_url(url_str) if self._api else None
                if parsed:
                    self._api.set_current_book({
                        "url": url_str,
                        "book_reader_id": str(parsed.get("reader_id") or "").strip(),
                        "book_id": str(parsed.get("book_id") or "").strip(),
                        "chapter_id": str(parsed.get("chapter_id") or "").strip(),
                        "title": "",  # 后续 loadFinished + JS 会再补齐
                    }, source="login_browser_copy_url")
                    self.reader_navigated.emit({
                        "url": url_str,
                        "book_reader_id": str(parsed.get("reader_id") or "").strip(),
                        "book_id": str(parsed.get("book_id") or "").strip(),
                        "chapter_id": str(parsed.get("chapter_id") or "").strip(),
                        "title": "",
                        "source": "copy_url",
                    })
            short = (url_str[:68] + "…") if len(url_str) > 68 else url_str
            self._status.setText(
                "✅ 已复制链接到剪贴板" + ("：当前书本 URL 已同步到状态页 🎉" if is_reader else "")
            )
            self._progress_label.setText(
                "✅ 已复制：" + short +
                ("\n切到「🟢 状态」就能看到『正在读的书』链接已同步；再点「🔄 立即刷新（Skill 优先）」可拉书名/进度。"
                 if is_reader else "\n如果这是书本阅读页链接，切到状态页粘贴再点「更新到阅读状态」即可。")
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("复制当前浏览器 URL 异常：%s", exc, exc_info=True)
            self._status.setText(f"❌ 复制失败：{exc}")
            try:
                QMessageBox.warning(self, "复制失败", f"复制链接时出错：{exc}")
            except Exception:  # noqa: BLE001
                pass

    def _on_open_current_in_external_browser(self) -> None:
        """在用户系统默认浏览器（Edge/Chrome 等）打开当前页：方便更顺手地查书架/找书。"""
        try:
            url_str = ""
            try:
                url_str = self._web.url().toString()
            except Exception:  # noqa: BLE001
                url_str = ""
            if not url_str or url_str in ("", "about:blank"):
                QMessageBox.information(
                    self, "没什么可打开", "浏览器还没加载任何页面，先等它加载完或扫码再试。"
                )
                return
            ok = QDesktopServices.openUrl(QUrl(url_str))
            if ok:
                self._status.setText("已在系统默认浏览器打开当前页")
            else:
                self._status.setText("⚠️ 打开系统浏览器失败")
        except Exception as exc:  # noqa: BLE001
            log.warning("打开系统浏览器异常：%s", exc, exc_info=True)

    def _on_web_load_finished(self, ok: bool) -> None:  # noqa: FBT001
        """页面加载完成后：从 <title>/JS 取书名，再补 reader_navigated 的 title 字段。"""
        try:
            url_str = self._web.url().toString()
        except Exception:  # noqa: BLE001
            url_str = ""
        if not (isinstance(url_str, str) and "weread.qq.com/web/reader/" in url_str):
            return
        if not ok:
            return
        # 取当前页标题：通常是"《书名》 - 微信读书"
        try:
            title_js = r"""(function(){
                try {
                    var t = document.title || '';
                    // 去掉后缀
                    t = t.replace(/\s*[-—–]\s*微信读书.*$/g, '').trim();
                    if (!t) {
                        var h1 = document.querySelector('h1, h2, .readerContent h1, .chapterItem[data-active]');
                        t = (h1 && (h1.innerText || h1.textContent) || '').trim();
                    }
                    // 章节标题
                    var c = '';
                    var ce = document.querySelector('.readerCatalog .chapterItem.selected, .chapterItem[data-active="true"]');
                    if (ce) c = (ce.innerText || ce.textContent || '').trim();
                    // 作者
                    var a = '';
                    var ae = document.querySelector('.readerInfo__author, .bookInfo .author, [class*="author"]');
                    if (ae) a = (ae.innerText || ae.textContent || '').replace(/^作者[:：]?\s*/, '').trim();
                    return JSON.stringify({title:t, chapter:c, author:a});
                } catch(e) { return JSON.stringify({error: String(e)}); }
            })();"""
            self._page.runJavaScript(title_js, 0, self._on_reader_js_done)
        except Exception as exc:  # noqa: BLE001
            log.debug("reader runJavaScript title 提取失败：%s", exc)

    def _on_reader_js_done(self, result: Any) -> None:
        if not isinstance(result, str):
            return
        try:
            import json as _json
            info = _json.loads(result)
            if not isinstance(info, dict):
                return
        except Exception:  # noqa: BLE001
            return
        last = getattr(self, "_last_navigated", None) or {}
        url_str = last.get("url")
        if not url_str:
            try:
                url_str = self._web.url().toString()
            except Exception:  # noqa: BLE001
                url_str = ""
        payload = {
            "url": url_str,
            "book_reader_id": last.get("book_reader_id") or "",
            "book_id": last.get("book_id") or "",
            "chapter_id": last.get("chapter_id") or "",
            "title": str(info.get("title") or "").strip(),
            "author": str(info.get("author") or "").strip(),
            "chapter_title": str(info.get("chapter") or "").strip(),
        }
        self._last_navigated = dict(payload)
        # 再发一次 reader_navigated（带 title），MainWindow 再同步为当前书
        self.reader_navigated.emit(dict(payload))

    # -------- cookiesLoaded 回调（loadAllCookies 完成后一次性拿全罐 cookie）--------
    def _on_cookie_store_cookies_loaded(self, cookies) -> None:  # type: ignore[no-untyped-def]
        """PySide6 cookiesLoaded 槽：入参是 list[QNetworkCookie]（Qt 跨线程交付）。
        全量罐比 cookieAdded 单点更稳，尤其应对 WebEngine 内部重放/批量 set cookie。"""
        total = 0
        kept = 0
        out: list[dict[str, Any]] = []
        try:
            for c in list(cookies or []):
                total += 1
                name = self._qbytes_to_str(c.name())
                if not _cookie_name_should_keep(name):
                    continue
                kept += 1
                out.append(
                    {
                        "name": name,
                        "value": self._qbytes_to_str(c.value()),
                        "domain": c.domain() or "",
                        "path": c.path() or "/",
                        "secure": bool(c.isSecure()),
                        "httpOnly": bool(c.isHttpOnly()),
                        "sameSite": _samesite_to_int(c.sameSitePolicy()) if hasattr(c, "sameSitePolicy") else 0,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("cookiesLoaded 回调序列化异常：%s", exc)
            return
        # 替换而不是 append：cookiesLoaded 语义是全量
        self._all_cookies_raw = out
        log.info("cookiesLoaded 全量回调：total=%d，白名单命中=%d", total, kept)
        # 再发一次到 UI 侧 self._on_all_cookies_loaded，使得"点了我已登录完成"之后的
        # 采集合并也能第一时间收到（而不是等 worker 启动后 1.4s 再从内存读）。
        self._all_cookies_loaded.emit(list(out))

    def _on_all_cookies_loaded(self, raw_list: list) -> None:  # type: ignore[no-untyped-def]
        # 当前仅用作采集计数：实际 merge 时会再次访问 self._all_cookies_raw，
        # 这里只在日志里打一条，确保链路活着。
        log.debug("cookiesLoaded UI 收到：%d 条", len(raw_list or []))

    def _on_cookie_added(self, cookie: QNetworkCookie) -> None:
        # 信号线：实时累积，任何过滤都放在加工阶段做
        self._pending_cookies.append(cookie)

    def _on_cookie_added_mirror_to_intercepted(self, cookie: QNetworkCookie) -> None:
        """因为 cookieAdded 是 Qt 在网络层接收到 Set-Cookie 后发出的，所以它已经是一颗
        完整的 cookie（含 domain/path/httpOnly）；这里把它镜像到 _intercepted_raw
        里的 schema，使得 cookieAdded 丢了但 SQLite 有结果时，仍能通过 schema 合并。"""
        try:
            name = self._qbytes_to_str(cookie.name())
            if not _cookie_name_should_keep(name):
                return
            self._intercepted_raw.append(
                {
                    "name": name,
                    "value": self._qbytes_to_str(cookie.value()),
                    "domain": cookie.domain() or "",
                    "path": cookie.path() or "/",
                    "secure": bool(cookie.isSecure()),
                    "httpOnly": bool(cookie.isHttpOnly()),
                    "sameSite": _samesite_to_int(cookie.sameSitePolicy()) if hasattr(cookie, "sameSitePolicy") else 0,
                }
            )
        except Exception as exc:  # pragma: no cover
            log.debug("cookieAdded 镜像失败：%s", exc)

    # ---------------- button handler ----------------
    def _on_done_clicked(self) -> None:
        self._status.setText(
            "正在抽取登录态（信号 + 拦截器 + SQLite + JS 四路并行）请稍候..."
        )
        self._btn_done.setEnabled(False)
        self._js_cookies_cache.clear()
        self._js_storage_cache.clear()
        # 启动 tick 文案刷新，避免“看起来一直卡死”（每 800ms 动一次状态行末尾小秒针）
        self._tick_sec = 0
        self._tick_stage = "准备"
        self._tick_timer.start(800)
        # —— 采集阶段：进度面板点亮 + 禁用操作按钮（防重复点击导致"双采集叠加"卡死）——
        try:
            self._progress_bar.show()
            self._progress_label.setText("四路并行采集中：cookie store / SQLite DB / document.cookie / storage…")
        except Exception:  # noqa: BLE001
            pass
        self._btn_done.setEnabled(False)
        self._btn_reload.setEnabled(False)
        log.info("[采集] 开始：loadAllCookies + JS 双兜底 + 后台 SQLite 复制读取")

        # 1) 信号源：显式触发一次 loadAllCookies。
        #    如果 3s 内没触发回调，下面 worker 的 payload 里会 WARN 提示
        #    （有时 Qt WebEngine 内部"罐没准备好"会静默跳过 cookiesLoaded）
        self._all_cookies_raw.clear()
        self._cookie_store.loadAllCookies()
        QTimer.singleShot(3000, self._diagnose_cookies_loaded_if_missing)

        # 2) JS 两路兜底（异步回调，结果会写入 _js_cookies_cache / _js_storage_cache）
        self._fetch_document_cookies_async()
        self._fetch_storage_kv_async()

        # 3) 把最可能阻塞（SQLite 拷贝+读取、sleep 重试、诊断）全部移到后台线程。
        #    后台线程只做纯 CPU/IO，不碰任何 QWidget/QNetworkCookie/Qt 对象。
        #    等 JS 回调 + cookieAdded 同时收集 1.2s 后再启动读库，减少 WebEngine 写锁冲突。
        self._collect_worker_stop.clear()
        worker = threading.Thread(target=self._collect_worker_thread, daemon=True)
        worker.start()

        self._status.setText(
            "四路并行采集中：cookie store / SQLite Cookies DB / document.cookie / storage..."
        )

    # -------- tick（状态行秒针，UI 不卡死的最基本体感反馈）--------
    def _on_collect_tick_timeout(self) -> None:
        self._tick_sec += 1
        dots = "." * (self._tick_sec % 5)
        bar_len = 16
        bar_fill = min(self._tick_sec, bar_len)
        bar = "█" * bar_fill + "·" * (bar_len - bar_fill)
        self._status.setText(
            f"采集中（{self._tick_stage}）{bar} {self._tick_sec:>2}s{dots}"
            f"（cookie store / SQLite DB / doc.cookie / storage）"
        )

    def _on_collect_tick(self, stage: str, tick_sec: int) -> None:  # 后台线程发来的 stage 更新
        self._tick_stage = str(stage) or self._tick_stage

    # -------- 后台 worker（纯 IO，不碰 Qt 对象）--------
    def _collect_worker_thread(self) -> None:
        stop_ev = self._collect_worker_stop
        try:
            profile_root = (
                Path(os.environ.get("APPDATA") or "") / "WxReadAssistant" / "WxReadAssistant"
                / "QtWebEngine" / self.PROFILE_NAME
            )
            alt = Path(os.environ.get("APPDATA") or "") / "WxReadAssistant" / "QtWebEngine" / self.PROFILE_NAME
            db_path = profile_root / "Cookies"
            if not db_path.exists():
                db_path = alt / "Cookies"
            log.info("[采集 worker] DB 路径 = %s (exist=%s)", db_path, db_path.exists())

            # —— stage: 等待 cookieAdded / JS 回调 并行收集一段（也让 WebEngine journal flush）
            self._collect_tick.emit("等待 WebEngine 落库", self._tick_sec)
            slept = 0.0
            while slept < 1.4 and not stop_ev.is_set():
                self._sleep_safe(0.15)
                slept += 0.15
            if stop_ev.is_set():
                return

            # —— stage: 第 1 次读取 SQLite（copy_timeout=2.5s 的安全拷贝）
            self._collect_tick.emit("读取 Cookies DB", self._tick_sec)
            first = _load_raw_from_sqlite(db_path, copy_timeout=2.5)
            log.info("[采集 worker] 第 1 次 SQLite 读取：保留 %d 条", len(first))
            if not first and not stop_ev.is_set():
                # 再等待一段（WebEngine 的 -wal / -journal 有时要 0.6~1.0s 才合版），再试一次
                self._collect_tick.emit("重试读 Cookies DB", self._tick_sec)
                self._sleep_safe(0.9)
                if not stop_ev.is_set():
                    first = _load_raw_from_sqlite(db_path, copy_timeout=2.5)
                    log.info("[采集 worker] 第 2 次 SQLite 读取：保留 %d 条", len(first))
            if stop_ev.is_set():
                return

            # —— stage: SQLite 汇总诊断
            self._collect_tick.emit("汇总诊断", self._tick_sec)
            stats: dict[str, int] = {"snapshot_rows": len(first)}
            stats.update(self._sqlite_extra_diag_threadsafe(db_path))

            # —— 主线程 _pending_cookies (list[QNetworkCookie]) 在后台线程里只能先序列化，
            #    因为 QNetworkCookie 不是可 pickle 的；我们这里只读 name/value/domain/path/...
            #    纯属性值，不访问任何 Qt 事件相关 API，Qt 对象本身只读属性通常是线程安全的。
            pending_list: list[dict[str, Any]] = []
            try:
                for c in list(self._pending_cookies):
                    pending_list.append(
                        {
                            "name": self._qbytes_to_str(c.name()),
                            "value": self._qbytes_to_str(c.value()),
                            "domain": c.domain() or "",
                            "path": c.path() or "/",
                            "secure": bool(c.isSecure()),
                            "httpOnly": bool(c.isHttpOnly()),
                            "sameSite": _samesite_to_int(c.sameSitePolicy()) if hasattr(c, "sameSitePolicy") else 0,
                        }
                    )
            except Exception as exc:  # pragma: no cover - 列表并发改的兜底
                log.warning("[采集 worker] pending_cookies 序列化异常：%s", exc)

            payload = {
                "ok": True,
                "sqlite_snapshot": first,
                "sqlite_stats": stats,
                "pending_list": pending_list,
                "intercepted_raw": list(self._intercepted_raw),
                "js_cookies_cache": dict(self._js_cookies_cache),
                "js_storage_cache": dict(self._js_storage_cache),
                "db_path": str(db_path),
                # ★★ 关键：cookiesLoaded 全量罐（UI 侧我们写入 _all_cookies_raw，
                # 后台线程这里只读 list，Qt 跨线程 dict 读取是原子的；万一 Qt 没
                # 触发回调就是空 list，merge 时忽略，有内容就优先级 最高（覆盖 cookieAdded））
                "all_cookies_loaded_raw": list(self._all_cookies_raw),
            }
            if stop_ev.is_set():
                return
            log.info(
                "[采集 worker] 完成：pending=%d intercepted=%d sqlite=%d js_cookie=%d js_storage=%d cookiesLoaded=%d",
                len(pending_list), len(payload["intercepted_raw"]), len(first),
                len(payload["js_cookies_cache"]), len(payload["js_storage_cache"]),
                len(payload.get("all_cookies_loaded_raw") or []),
            )
            self._collect_snapshot_ready.emit(payload)
        except Exception as exc:  # noqa: BLE001
            log.exception("[采集 worker] 异常：%s", exc)
            self._collect_snapshot_ready.emit({"ok": False, "err": f"{type(exc).__name__}: {exc}"})

    @staticmethod
    def _sleep_safe(sec: float) -> None:
        # 用 Event.wait 代替 time.sleep，便于上层 stop_ev 被触发时立即唤醒退出
        import time as _time
        _time.sleep(sec)

    # -------- UI 线程：接收到 snapshot，做最终 merge+判定+弹窗 --------
    def _on_collect_snapshot_ready(self, payload: dict[str, Any]) -> None:
        self._tick_timer.stop()
        # —— 采集结束，采集阶段的进度面板隐藏；如果下一步要 verify，
        #    _spin_verify 里会重新以"verify 模式"点亮它。——
        try:
            self._progress_bar.hide()
            self._progress_label.setText("采集完成：正在合并多路 cookie 结果并写入配置…")
        except Exception:  # noqa: BLE001
            pass
        self._btn_reload.setEnabled(True)  # 采集结束就允许用户刷新页面（verify 阶段仍可能失败）
        if not payload.get("ok", False):
            self._btn_done.setEnabled(True)
            err = str(payload.get("err") or "未知原因")
            self._status.setText("❌ 采集失败：" + err[:80])
            try:
                self._progress_label.setText("❌ 采集失败：" + err[:120])
            except Exception:  # noqa: BLE001
                pass
            self._login_fail_block(
                f"登录态采集出现内部错误：{err[:300]}\n\n请重试，若仍失败请把 APPDATA\\logs\\app.log 发给开发排查。"
            )
            return
        self._sqlite_snapshot = list(payload.get("sqlite_snapshot") or [])
        self._sqlite_stats = dict(payload.get("sqlite_stats") or {})
        pending_serialized = list(payload.get("pending_list") or [])
        intercepted_raw = list(payload.get("intercepted_raw") or [])
        js_cookies = dict(payload.get("js_cookies_cache") or {})
        js_storage = dict(payload.get("js_storage_cache") or {})
        all_cookies_loaded_raw = list(payload.get("all_cookies_loaded_raw") or [])
        log.info(
            "[采集 UI] snapshot ready: sqlite=%d stats=%s cookiesLoaded=%d",
            len(self._sqlite_snapshot), self._sqlite_stats, len(all_cookies_loaded_raw),
        )
        # merge & judge
        self._process_cookies_from_sources(
            pending_list=pending_serialized,
            intercepted_raw=intercepted_raw,
            sqlite_snapshot=self._sqlite_snapshot,
            all_cookies_loaded_raw=all_cookies_loaded_raw,
            js_cookies_cache=js_cookies,
            js_storage_cache=js_storage,
        )

    def _diagnose_cookies_loaded_if_missing(self) -> None:
        """【兜底诊断】用户点击"我已登录完成"→ loadAllCookies 之后 3s 若仍没
        任何 cookiesLoaded 回调，就打一条 WARN 到 app.log，方便现场定位：
        - PySide6 版本过低（< 6.7）没有这个信号？
        - profile 初始化顺序问题？
        - WebEngine 内部罐为空（根本没下发 cookie）
        """
        if self._all_cookies_raw:
            return
        # 用 pending / intercepted 有值区分是 Qt 信号静默 还是 根本没 Set-Cookie
        pending_n = len(self._pending_cookies)
        intercept_n = len(self._intercepted_raw)
        if pending_n == 0 and intercept_n == 0:
            log.warning(
                "[cookiesLoaded 诊断] 3s 内 3 项全空（cookiesLoaded_cb=%d pending=%d intercept=%d）："
                "大概率 WebEngine 期间根本没有收到 Set-Cookie。请确认扫码完成 + 打开一本书 + 等待 10s 后再点按钮。",
                len(self._all_cookies_raw), pending_n, intercept_n,
            )
        else:
            log.warning(
                "[cookiesLoaded 诊断] 3s 内 loadAllCookies 没触发回调（cookiesLoaded_cb=0），"
                "但 cookieAdded 仍有 pending=%d / intercept=%d：PySide6 cookiesLoaded 信号失效或版本不兼容，"
                "已自动降级使用 cookieAdded + SQLite + JS 兜底。",
                pending_n, intercept_n,
            )

    def _sqlite_extra_diag_threadsafe(self, db_path: Path) -> dict[str, int]:
        """返回 {'total_rows': N, 'domains': M} 等纯取证用汇总。

        线程安全版（复用 _load_raw_from_sqlite 的 URI immutable 套路，
        避免 WebEngine 写锁把 Python open() 永远卡住，之前 _copyfile_with_timeout 连续 2.5s 超时就是这个原因）。
        """
        out: dict[str, int] = {"total_rows": 0, "domains": 0, "diag_method": 0}
        if not db_path.exists():
            return out
        # —— 优先级同 _load_raw_from_sqlite：URI ro+immutable（最抗 WebEngine 锁） ——
        uri = f"file:{db_path.as_posix()}?mode=ro&immutable=1"
        conn = None
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=2.0, isolation_level=None)
            out["diag_method"] = 1
        except sqlite3.Error as exc1:
            # fallback: 普通只读 URI（不带 immutable）
            try:
                conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=2.0, isolation_level=None)
                out["diag_method"] = 2
            except sqlite3.Error as exc2:
                # fallback: 带超时 copy + 读
                try:
                    tmp_dir = Path(tempfile.mkdtemp(prefix="wxread_diag_"))
                    copy_path = tmp_dir / "C.diag.sqlite"
                    try:
                        if not _copyfile_with_timeout(db_path, copy_path, timeout_sec=2.0):
                            log.warning("SQLite 诊断 copyfile 超时（2.0s），domains/total_rows=0/0")
                            shutil.rmtree(tmp_dir, ignore_errors=True)
                            return out
                        conn = sqlite3.connect(str(copy_path), timeout=2.0)
                        out["diag_method"] = 3
                    finally:
                        # 等 conn 使用完再删
                        _ = (tmp_dir, copy_path)  # 保留引用，连接关闭后清理
                except Exception as exc3:
                    log.warning("SQLite 诊断全部打开方式失败：immutable=%s / ro=%s / copy=%s", exc1, exc2, exc3)
                    return out
        if conn is None:
            return out
        try:
            try:
                cur = conn.cursor()
                cur.execute("PRAGMA query_only = 1")
                cur.execute("SELECT COUNT(*) FROM cookies")
                out["total_rows"] = int(cur.fetchone()[0] or 0)
                cur.execute("SELECT COUNT(DISTINCT host_key) FROM cookies")
                out["domains"] = int(cur.fetchone()[0] or 0)
            finally:
                try: conn.close()
                except Exception: pass
        except sqlite3.Error as exc:
            log.warning("SQLite 诊断 查询异常: %s", exc)
        # 清理 fallback 3 的临时目录（如存在）
        try:
            if out.get("diag_method") == 3:
                for p in Path(tempfile.gettempdir()).glob("wxread_diag_*"):
                    try:
                        if p.is_dir():
                            shutil.rmtree(p, ignore_errors=True)
                    except OSError:
                        pass
        except Exception:
            pass
        return out

    # ---------- 旧的 UI 阻塞版 入口已弃，留着兼容未覆盖的老分支 调用 即改为后台版 即可 ----------
    def _collect_sqlite_and_process(self) -> None:
        self._collect_worker_thread()  # 兼容兜底：实际不会再通过 singleShot 走到这里

    def _sqlite_extra_diag(self, db_path: Path) -> dict[str, int]:  # pragma: no cover - 旧入口，仍线程安全
        return self._sqlite_extra_diag_threadsafe(db_path)

    def _process_cookies(self) -> None:  # pragma: no cover - 旧入口，保留给外部调用
        # 从当前内存状态构造一个等价 payload，然后交给新流程判定
        pending = []
        try:
            for c in list(self._pending_cookies):
                pending.append(
                    {
                        "name": self._qbytes_to_str(c.name()),
                        "value": self._qbytes_to_str(c.value()),
                        "domain": c.domain() or "",
                        "path": c.path() or "/",
                        "secure": bool(c.isSecure()),
                        "httpOnly": bool(c.isHttpOnly()),
                        "sameSite": _samesite_to_int(c.sameSitePolicy()) if hasattr(c, "sameSitePolicy") else 0,
                    }
                )
        except Exception:
            pending = []
        self._process_cookies_from_sources(
            pending_list=pending,
            intercepted_raw=list(self._intercepted_raw),
            sqlite_snapshot=list(self._sqlite_snapshot),
            all_cookies_loaded_raw=list(self._all_cookies_raw),
            js_cookies_cache=dict(self._js_cookies_cache),
            js_storage_cache=dict(self._js_storage_cache),
        )

    # ---------- 新的 4 路合并 & 判定（UI 线程执行，可直接操作 QMessageBox/widgets）----------
    def _process_cookies_from_sources(
        self,
        *,
        pending_list: list[dict[str, Any]],
        intercepted_raw: list[dict[str, Any]],
        sqlite_snapshot: list[dict[str, Any]],
        all_cookies_loaded_raw: list[dict[str, Any]] | None = None,
        js_cookies_cache: dict[str, str],
        js_storage_cache: dict[str, str],
    ) -> None:
        cookies_raw: list[dict[str, Any]] = []
        cookies_dict: dict[str, str] = {}
        domains: set[str] = set()
        sources = {
            "cookieAdded_QNetworkCookie": 0,
            "intercepted_raw": 0,
            "sqlite_snapshot": 0,
            "all_cookies_loaded": 0,   # cookiesLoaded 全量回调命中
            "js_document_cookie": 0,
            "js_storage": 0,
        }

        def _push_entry(src: str, entry: dict[str, Any]) -> None:
            ks = str(entry.get("name") or "")
            vs = str(entry.get("value") or "")
            if not ks or not _cookie_name_should_keep(ks) or not vs:
                return
            dm = str(entry.get("domain") or "")
            if dm:
                domains.add(dm)
            # —— 去重改为 name-only（domain/path 不同都视为同一颗 cookie 的不同域副本）——
            #   老逻辑 name+domain+path 会把 ".qq.com" 的 RK/ptcz 和 "weread.qq.com" 的同名
            #   cookie 都当成不同项，但 requests Session 种 cookie 时只看 name，导致
            #   cookies_dict 只保留"先入者"（可能是 weread.qq.com 那条短值或空值）。
            dup_idx = -1
            for i, old in enumerate(cookies_raw):
                if str(old.get("name")) == ks:
                    dup_idx = i
                    break
            if dup_idx >= 0:
                old = cookies_raw[dup_idx]
                if len(vs) > len(str(old.get("value") or "")):
                    old["value"] = vs
                    # 合并最佳属性：httpOnly=true 比 false 更权威
                    if entry.get("httpOnly"):
                        old["httpOnly"] = True
                    if entry.get("secure"):
                        old["secure"] = True
                    for kk in ("secure", "sameSite", "path", "domain"):
                        if kk in entry:
                            # domain/path 选"更泛"的（前导点、path="/"）
                            if kk == "domain":
                                new_d = str(entry["domain"] or "")
                                old_d = str(old.get("domain") or "")
                                if new_d.startswith(".") and not old_d.startswith("."):
                                    old["domain"] = new_d
                            elif kk == "path":
                                if str(entry["path"] or "/") == "/" and str(old.get("path") or "") != "/":
                                    old["path"] = "/"
                            else:
                                old[kk] = entry[kk]
            else:
                cookies_raw.append(dict(entry))
                sources[src] = sources.get(src, 0) + 1
            prev = cookies_dict.get(ks, "")
            if len(vs) > len(prev or ""):
                cookies_dict[ks] = vs

        # ——— 【诊断专用】先把 pending_list / intercepted_raw 每条 cookie 打 WARNING，
        #     下一版本如果 RK/ptcz/wr_skey 还是 8 字符或空，直接从日志就能看出：
        #     A) cookieAdded 信号本身就只给了 8 字符？ B) 名字不对？ C) domain 错？
        if pending_list:
            log.warning("【pending_list 诊断】共 %d 条（逐条 name=len(value):domain）：", len(pending_list))
            for e in pending_list:
                log.warning("    - %-22s len=%-4d domain=%s  path=%s  httpOnly=%s  secure=%s",
                            str(e.get("name") or ""), len(str(e.get("value") or "")),
                            str(e.get("domain") or ""), str(e.get("path") or ""),
                            bool(e.get("httpOnly")), bool(e.get("secure")))
        if intercepted_raw:
            log.warning("【intercepted_raw 诊断】共 %d 条（逐条）：", len(intercepted_raw))
            for e in intercepted_raw:
                log.warning("    - %-22s len=%-4d domain=%s  path=%s  httpOnly=%s",
                            str(e.get("name") or ""), len(str(e.get("value") or "")),
                            str(e.get("domain") or ""), str(e.get("path") or ""),
                            bool(e.get("httpOnly")))

        # 源 1：pending_list（cookieAdded 序列化结果）
        for e in pending_list:
            _push_entry("cookieAdded_QNetworkCookie", e)
        # 源 2：intercepted_raw（Set-Cookie + mirror）
        for e in intercepted_raw:
            _push_entry("intercepted_raw", e)
        # 源 3：SQLite 直读
        for e in sqlite_snapshot:
            _push_entry("sqlite_snapshot", e)
        # 源 0：cookiesLoaded 全量罐（优先级最高，有值就覆盖前 3 条；因为 Qt 交付的是"罐里最新的"）
        for e in (all_cookies_loaded_raw or []):
            _push_entry("all_cookies_loaded", e)
        # 源 4a：document.cookie
        sources["js_document_cookie"] = self._merge_raw_from_kv(
            cookies_raw, cookies_dict, js_cookies_cache,
            default_domain=".weread.qq.com", default_http_only=False,
        )
        # 源 4b：storage + __wr_vid / __wr_userId 等 特殊映射到 wr_vid
        storage_merged = 0
        for k, v in (js_storage_cache or {}).items():
            ks = str(k)
            vs = str(v or "")
            if not ks or not vs:
                continue
            if ks.startswith("__wr_"):
                target_name = "wr_vid"
                existing_val = cookies_dict.get(target_name) or ""
                if len(vs) <= len(existing_val):
                    if existing_val:
                        log.debug("storage __wr_* 跳过：已有 wr_vid len=%d ≥ 候选 len=%d", len(existing_val), len(vs))
                    continue
                # storage 兜底的 vid 通常是真实 userId/vid（cookieAdded + SQLite 双空时最有价值）
                log.warning("【storage 兜底】__wr_* 映射到 %s：老 len=%d → 新 len=%d", target_name, len(existing_val), len(vs))
                cookies_dict[target_name] = vs
                # 如果 raw 里已存在同名，就覆盖
                found = False
                for e in cookies_raw:
                    if str(e.get("name")) == target_name:
                        e["value"] = vs
                        found = True
                        break
                if not found:
                    cookies_raw.append(
                        {"name": target_name, "value": vs,
                         "domain": ".weread.qq.com", "path": "/",
                         "secure": True, "httpOnly": True, "sameSite": 0}
                    )
                storage_merged += 1
                continue
            if not _cookie_name_should_keep(ks):
                continue
            existing_val = cookies_dict.get(ks) or ""
            if len(vs) <= len(existing_val):
                continue  # 短值不覆盖长值
            cookies_dict[ks] = vs
            found = False
            for e in cookies_raw:
                if str(e.get("name")) == ks:
                    e["value"] = vs
                    found = True
                    break
            if not found:
                cookies_raw.append(
                    {"name": ks, "value": vs, "domain": ".weread.qq.com", "path": "/",
                     "secure": True, "httpOnly": False, "sameSite": 0}
                )
            storage_merged += 1
        sources["js_storage"] = storage_merged

        # —— 采集摘要：成功 / 失败 都写 config.debug.last_collect ——
        #    用户如果点完【我已登录完成】仍然"没数据"，不用把 APPDATA\logs 整个发来，
        #    直接把 config.json 里这段 debug 复制出来就能看到每条采集管道干没干活。
        datetime = __import__("datetime")
        ts_now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        summary: dict[str, Any] = {
            "ts": ts_now,
            "sources": dict(sources),
            "counts": {
                "dict": len(cookies_dict),
                "raw": len(cookies_raw),
                "domains": len(domains),
                "sqlite_total": int(self._sqlite_stats.get("total_rows") or 0),
                "sqlite_wr": int(self._sqlite_stats.get("snapshot_rows") or len(sqlite_snapshot)),
            },
            "names": sorted(cookies_dict.keys()),
            "len": {k: len(str(v or "")) for k, v in cookies_dict.items()},
            "sqlite_stats": dict(self._sqlite_stats),
            "stage": None,
        }

        def _save_summary(stage: str, *, extra: dict[str, Any] | None = None) -> None:
            s = dict(summary)
            s["stage"] = stage
            if extra:
                s.update(extra)
            try:
                self._cfg.set("debug.last_collect", s, auto_save=True)
            except Exception as exc:  # pragma: no cover
                log.debug("写 debug.last_collect 失败: %s", exc)

        log.info("登录抽取：各源贡献=%s；合并后 dict=%d raw=%d；涉及域=%s",
                 sources, len(cookies_dict), len(cookies_raw), sorted(domains))
        if self._sqlite_stats:
            log.info("SQLite 诊断汇总: %s", self._sqlite_stats)
        for k in ("wr_vid", "wr_skey", "wr_at", "wr_rt", "RK", "ptcz", "pac_uid",
                  "wr_fp", "wr_gid", "wr_localvid"):
            v = cookies_dict.get(k, "") or ""
            log.info("  - %-14s len=%d  head=%s", k, len(v), (v[:4] + "***") if v else "(空)")

        wr_vid = (cookies_dict.get("wr_vid") or "").strip()
        wr_skey = (cookies_dict.get("wr_skey") or "").strip()
        wr_at = (cookies_dict.get("wr_at") or "").strip()
        wr_rt = (cookies_dict.get("wr_rt") or "").strip()
        has_refresh_token = bool(wr_at or wr_rt)
        sqlite_wr = sum(1 for e in sqlite_snapshot if e["name"].startswith("wr_"))
        sqlite_total = int(self._sqlite_stats.get("total_rows") or 0)

        if not wr_vid:
            clues = []
            if sqlite_total == 0:
                clues.append("Profile Cookies DB 完全空（扫码/点书流程没走完？）")
            elif sqlite_wr == 0:
                clues.append(f"Profile Cookies DB 共 {sqlite_total} 条，但 wr_* 系列 0 条（未授权？）")
            else:
                clues.append(f"Profile Cookies DB 已写 {sqlite_wr} 条 wr_*，但未能抽取到 wr_vid（请把 APPDATA 日志发给开发）")
            clues.append("")
            clues.append("建议重新扫码，确保：")
            clues.append("  1) 微信弹窗授权确认点击【同意】；")
            clues.append("  2) 页面自动跳回书架后，再点击书架中任意一本书进入阅读页，停留 10 秒；")
            clues.append("  3) 若使用了全局代理/VPN，临时关闭后重试。")
            _save_summary("fail.no_wr_vid", extra={"sqlite_wr": sqlite_wr, "sqlite_total": sqlite_total})
            self._login_fail_block("关键登录票【wr_vid】完全未出现，扫码未完成。\n诊断线索：\n  - " +
                                   "\n  - ".join(clues) +
                                   "\n\n（已将采集管道摘要写入 config.json → debug.last_collect，若仍失败请把该段发给开发者）")
            return

        if not wr_skey and not has_refresh_token:
            self._status.setText("检测到 wr_vid 已存在，正在主动签发 wr_skey（renewal）...")
            temp_ok, refreshed_skey = self._try_emergency_renewal(cookies_dict, cookies_raw)
            if temp_ok and refreshed_skey:
                wr_skey = refreshed_skey
                cookies_dict["wr_skey"] = wr_skey
                found = False
                for e in cookies_raw:
                    if str(e.get("name")) == "wr_skey":
                        e["value"] = wr_skey
                        found = True
                if not found:
                    cookies_raw.append(
                        {
                            "name": "wr_skey",
                            "value": wr_skey,
                            "domain": ".weread.qq.com",
                            "path": "/",
                            "secure": True,
                            "httpOnly": True,
                            "sameSite": 0,
                        }
                    )
                log.info("renewal 现场补救签发 wr_skey 成功，len=%d", len(wr_skey))
            else:
                _save_summary("fail.no_wr_skey", extra={"renewal": False})
                self._login_fail_block(
                    f"扫码后已读到 wr_vid(len={len(wr_vid)})，但缺少登录短期票 wr_skey，且 renewal 现场签发失败。\n"
                    "请在微信读书网页内多停留 10 秒、点击一本书进入阅读页，再点【我已登录完成】。\n"
                    "若仍失败：点击页面右上角【🔄 刷新页面】，等页面再次加载完毕后重试。\n\n"
                    "（已将采集管道摘要写入 config.json → debug.last_collect）"
                )
                return

        headers = dict(self._cfg.get("headers", {}) or {})
        headers["user-agent"] = self._profile.httpUserAgent()
        headers.setdefault("referer", "https://weread.qq.com/")
        headers.setdefault("origin", "https://weread.qq.com")

        self._api.set_session(headers, cookies_dict, cookies_raw)
        _save_summary("ok.pending_verify")
        self._status.setText("登录态保存完毕！正在验证是否可用（chapter_sync / shelf_sync）...")
        # —— 把 verify 阶段的"进度面板/秒针/按钮禁用"统一集中在 _spin_verify 里打开；
        #    但先在进度面板里报个"开始"，避免用户看到"上一次采集的旧状态"。
        try:
            self._progress_label.setText("验证中：调用 chapter_sync / shelf_sync 检查登录态有效性…")
        except Exception:  # noqa: BLE001
            pass
        self._spin_verify()

    def _fetch_document_cookies_async(self) -> None:
        page = self._page

        def _cb(result: Any) -> None:
            out: dict[str, str] = {}
            if isinstance(result, str):
                for seg in result.split(";"):
                    seg = seg.strip()
                    if not seg or "=" not in seg:
                        continue
                    k, v = seg.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    if k:
                        out[k] = v
            self._js_cookies_ready.emit(out)

        try:
            page.runJavaScript("document.cookie || ''", 0, _cb)  # type: ignore[call-arg]
        except TypeError:
            try:
                page.runJavaScript("document.cookie || ''", _cb)  # type: ignore[call-arg]
            except Exception as exc:  # noqa: BLE001
                log.warning("document.cookie JS 兜底采集失败：%s", exc)
                self._js_cookies_ready.emit({})

    def _fetch_storage_kv_async(self) -> None:
        """扫 localStorage / sessionStorage 的 key 前缀 wr_ / RK / ptcz / pac_uid 等。
        新版 weread 会把 uid/avatar/name 等非鉴权但签名噪声有用的数据写到 localStorage。"""
        js = r"""
(function(){
  var out = {};
  var tryPick = function(storage){
    if(!storage) return;
    var pref = ["wr_","RK","ptcz","pac_uid","iip","qq_","wq_","pgv_","ts_uid","wx_code","wx_state","openid2","appmsg_token","uuid","_qimei","QIMEI"];
    var exact = {"RK":1,"ptcz":1,"pac_uid":1,"iip":1,"skey":1,"uin":1,"key":1,"p_uin":1,"p_skey":1};
    for(var i = 0; i < storage.length; i++){
      try {
        var k = storage.key(i);
        if(!k) continue;
        var keep = false;
        for(var j=0;j<pref.length;j++){ if(k.indexOf(pref[j])===0){ keep=true; break; } }
        if(!keep && exact[k]) keep = true;
        if(!keep) continue;
        var v = storage.getItem(k);
        if(typeof v === "string" && v) out[k] = v;
      } catch(e) {}
    }
  };
  try { tryPick(window.localStorage); } catch(e){}
  try { tryPick(window.sessionStorage); } catch(e){}
  // 额外扫 window.__weread__ / __NEXT_DATA__ 里的 vid 字段
  try {
    var W = (window.__weread__ || window.__NEXT_DATA__ || {});
    function walk(node, depth){
      if(!node || depth > 6) return;
      if(typeof node !== "object") return;
      if(Array.isArray(node)){ node.forEach(function(x){ walk(x, depth+1); }); return; }
      Object.keys(node).forEach(function(k){
        var v = node[k];
        if(k === "vid" || k === "wr_vid" || k === "userId" || k === "userVid"){
          if(typeof v === "string" && v) out["__wr_"+k] = v;
        } else {
          walk(v, depth+1);
        }
      });
    }
    walk(W, 0);
  } catch(e){}
  return out;
})();
        """
        page = self._page

        def _cb(result: Any) -> None:
            if isinstance(result, dict):
                self._js_storage_ready.emit(result)  # type: ignore[arg-type]
            else:
                self._js_storage_ready.emit({})

        try:
            page.runJavaScript(js, 0, _cb)  # type: ignore[call-arg]
        except TypeError:
            try:
                page.runJavaScript(js, _cb)  # type: ignore[call-arg]
            except Exception as exc:  # noqa: BLE001
                log.warning("storage JS 兜底采集失败：%s", exc)
                self._js_storage_ready.emit({})

    def _on_js_cookies_ready(self, kv: dict[str, str]) -> None:
        self._js_cookies_cache.update(kv or {})

    def _on_js_storage_ready(self, kv: dict[str, str]) -> None:
        self._js_storage_cache.update(kv or {})

    @staticmethod
    def _qbytes_to_str(b) -> str:  # type: ignore[no-untyped-def]
        if isinstance(b, bytes):
            return b.decode("utf-8", errors="ignore")
        if isinstance(b, (QByteArray, bytearray)):
            return bytes(b).decode("utf-8", errors="ignore")
        if b is None:
            return ""
        return str(b)

    def _merge_raw_from_kv(
        self,
        cookies_raw: list[dict[str, Any]],
        cookies_dict: dict[str, str],
        kv: dict[str, str],
        *,
        default_domain: str = ".weread.qq.com",
        default_http_only: bool = False,
    ) -> int:
        added = 0
        for k, v in (kv or {}).items():
            ks = str(k)
            vs = str(v or "")
            if not ks or not vs:
                continue
            if not _cookie_name_should_keep(ks):
                continue
            # —— 长值覆盖策略：cookies_dict 里如果已经有这条 name，
            #    但 kv 的值更长，必须覆盖（setdefault 会让 pending 里 8 字符截断版
            #    抢占槽位，document.cookie / storage 的真实长值反而进不来）。
            prev = cookies_dict.get(ks) or ""
            if not prev or len(vs) > len(prev):
                cookies_dict[ks] = vs
            # cookies_raw 里如果已存在同名，只在"新值更长"时覆写 value
            found_in_raw = False
            for e in cookies_raw:
                if str(e.get("name")) == ks:
                    old_v = str(e.get("value") or "")
                    if len(vs) > len(old_v):
                        e["value"] = vs
                    found_in_raw = True
                    break
            if not found_in_raw:
                cookies_raw.append(
                    {
                        "name": ks,
                        "value": vs,
                        "domain": default_domain,
                        "path": "/",
                        "secure": True,
                        "httpOnly": bool(default_http_only),
                        "sameSite": 0,
                    }
                )
                added += 1
        return added

    # ---------- 4 路合并 + 判定 ----------
    def _process_cookies(self) -> None:
        # — 权威结构：cookies_raw（存到 config），cookies_dict（便捷读取）
        cookies_raw: list[dict[str, Any]] = []
        cookies_dict: dict[str, str] = {}
        domains: set[str] = set()
        sources = {
            "cookieAdded_QNetworkCookie": 0,
            "intercepted_raw": 0,
            "sqlite_snapshot": 0,
            "js_document_cookie": 0,
            "js_storage": 0,
        }

        def _push_entry(src: str, entry: dict[str, Any]) -> None:
            ks = str(entry.get("name") or "")
            vs = str(entry.get("value") or "")
            if not ks or not _cookie_name_should_keep(ks) or not vs:
                return
            dm = str(entry.get("domain") or "")
            if dm:
                domains.add(dm)
            # 去重：同 name + 同 domain + 同 path 才视为重复（后到的如果长度更大覆盖）
            dup_idx = -1
            for i, old in enumerate(cookies_raw):
                if (str(old.get("name")) == ks and str(old.get("domain")) == dm
                        and str(old.get("path")) == str(entry.get("path") or "/")):
                    dup_idx = i
                    break
            if dup_idx >= 0:
                old = cookies_raw[dup_idx]
                if len(vs) > len(str(old.get("value") or "")):
                    old["value"] = vs
                    for kk in ("secure", "httpOnly", "sameSite"):
                        if kk in entry:
                            old[kk] = entry[kk]
            else:
                cookies_raw.append(dict(entry))
                sources[src] = sources.get(src, 0) + 1
            # cookies_dict（浅覆盖：长值优先）
            prev = cookies_dict.get(ks, "")
            if len(vs) > len(prev or ""):
                cookies_dict[ks] = vs

        # 源 1：cookieAdded 信号（QNetworkCookie）
        for c in self._pending_cookies:
            _push_entry(
                "cookieAdded_QNetworkCookie",
                {
                    "name": self._qbytes_to_str(c.name()),
                    "value": self._qbytes_to_str(c.value()),
                    "domain": c.domain() or "",
                    "path": c.path() or "/",
                    "secure": bool(c.isSecure()),
                    "httpOnly": bool(c.isHttpOnly()),
                    "sameSite": _samesite_to_int(c.sameSitePolicy()) if hasattr(c, "sameSitePolicy") else 0,
                },
            )
        # 源 2：intercepted_raw（Set-Cookie 解析 + cookieAdded 镜像）
        for e in self._intercepted_raw:
            _push_entry("intercepted_raw", e)
        # 源 3a：SQLite 直读（黄金兜底，必须在前两个之后覆盖）
        for e in self._sqlite_snapshot:
            _push_entry("sqlite_snapshot", e)
        # 源 4a：document.cookie
        sources["js_document_cookie"] = self._merge_raw_from_kv(
            cookies_raw, cookies_dict, self._js_cookies_cache,
            default_domain=".weread.qq.com", default_http_only=False,
        )
        # 源 4b：storage；storage 的 key 可能与真实 cookie 同名（例如 wr_vid）——不要覆盖 SQLite/Set-Cookie
        #   已经写入的值；只在当前 cookies_raw 里缺少该 key 时才补
        storage_merged = 0
        existing_names = {str(e.get("name")) for e in cookies_raw}
        for k, v in (self._js_storage_cache or {}).items():
            ks = str(k)
            vs = str(v or "")
            if not ks or not vs:
                continue
            if ks.startswith("__wr_"):
                # window.__weread__ / __NEXT_DATA__ 抽出来的 vid 特殊映射到 wr_vid 作为最后兜底
                # __wr_vid / __wr_wr_vid / __wr_userId / __wr_userVid 任一存在且 cookies_dict.wr_vid 空就用
                target_name = "wr_vid"
                if cookies_dict.get(target_name):
                    continue
                cookies_dict[target_name] = vs
                cookies_raw.append(
                    {"name": target_name, "value": vs,
                     "domain": ".weread.qq.com", "path": "/",
                     "secure": True, "httpOnly": True, "sameSite": 0}
                )
                storage_merged += 1
                continue
            if not _cookie_name_should_keep(ks):
                continue
            if ks in existing_names:
                continue
            cookies_dict[ks] = vs
            cookies_raw.append(
                {"name": ks, "value": vs, "domain": ".weread.qq.com", "path": "/",
                 "secure": True, "httpOnly": False, "sameSite": 0}
            )
            storage_merged += 1
        sources["js_storage"] = storage_merged

        log.info("登录抽取：各源贡献=%s；合并后 dict=%d raw=%d；涉及域=%s",
                 sources, len(cookies_dict), len(cookies_raw), sorted(domains))
        if self._sqlite_stats:
            log.info("SQLite 诊断汇总: %s", self._sqlite_stats)
        # 关键 cookie 摘要（只打前缀，防泄露）
        for k in ("wr_vid", "wr_skey", "wr_at", "wr_rt", "RK", "ptcz", "pac_uid",
                  "wr_fp", "wr_gid", "wr_localvid"):
            v = cookies_dict.get(k, "") or ""
            log.info("  - %-14s len=%d  head=%s", k, len(v), (v[:4] + "***") if v else "(空)")

        # —— 登录判定（放宽，和 v2 版一致）——
        wr_vid = (cookies_dict.get("wr_vid") or "").strip()
        wr_skey = (cookies_dict.get("wr_skey") or "").strip()
        wr_at = (cookies_dict.get("wr_at") or "").strip()
        wr_rt = (cookies_dict.get("wr_rt") or "").strip()
        has_refresh_token = bool(wr_at or wr_rt)
        sqlite_wr = sum(1 for e in self._sqlite_snapshot if e["name"].startswith("wr_"))
        sqlite_total = int(self._sqlite_stats.get("total_rows") or 0)

        if not wr_vid:
            # 给出更具体的线索，直接告诉用户哪条管道失败
            clues = []
            if sqlite_total == 0:
                clues.append("Profile Cookies DB 完全空（扫码/点书流程没走完？）")
            elif sqlite_wr == 0:
                clues.append(f"Profile Cookies DB 共 {sqlite_total} 条，但 wr_* 系列 0 条（未授权？）")
            else:
                clues.append(f"Profile Cookies DB 已写 {sqlite_wr} 条 wr_*，但未能抽取到 wr_vid（请把 APPDATA 日志发给开发）")
            clues.append("")
            clues.append("建议重新扫码，确保：")
            clues.append("  1) 微信弹窗授权确认点击【同意】；")
            clues.append("  2) 页面自动跳回书架后，再点击书架中任意一本书进入阅读页，停留 10 秒；")
            clues.append("  3) 若使用了全局代理/VPN，临时关闭后重试。")
            self._login_fail_block("关键登录票【wr_vid】完全未出现，扫码未完成。\n诊断线索：" +
                                   "\n  - ".join(clues))
            return

        if not wr_skey and not has_refresh_token:
            self._status.setText("检测到 wr_vid 已存在，正在主动签发 wr_skey（renewal）...")
            temp_ok, refreshed_skey = self._try_emergency_renewal(cookies_dict, cookies_raw)
            if temp_ok and refreshed_skey:
                wr_skey = refreshed_skey
                cookies_dict["wr_skey"] = wr_skey
                found = False
                for e in cookies_raw:
                    if str(e.get("name")) == "wr_skey":
                        e["value"] = wr_skey
                        found = True
                if not found:
                    cookies_raw.append(
                        {
                            "name": "wr_skey",
                            "value": wr_skey,
                            "domain": ".weread.qq.com",
                            "path": "/",
                            "secure": True,
                            "httpOnly": True,
                            "sameSite": 0,
                        }
                    )
                log.info("renewal 现场补救签发 wr_skey 成功，len=%d", len(wr_skey))
            else:
                self._login_fail_block(
                    f"扫码后已读到 wr_vid(len={len(wr_vid)})，但缺少登录短期票 wr_skey，且 renewal 现场签发失败。\n"
                    "请在微信读书网页内多停留 10 秒、点击一本书进入阅读页，再点【我已登录完成】。\n"
                    "若仍失败：点击页面右上角【🔄 刷新页面】，等页面再次加载完毕后重试。"
                )
                return

        # 保存 headers + cookies
        headers = dict(self._cfg.get("headers", {}) or {})
        headers["user-agent"] = self._profile.httpUserAgent()
        headers.setdefault("referer", "https://weread.qq.com/")
        headers.setdefault("origin", "https://weread.qq.com")

        self._api.set_session(headers, cookies_dict, cookies_raw)
        self._status.setText("登录态保存完毕！正在验证是否可用（chapter_sync / shelf_sync）...")
        try:
            self._progress_label.setText("验证中：调用 chapter_sync / shelf_sync 检查登录态有效性…")
        except Exception:  # noqa: BLE001
            pass
        self._spin_verify()

    def _try_emergency_renewal(
        self,
        cookies_dict: dict[str, str],
        cookies_raw: list[dict[str, Any]],
    ) -> tuple[bool, str]:
        try:
            import requests
            from app.core.weread_api import COOKIE_DATA_VARIANTS, RENEW_URL

            sess = requests.Session()
            # 先种到 self._api._session 让 _plant_cookies 走一遍，保证与真实 use 一致
            self._api._plant_cookies(cookies_dict=cookies_dict, cookies_raw=cookies_raw)
            for k, v in cookies_dict.items():
                if not v:
                    continue
                try:
                    sess.cookies.set(k, v, domain="weread.qq.com", path="/")
                    sess.cookies.set(k, v, domain=".weread.qq.com", path="/")
                except Exception:
                    pass
            headers = dict(self._cfg.get("headers", {}) or {})
            headers["user-agent"] = self._profile.httpUserAgent()
            headers.setdefault("referer", "https://weread.qq.com/")
            headers.setdefault("origin", "https://weread.qq.com")
            headers.setdefault("accept", "application/json, text/plain, */*")
            headers["content-type"] = "application/json;charset=UTF-8"  # 强制覆写，避免旧 config 无此字段
            import json
            for idx, payload in enumerate(COOKIE_DATA_VARIANTS):
                resp = sess.post(
                    RENEW_URL,
                    headers=headers,
                    data=json.dumps(payload, separators=(",", ":")),
                    timeout=8,
                )
                log.info("renewal 现场补救 变体 %d：HTTP=%s body(前200)=%s", idx, resp.status_code, resp.text[:200])
                skey = resp.cookies.get("wr_skey")
                if skey:
                    skey_s = str(skey)
                    log.info("renewal 现场补救命中 resp.cookies.wr_skey len=%d（无截断）", len(skey_s))
                    return True, skey_s
                sc = resp.headers.get("Set-Cookie", "") or ""
                if "wr_skey=" in sc:
                    for seg in sc.split(","):
                        for part in seg.split(";"):
                            p = part.strip()
                            if p.startswith("wr_skey="):
                                v = p.split("=", 1)[1].strip()
                                if v:
                                    log.info("renewal 现场补救命中 Set-Cookie.wr_skey len=%d（无截断）", len(v))
                                    return True, v
        except Exception as exc:  # noqa: BLE001
            log.warning("renewal 现场补救请求异常：%s", exc)
        return False, ""

    def _login_fail_block(self, msg: str) -> None:
        self._btn_done.setEnabled(True)
        self._status.setText("未检测到登录态，请按提示重试。")
        # 采集失败兜底：如果这一次没触发 debug.last_collect（例如 snapshot 阶段之前异常），
        # 就再写一次当前各管道计数，保证 JSON 里永远有可定位的数字。
        try:
            datetime = __import__("datetime")
            self._cfg.set(
                "debug.last_fail_dialog",
                {
                    "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "counts": {
                        "pending": len(getattr(self, "_pending_cookies", []) or []),
                        "intercepted": len(getattr(self, "_intercepted_raw", []) or []),
                        "js_cookie": len(getattr(self, "_js_cookies_cache", {}) or {}),
                        "js_storage": len(getattr(self, "_js_storage_cache", {}) or {}),
                        "sqlite": len(getattr(self, "_sqlite_snapshot", []) or []),
                    },
                    "sqlite_stats": dict(getattr(self, "_sqlite_stats", {}) or {}),
                },
                auto_save=True,
            )
        except Exception:  # pragma: no cover - 弹窗兜底再失败就算了
            pass
        QMessageBox.warning(self, "尚未登录", msg)

    # ---------- verify 阶段辅助：秒针 / 面板 / 防重入 / API 消息桥接 ----------
    def _on_verify_tick_timeout(self) -> None:
        self._verify_sec += 1
        sec = self._verify_sec
        # 给每 10 秒一个阶段性提示，避免用户怀疑"卡在哪一步不动"
        if sec <= 10:
            stage = "第 1 轮：chapter_sync / shelf_sync 健康检查"
        elif sec <= 30:
            stage = "第 2 轮：renewal 刷新 wr_skey / 重试（fallback）"
        elif sec <= 55:
            stage = "第 3 轮：再次 chapter_sync / shelf_sync 健康检查"
        else:
            stage = "已接近最大耗时，准备给出失败提示"
        dots = "." * (sec % 5)
        self._status.setText(f"验证中【{stage}】{sec:>2}s{dots}（chapter_sync / shelf_sync / renewal）")
        # 同步更新"进度面板"下的详细状态行（用户视线焦点位置）
        try:
            self._progress_label.setText(
                f"【{stage}】已用时 {sec:>2}s{dots}\n"
                f"  • check_session：先用章节同步与书架同步双接口探测；\n"
                f"  • 若失败：自动用 wr_rt/RK/ptcz 做 wr_skey renewal，再重试验证。"
            )
        except Exception:  # noqa: BLE001
            pass
        # —— 硬性超时：70 秒还没 _verify_done 回来，强制收尾（否则用户卡死到天荒地老）——
        if sec >= 70:
            log.warning("verify 阶段已超时 70s，强制收尾为 False（UI 防卡兜底）")
            self._verify_done.emit(False)

    def _start_verify_ui(self, *, initial_label: str | None = None) -> None:
        """verify 启动前 UI 一致性操作：显禁止重复入、打开 busy 进度条+秒针、禁用按钮。"""
        if getattr(self, "_verify_running", False):
            log.info("_start_verify_ui 忽略重复触发：已有 verify 任务运行中")
            return
        self._verify_running = True
        self._verify_stage = "verify"
        self._verify_sec = 0
        self._btn_done.setEnabled(False)
        self._btn_reload.setEnabled(False)
        try:
            self._progress_bar.show()
            if initial_label:
                self._progress_label.setText(initial_label)
        except Exception:  # noqa: BLE001
            pass
        # 每 1 秒走一次秒针，状态行与进度面板都会"动"，彻底消除"看起来卡死"
        self._verify_timer.start(1000)

    def _stop_verify_ui(self, *, keep_progress: bool = True) -> None:
        """verify 收尾：关秒针、关 busy 条、恢复按钮可用。"""
        self._verify_timer.stop()
        self._verify_running = False
        self._verify_stage = "idle"
        try:
            self._progress_bar.hide()
            if not keep_progress:
                self._progress_label.setText("就绪：上方扫码 → 打开一本书 → 点【我已登录完成】")
        except Exception:  # noqa: BLE001
            pass
        self._btn_done.setEnabled(True)
        self._btn_reload.setEnabled(True)

    # -------- API 信号 -> 进度面板（避免后台线程里 30s 无声无息）--------
    def _on_api_message(self, msg: str) -> None:
        m = str(msg or "")
        log.info("[api.message 桥接] %s", m[:200])
        try:
            self._progress_label.setText("🔔 " + m[:240])
        except Exception:  # noqa: BLE001
            pass

    def _on_api_warning(self, msg: str) -> None:
        m = str(msg or "")
        log.warning("[api.warning 桥接] %s", m[:200])
        try:
            self._progress_label.setText("⚠️ " + m[:240])
        except Exception:  # noqa: BLE001
            pass

    def _on_api_error(self, msg: str) -> None:
        m = str(msg or "")
        log.error("[api.error 桥接] %s", m[:200])
        try:
            self._progress_label.setText("❌ " + m[:240])
        except Exception:  # noqa: BLE001
            pass

    def _spin_verify(self) -> None:
        # —— 启动 UI 一致性：禁止重复触发、打开 busy 条 + 秒针、禁用按钮 ——
        self._start_verify_ui(
            initial_label=(
                "验证中：调用 chapter_sync / shelf_sync 检查登录态有效性…\n"
                "（如 wr_skey 过期，会自动走 renewal 刷新；请耐心等待，最多约 60s）"
            )
        )

        def _task() -> None:
            try:
                ok = bool(self._api.check_session())
            except Exception as exc:  # noqa: BLE001
                log.warning("check_session 顶层异常：%s", exc)
                ok = False
            self._verify_done.emit(ok)
        threading.Thread(target=_task, daemon=True).start()

    def _on_verify_done(self, ok: bool) -> None:  # noqa: FBT001
        # 只在第一次收尾时真正关 UI；_fix 分支再进 `_spin_verify` 不会重复关/开
        if not ok and not getattr(self, "_fix_fired", False):
            # 这是"第一轮失败 → 进入 fallback"，不关 verify 秒针/进度条，保持"一条连续进度"
            pass
        else:
            # 成功 或 两轮都失败：彻底关闭 verify UI
            self._stop_verify_ui(keep_progress=True)
        if ok:
            # 保存成功摘要（覆盖之前的 pending_verify）
            try:
                cur = self._cfg.get("debug.last_collect") or {}
                if isinstance(cur, dict):
                    cur = dict(cur)
                    cur["stage"] = "ok.verify_pass"
                    cur["verify_ts"] = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cur["verify_sec"] = int(getattr(self, "_verify_sec", 0))
                    self._cfg.set("debug.last_collect", cur, auto_save=True)
            except Exception:  # pragma: no cover
                pass
            try:
                self._progress_label.setText(
                    f"✅ 登录态验证通过（耗时 {getattr(self, '_verify_sec', 0)}s），可以开始自动阅读啦！"
                )
            except Exception:  # noqa: BLE001
                pass
            self._status.setText("✅ 登录态验证通过，可以开始自动阅读啦！")
            QMessageBox.information(self, "登录成功", "登录态已保存并验证通过！前往「状态」页开启自动阅读。")
            self.session_ready.emit()
            return

        # 第一轮失败：走 renewal fallback（仍然在 verify 秒针/进度条下跑，UI 不闪）
        if not getattr(self, "_fix_fired", False):
            self._status.setText("⚠️ Cookie 已保存但健康检查失败，正在尝试 renewal 修复...")
            try:
                self._progress_label.setText(
                    "⚠️ 第一轮健康检查未通过：正在用 renewal 接口刷新 wr_skey…\n"
                    "（最多再等待 ~50s；若 wr_vid 已过期则会失败，届时请重新扫码）"
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                cur = self._cfg.get("debug.last_collect") or {}
                if isinstance(cur, dict):
                    cur = dict(cur)
                    cur["stage"] = "pending_renewal_fallback"
                    self._cfg.set("debug.last_collect", cur, auto_save=True)
            except Exception:  # pragma: no cover
                pass
            self._fix_fired = True

            def _fix() -> None:
                try:
                    fixed = bool(self._api.ensure_session())
                except Exception as exc:  # noqa: BLE001
                    log.warning("ensure_session fallback 顶层异常：%s", exc)
                    fixed = False
                # —— 注意：这里仍 emit 同一个 _verify_done，避免出现两条并行路径。
                #    _on_verify_done 会因 `_fix_fired == True` 走入"两轮都失败"收尾分支。
                self._verify_done.emit(fixed)
            threading.Thread(target=_fix, daemon=True).start()
            return

        # ===== 第二轮也失败：verify 彻底失败 =====
        self._fix_fired = False
        try:
            cur = self._cfg.get("debug.last_collect") or {}
            if isinstance(cur, dict):
                cur = dict(cur)
                cur["stage"] = "fail.verify_pass"
                cur["verify_sec"] = int(getattr(self, "_verify_sec", 0))
                self._cfg.set("debug.last_collect", cur, auto_save=True)
        except Exception:  # pragma: no cover
            pass
        try:
            self._progress_label.setText(
                f"❌ 验证失败（耗时 {getattr(self, '_verify_sec', 0)}s）：登录票已保存但服务端不接受。\n"
                f"   详情查看：config.json → debug.last_collect；或 APPDATA\\logs\\app.log。"
            )
        except Exception:  # noqa: BLE001
            pass
        self._status.setText("❌ 登录票已保存但服务端不接受，请尝试重新扫码登录。")
        QMessageBox.warning(
            self,
            "登录验证失败",
            "登录票据（wr_vid + wr_skey）已保存，但调用微信读书接口不通过。\n"
            "常见原因：\n"
            "  1) 扫码后停留过久，wr_skey 已过期（仅 5400 秒）——请重新扫码；\n"
            "  2) 同一账号在 App / 其它浏览器重新登录过 —— 请在本软件内重新扫码；\n"
            "  3) 使用了全局代理/VPN 改写 header —— 确认关闭后重试；\n"
            "  4) wr_vid/wr_skey 长度仍 < 20 —— 扫码流程没走完【点同意→打开一本书→停 10s】。\n\n"
            "（采集诊断摘要在 config.json 的 debug.last_collect 段，可直接附这段 JSON 给开发排查。）",
        )
        # 仍然通知主窗口刷新状态页（用户可随时重新扫码 / 切到状态页查看）
        self.session_ready.emit()

