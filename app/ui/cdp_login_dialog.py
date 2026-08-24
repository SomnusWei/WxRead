"""CDP 扫码登录对话框：通过 Chrome DevTools Protocol 获取 Cookie、书架和章节池。

工作流：
  1. QWebEngineView 加载 weread.qq.com → 用户扫码
  2. 后台线程通过 CDP WebSocket 连接浏览器
  3. 调用 Network.getCookies 获取全量 Cookie
  4. 校验 wr_skey 长度 ≥ 8
  5. 持久化 Cookie → 拉书架 → 构建章节池
  6. 发射 login_success 信号，通知主程序

日志：
  使用项目统一的 get_logger("wxread.ui.cdp_login_dialog") 输出到 app.log
  关键节点均带 INFO/DEBUG/WARNING/ERROR 级别日志，便于排查。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import random
import socket
import struct
import threading
import time
import traceback
from typing import Any

import requests
import websocket
from PySide6.QtCore import QObject, QThread, QTimer, Signal, QUrl, Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.config import ConfigStore
from app.core.weread_api import (
    BOOKLIST_URL,
    CHAPTER_SYNC_URL,
    WeReadApi,
)
from app.core.notifier import WxPusherNotifier
from app.utils.logger import get_logger

log = get_logger("wxread.ui.cdp_login_dialog")

# —— CDP 端口：用 9223 避免与 test_extension 的 9222 冲突 ——
CDP_PORT = 9223
WEREAD_URL = "https://weread.qq.com/"
KEY = "3c5c8717f3daf09iop3423zafeqoi"

# —— Skill 1.0.5 接口 ——
SKILL_GATEWAY_URL = "https://i.weread.qq.com/api/agent/gateway"

# Chromium flags：启用 CDP
_chromium_flags = f"--remote-debugging-port={CDP_PORT} --disable-features=ExtensionsBrowserActivity,DesktopCaptureNotifications"
_existing_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
    f"{_existing_flags} {_chromium_flags}".strip()
    if _existing_flags
    else _chromium_flags
)

log.info("=" * 60)
log.info("CDP 登录对话框模块加载")
log.info("CDP 端口: %d", CDP_PORT)
log.info("Chromium flags: %s", _chromium_flags)
log.info("websocket-client 版本: %s", websocket.__version__)
log.info("requests 版本: %s", requests.__version__)
log.info("QTWEBENGINE_CHROMIUM_FLAGS: %s", os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", ""))
log.info("=" * 60)


# ============================================================================
# 辅助函数
# ============================================================================
def _encode_data(data: dict) -> str:
    """微信读书请求体编码（压缩 JSON 并特殊处理）。"""
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    # Python 字符串直接返回，不需要 JS 的 charCodeAt 转换
    return raw


def cal_hash(data_str: str) -> str:
    """微信读书请求体签名：md5(data_str)."""
    return hashlib.md5(data_str.encode("utf-8")).hexdigest()


def _shorten(s: str, n: int = 8) -> str:
    """截断字符串用于日志显示。"""
    if not s:
        return "(empty)"
    if len(s) <= n * 2:
        return s
    return s[:n] + "***" + s[-4:]


def _mask_cookie_value(value: str) -> str:
    """Cookie 值脱敏显示。"""
    if not value:
        return "(empty)"
    if len(value) <= 4:
        return f"len={len(value)}"
    return f"{value[:4]}*** (len={len(value)})"


# ============================================================================
# CDP 连接与 Cookie 获取（后台线程）
# ============================================================================
class CDPWorker(QObject):
    """后台线程：连接 Chrome DevTools Protocol，轮询获取 Cookie。

    信号：
      connected(dict)     → CDP 连接成功，payload={port, browser_info}
      cookies_detected(list) → 检测到 Cookie（每次检测都会发）
      login_success(dict)  → 检测到有效登录态 → Cookie 列表
      login_timeout()      → 超时，仍未检测到登录态
      error(str)           → 发生错误
      log_msg(str)         → 日志消息（转发给 UI）
    """

    connected = Signal(dict)
    cookies_detected = Signal(list)
    login_success = Signal(dict)
    login_timeout = Signal()
    error = Signal(str)
    log_msg = Signal(str)

    def __init__(self, port: int = CDP_PORT, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._port = port
        self._ws: websocket.WebSocket | None = None
        self._connected_cdp = False
        self._msg_id = 0
        self._stop_event = threading.Event()
        self._poll_interval = 2.0  # Cookie 轮询间隔（秒）
        self._timeout = 180.0  # 总超时（秒）
        self._last_cookie_count = 0

        log.info("[CDPWorker] 初始化：port=%d, poll_interval=%.1fs, timeout=%.1fs",
                 port, self._poll_interval, self._timeout)

    def stop(self) -> None:
        """请求停止 CDP 连接。"""
        log.info("[CDPWorker] 收到停止请求")
        self._stop_event.set()
        self._close_ws()

    def run(self) -> None:
        """主循环：CDP 连接 + Cookie 轮询。"""
        log.info("[CDPWorker] 开始运行，尝试连接 CDP 端口 %d...", self._port)
        self.log_msg.emit("🔌 正在连接 CDP...")

        try:
            # 1. 等待 CDP 端口就绪
            if not self._wait_for_port(timeout=30.0):
                err = f"CDP 端口 {self._port} 未就绪（30s 超时）"
                log.error("[CDPWorker] %s", err)
                self.error.emit(err)
                return

            log.info("[CDPWorker] CDP 端口已就绪")
            self.log_msg.emit("✅ CDP 端口已就绪")

            # 2. 获取 targets 列表
            targets = self._get_targets()
            if not targets:
                err = "CDP 未返回任何 targets"
                log.error("[CDPWorker] %s", err)
                self.error.emit(err)
                return

            log.info("[CDPWorker] 获取到 %d 个 targets：", len(targets))
            for t in targets:
                log.info("[CDPWorker]   - type=%s id=%s title=%s",
                         t.get("type", "?"),
                         _shorten(str(t.get("id", "")), 12),
                         str(t.get("title", ""))[:60])

            # 3. 找到 browser target
            browser = self._find_browser_target(targets)
            if not browser:
                err = "未找到 browser 类型的 CDP target"
                log.error("[CDPWorker] %s", err)
                self.error.emit(err)
                return

            ws_url = browser.get("webSocketDebuggerUrl", "")
            log.info("[CDPWorker] 使用 browser target：id=%s ws_url=%s",
                     _shorten(str(browser.get("id", "")), 12),
                     ws_url[:120])

            # 4. 建立 WebSocket 连接
            if not self._connect_ws(ws_url):
                return

            # 5. Cookie 轮询循环
            log.info("[CDPWorker] 开始 Cookie 轮询（间隔=%.1fs，超时=%.1fs）",
                     self._poll_interval, self._timeout)
            self.log_msg.emit("🔍 正在轮询 Cookie，请扫码登录...")

            start_ts = time.time()
            tick_count = 0

            while not self._stop_event.is_set():
                elapsed = time.time() - start_ts
                if elapsed > self._timeout:
                    log.warning("[CDPWorker] 总超时 %.0fs，仍未检测到登录态", self._timeout)
                    self.login_timeout.emit()
                    return

                tick_count += 1
                log.debug("[CDPWorker] 轮询 #%d (elapsed=%.1fs)", tick_count, elapsed)

                try:
                    cookies = self._fetch_cookies_once()
                    wr_skey = self._find_cookie(cookies, "wr_skey")
                    wr_vid = self._find_cookie(cookies, "wr_vid")

                    # 每次都发 cookies_detected（供 UI 显示 Cookie 数量）
                    if len(cookies) != self._last_cookie_count:
                        self._last_cookie_count = len(cookies)
                        log.info("[CDPWorker] 检测到 %d 条 Cookie", len(cookies))
                        self.log_msg.emit(f"🍪 已获取 {len(cookies)} 条 Cookie")
                        self.cookies_detected.emit(cookies)

                    # 检查登录态
                    if wr_skey:
                        skey_val = wr_skey.get("value", "")
                        log.info("[CDPWorker] wr_skey 已获取：len=%d %s",
                                 len(skey_val), _mask_cookie_value(skey_val))

                        # 校验：wr_skey 存在且长度 ≥ 8
                        if len(str(skey_val)) >= 8:
                            log.info("[CDPWorker] ✅ 登录态有效（wr_skey len=%d）", len(skey_val))
                            self.log_msg.emit("✅ 登录成功！")
                            self.login_success.emit({
                                "cookies": cookies,
                                "wr_skey_length": len(str(skey_val)),
                                "has_wr_vid": wr_vid is not None,
                                "elapsed": round(elapsed, 1),
                                "cookie_count": len(cookies),
                            })
                            # 登录成功后进入待命模式，保持 WS 连接供书架/章节池拉取使用
                            log.info("[CDPWorker] 登录成功，进入待命模式（保持 WS 连接）")
                            self.log_msg.emit("⏳ 等待书架/章节池拉取完成...")
                            # 持续等待 stop_event（由 _stop_cdp_worker 设置）
                            self._stop_event.wait()
                            log.info("[CDPWorker] 待命结束，关闭 WS")
                            return
                        else:
                            log.warning("[CDPWorker] wr_skey 长度不足 (%d < 8)，等待完整值...", len(str(skey_val)))
                            self.log_msg.emit(f"⏳ wr_skey 不完整，继续等待...")
                    else:
                        if tick_count % 10 == 0:  # 每 10 次轮询打印一次（20s）
                            log.info("[CDPWorker] 等待扫码中... (#%d, elapsed=%.0fs)", tick_count, elapsed)

                except Exception as exc:
                    log.warning("[CDPWorker] Cookie 轮询异常：%s", exc)
                    if tick_count % 5 == 0:
                        self.log_msg.emit(f"⚠️ 连接异常，重试中... ({exc})")

                # 等待下一次轮询
                remaining = self._timeout - elapsed
                sleep_time = min(self._poll_interval, max(0.5, remaining))
                if self._stop_event.wait(sleep_time):
                    log.info("[CDPWorker] 轮询被中断")
                    break

        except Exception as exc:
            tb = traceback.format_exc()
            log.error("[CDPWorker] 未处理异常：%s\n%s", exc, tb)
            self.error.emit(f"CDP 工作线程异常：{exc}")
        finally:
            self._close_ws()
            log.info("[CDPWorker] 线程结束")

    # ---- CDP 基础操作 ----
    def _wait_for_port(self, timeout: float = 15.0) -> bool:
        """等待 CDP 端口就绪。"""
        log.debug("[CDPWorker] 等待 CDP 端口 %d 就绪（timeout=%.1fs）...", self._port, timeout)
        deadline = time.time() + timeout
        attempts = 0
        while time.time() < deadline and not self._stop_event.is_set():
            attempts += 1
            try:
                with socket.create_connection(("127.0.0.1", self._port), timeout=0.5):
                    log.debug("[CDPWorker] CDP 端口已就绪（第 %d 次尝试）", attempts)
                    return True
            except (ConnectionRefusedError, OSError, socket.timeout):
                pass
            # 间隔稍长一些，避免过于频繁
            time.sleep(0.3)
        log.debug("[CDPWorker] CDP 端口等待超时（共 %d 次尝试）", attempts)
        return False

    def _get_targets(self) -> list[dict]:
        """从 CDP HTTP 端点获取所有 targets。"""
        url = f"http://127.0.0.1:{self._port}/json"
        log.debug("[CDPWorker] 请求 targets: %s", url)
        try:
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()
            targets = resp.json()
            log.info("[CDPWorker] 获取到 %d 个 targets", len(targets))
            return targets
        except Exception as exc:
            log.error("[CDPWorker] 获取 targets 失败：%s", exc)
            raise

    def _find_browser_target(self, targets: list[dict]) -> dict | None:
        """优先找 'browser' 类型 target。"""
        for t in targets:
            if t.get("type") == "browser":
                log.info("[CDPWorker] 找到 browser target：id=%s", _shorten(str(t.get("id", "")), 12))
                return t
        # 备选：找第一个 'page' 类型
        for t in targets:
            if t.get("type") == "page":
                log.info("[CDPWorker] 用 page target 替代：id=%s", _shorten(str(t.get("id", "")), 12))
                return t
        log.warning("[CDPWorker] 未找到可用的 CDP target（types=%s）",
                     sorted({t.get("type", "") for t in targets}))
        return None

    def _connect_ws(self, ws_url: str) -> bool:
        """建立 WebSocket 连接到 CDP。"""
        log.info("[CDPWorker] 连接 WebSocket：%s", ws_url[:120])
        self.log_msg.emit("🔌 正在建立 WebSocket 连接...")
        try:
            self._ws = websocket.create_connection(
                ws_url,
                timeout=10,
                enable_multithread=True,
            )
            # 测试连通性
            self._msg_id += 1
            test_cmd = {"id": self._msg_id, "method": "Browser.getVersion"}
            self._ws.send(json.dumps(test_cmd))
            raw = self._ws.recv()
            resp = json.loads(raw)
            result = resp.get("result", {})
            log.info("[CDPWorker] Browser.getVersion: %s", json.dumps(result, ensure_ascii=False)[:200])
            self._connected_cdp = True
            self.connected.emit(result)
            self.log_msg.emit("✅ CDP 连接成功")
            return True
        except Exception as exc:
            log.error("[CDPWorker] WebSocket 连接失败：%s", exc)
            self.error.emit(f"WebSocket 连接失败：{exc}")
            self._connected_cdp = False
            return False

    def _close_ws(self) -> None:
        """关闭 WebSocket 连接。"""
        if self._ws:
            try:
                self._ws.close()
                log.debug("[CDPWorker] WebSocket 已关闭")
            except Exception:
                pass
            self._ws = None
        self._connected_cdp = False

    def _send_and_receive(self, method: str, params: dict | None = None,
                          timeout: float = 10.0) -> dict:
        """发送 CDP 命令并等待匹配响应。"""
        if not self._ws or not self._connected_cdp:
            raise RuntimeError("CDP 未连接")
        self._msg_id += 1
        cmd = {"id": self._msg_id, "method": method}
        if params:
            cmd["params"] = params
        self._ws.send(json.dumps(cmd))

        # 读取响应（跳过事件通知）
        deadline = time.time() + timeout
        while time.time() < deadline and not self._stop_event.is_set():
            try:
                self._ws.settimeout(timeout)
                raw = self._ws.recv()
                msg = json.loads(raw)
                if msg.get("id") == self._msg_id:
                    return msg
                # 跳过事件通知
                log.debug("[CDPWorker] 跳过事件：%s", raw[:100])
            except websocket.WebSocketTimeoutException:
                log.warning("[CDPWorker] 等待 %s 响应超时", method)
                raise
        raise TimeoutError(f"CDP {method} 超时")

    def evaluate_js(self, expression: str, *, timeout: float = 15.0) -> dict | None:
        """通过 Runtime.evaluate 在浏览器内执行 JS 代码，返回执行结果。

        关键：通过 CDP 在浏览器上下文内执行 fetch()，自动携带 Cookie、baggage header 等签名。
        """
        try:
            params = {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "timeout": int(timeout * 1000),
            }
            resp = self._send_and_receive("Runtime.evaluate", params, timeout=timeout + 5)
            result = resp.get("result", {}).get("result", {})
            if result.get("type") == "string":
                val = result.get("value", "")
                # 尝试 JSON 解析
                if val:
                    try:
                        return json.loads(val)
                    except (json.JSONDecodeError, TypeError):
                        return {"_raw": val}
            elif result.get("type") == "object":
                return result.get("value") or result
            elif result.get("type") == "undefined":
                return None
            return result
        except Exception as exc:
            log.warning("[CDPWorker] Runtime.evaluate 失败：%s", exc)
            return None

    def navigate_to(self, url: str, *, timeout: float = 15.0) -> bool:
        """通过 Page.navigate 导航到指定 URL。"""
        try:
            # 先 Page.enable（确保 Page 域可用）
            try:
                self._send_and_receive("Page.enable", {}, timeout=3)
            except Exception:
                pass
            # 导航
            resp = self._send_and_receive("Page.navigate", {"url": url}, timeout=timeout)
            log.info("[CDPWorker] Page.navigate 返回：%s", json.dumps(resp, ensure_ascii=False)[:200])
            return True
        except Exception as exc:
            log.error("[CDPWorker] navigate_to 异常：%s", exc)
            return False

    def _fetch_cookies_once(self) -> list[dict]:
        """获取当前所有 Cookie（Network.getCookies + Storage.getCookies 合并）。"""
        try:
            log.debug("[CDPWorker] 发送 Network.getCookies...")
            resp = self._send_and_receive("Network.getCookies", {})
            cookies = resp.get("result", {}).get("cookies", [])
            log.debug("[CDPWorker] Network.getCookies 返回 %d 条", len(cookies))
        except Exception as exc:
            log.warning("[CDPWorker] Network.getCookies 失败：%s", exc)
            cookies = []

        # 也尝试 Storage.getCookies（更现代的接口）
        try:
            resp2 = self._send_and_receive("Storage.getCookies", {}, timeout=5.0)
            storage_cookies = resp2.get("result", {}).get("cookies", [])
            if storage_cookies:
                log.debug("[CDPWorker] Storage.getCookies 返回 %d 条", len(storage_cookies))
                for c in storage_cookies:
                    if c not in cookies:
                        cookies.append(c)
        except Exception:
            log.debug("[CDPWorker] Storage.getCookies 不可用（正常）")

        return cookies

    @staticmethod
    def _find_cookie(cookies: list[dict], name: str) -> dict | None:
        """在 Cookie 列表中按名称查找。"""
        for c in cookies:
            if c.get("name") == name:
                return c
        return None


# ============================================================================
# CDP 登录对话框（QDialog）
# ============================================================================
class CDPLoginDialog(QDialog):
    """CDP 扫码登录对话框。

    信号：
      login_success(dict)  → 登录成功，payload 包含 cookies/books/chapter_pools
      login_failed(str)    → 登录失败
    """

    login_success = Signal(dict)
    login_failed = Signal(str)

    def __init__(
        self,
        config: ConfigStore,
        api: WeReadApi,
        notifier: WxPusherNotifier | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._api = api
        self._notifier = notifier or WxPusherNotifier(config)
        self._worker: CDPWorker | None = None
        self._thread: QThread | None = None
        self._latest_cookies: list[dict] = []
        self._login_completed = False
        self._web_engine_view = None  # 延迟初始化

        log.info("[CDPLoginDialog] 初始化")
        self._build_ui()
        self._start_cdp_worker()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.setWindowTitle("🍪 微信读书扫码登录（CDP）")
        self.setMinimumSize(900, 700)
        self.resize(1100, 800)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        # --- 内嵌浏览器 ---
        web_header = QLabel("🌐 微信读书网页（请使用手机扫码登录）")
        web_header.setStyleSheet("font-size:14px;font-weight:600;color:#2f3b52;")
        root.addWidget(web_header)

        try:
            from PySide6.QtWebEngineWidgets import QWebEngineView
            self._web_engine_view = QWebEngineView(self)
            self._web_engine_view.load(QUrl(WEREAD_URL))
            self._web_engine_view.setMinimumHeight(400)
            self._web_engine_view.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )
            root.addWidget(self._web_engine_view, 1)
            log.info("[CDPLoginDialog] QWebEngineView 已创建，加载 %s", WEREAD_URL)
        except Exception as exc:
            log.error("[CDPLoginDialog] QWebEngineView 创建失败：%s", exc)
            fallback = QLabel(
                f"⚠️ 无法创建浏览器组件：{exc}\n\n"
                "请确保 PySide6-WebEngine 已正确安装。"
            )
            fallback.setStyleSheet("color:#d9534f;padding:20px;")
            fallback.setWordWrap(True)
            fallback.setAlignment(Qt.AlignmentFlag.AlignCenter)
            fallback.setMinimumHeight(300)
            root.addWidget(fallback, 1)

        # --- 状态面板 ---
        status_frame = QWidget()
        status_frame.setStyleSheet(
            "QWidget{background:#f4f7fc;border:1px solid #e3e8f1;border-radius:10px;padding:12px;}"
        )
        status_layout = QVBoxLayout(status_frame)
        status_layout.setContentsMargins(12, 10, 12, 10)
        status_layout.setSpacing(8)

        self._lbl_cdp_status = QLabel("⏳ 正在启动 CDP...")
        self._lbl_cdp_status.setStyleSheet("color:#2d6cdf;font-size:13px;font-weight:600;")
        status_layout.addWidget(self._lbl_cdp_status)

        self._lbl_cookie_count = QLabel("🍪 Cookie 数量：等待检测...")
        self._lbl_cookie_count.setStyleSheet("color:#5f6c85;font-size:12px;")
        status_layout.addWidget(self._lbl_cookie_count)

        self._lbl_wr_skey = QLabel("🔑 wr_skey：等待扫码...")
        self._lbl_wr_skey.setStyleSheet("color:#5f6c85;font-size:12px;")
        status_layout.addWidget(self._lbl_wr_skey)

        self._lbl_bookshelf = QLabel("📚 书架：等待登录成功后获取...")
        self._lbl_bookshelf.setStyleSheet("color:#5f6c85;font-size:12px;")
        status_layout.addWidget(self._lbl_bookshelf)

        self._lbl_chapter_pool = QLabel("📖 章节池：等待书架获取后构建...")
        self._lbl_chapter_pool.setStyleSheet("color:#5f6c85;font-size:12px;")
        status_layout.addWidget(self._lbl_chapter_pool)

        self._lbl_log_hint = QLabel(
            "💡 提示：打开 app.log 查看详细 CDP 调试日志"
        )
        self._lbl_log_hint.setStyleSheet("color:#93a0b8;font-size:11px;")
        status_layout.addWidget(self._lbl_log_hint)

        root.addWidget(status_frame)

        # --- 调试工具按钮区 ---
        debug_btn_row = QHBoxLayout()
        debug_btn_row.setSpacing(10)

        self._btn_fetch_shelf = QPushButton("📚 获取书架")
        self._btn_fetch_shelf.setStyleSheet(self._debug_btn())
        self._btn_fetch_shelf.setToolTip(
            "先在上方浏览器中手动导航到书架页面（/web/shelf），再点击此按钮\n"
            "系统会以当前页面为 Referer 拉取书架列表"
        )
        self._btn_fetch_shelf.clicked.connect(self._on_debug_fetch_shelf)

        self._btn_fetch_chapters = QPushButton("📖 获取章节")
        self._btn_fetch_chapters.setStyleSheet(self._debug_btn())
        self._btn_fetch_chapters.setToolTip(
            "先在上方浏览器中手动导航到某本书的读书页面（/web/reader/wb{bookId}），再点击此按钮\n"
            "系统会以当前页面为 Referer 拉取当前书籍的章节池"
        )
        self._btn_fetch_chapters.clicked.connect(self._on_debug_fetch_chapters)

        debug_btn_row.addWidget(self._btn_fetch_shelf)
        debug_btn_row.addWidget(self._btn_fetch_chapters)
        debug_btn_row.addStretch(1)

        root.addLayout(debug_btn_row)

        # --- 按钮区 ---
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        self._btn_reload = QPushButton("🔄 重新加载页面")
        self._btn_reload.setStyleSheet(self._secondary_btn())
        self._btn_reload.clicked.connect(self._on_reload_page)

        self._btn_done = QPushButton("✅ 扫码完成，保存并继续")
        self._btn_done.setStyleSheet(self._primary_btn())
        self._btn_done.setEnabled(False)
        self._btn_done.clicked.connect(self._on_done_clicked)

        self._btn_cancel = QPushButton("❌ 取消")
        self._btn_cancel.setStyleSheet(self._secondary_btn())
        self._btn_cancel.clicked.connect(self.reject)

        btn_row.addWidget(self._btn_reload)
        btn_row.addStretch(1)
        btn_row.addWidget(self._btn_done)
        btn_row.addWidget(self._btn_cancel)

        root.addLayout(btn_row)

    @staticmethod
    def _primary_btn() -> str:
        return (
            "QPushButton{background:#2d6cdf;color:#fff;border:none;border-radius:8px;"
            "padding:8px 20px;font-size:13px;font-weight:600;}"
            "QPushButton:hover{background:#265bc0;}"
            "QPushButton:disabled{background:#b9c9eb;color:#fff;}"
        )

    @staticmethod
    def _debug_btn() -> str:
        return (
            "QPushButton{background:#f0f5ff;color:#2d6cdf;border:1px solid #c7d8f7;border-radius:8px;"
            "padding:6px 14px;font-size:12px;font-weight:500;}"
            "QPushButton:hover{background:#e0ecff;border-color:#2d6cdf;}"
            "QPushButton:disabled{color:#aab3c5;background:#f7f9fc;border-color:#e3e8f1;}"
        )

    @staticmethod
    def _secondary_btn() -> str:
        return (
            "QPushButton{background:#eef2f8;color:#2f3b52;border:none;border-radius:8px;"
            "padding:8px 16px;font-size:13px;}"
            "QPushButton:hover{background:#e2e8f4;}"
            "QPushButton:disabled{color:#aab3c5;background:#f3f5fb;}"
        )

    # ------------------------------------------------------------------
    # CDP Worker 生命周期
    # ------------------------------------------------------------------
    def _start_cdp_worker(self) -> None:
        """创建并启动 CDP Worker 线程。"""
        log.info("[CDPLoginDialog] 启动 CDP Worker 线程")
        self._worker = CDPWorker(port=CDP_PORT)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)

        # 信号连接
        self._thread.started.connect(self._worker.run)
        self._worker.connected.connect(self._on_cdp_connected)
        self._worker.cookies_detected.connect(self._on_cookies_detected)
        self._worker.login_success.connect(self._on_login_success)
        self._worker.login_timeout.connect(self._on_login_timeout)
        self._worker.error.connect(self._on_worker_error)
        self._worker.log_msg.connect(self._on_worker_log)

        # 清理
        self._worker.login_success.connect(self._thread.quit)
        self._worker.login_timeout.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)

        self._thread.start()
        log.info("[CDPLoginDialog] CDP Worker 线程已启动")

    def _stop_cdp_worker(self) -> None:
        """停止 CDP Worker 线程。"""
        if self._worker:
            self._worker.stop()
        if self._thread:
            self._thread.quit()
            self._thread.wait(3000)
        log.info("[CDPLoginDialog] CDP Worker 线程已停止")

    # ------------------------------------------------------------------
    # Worker 信号槽
    # ------------------------------------------------------------------
    def _on_cdp_connected(self, payload: dict) -> None:
        log.info("[CDPLoginDialog] CDP 已连接：%s", json.dumps(payload, ensure_ascii=False)[:200])
        self._lbl_cdp_status.setText("✅ CDP 已连接，等待扫码...")
        self._lbl_cdp_status.setStyleSheet("color:#2d6cdf;font-size:13px;font-weight:600;")

    def _on_cookies_detected(self, cookies: list) -> None:
        self._latest_cookies = cookies
        # 查找 wr_* 相关 Cookie
        wr_cookies = [c for c in cookies if str(c.get("name", "")).startswith("wr_")]
        wr_skey = CDPWorker._find_cookie(cookies, "wr_skey")
        log.info("[CDPLoginDialog] Cookie 检测更新：共 %d 条，wr_*: %d 条",
                 len(cookies), len(wr_cookies))
        self._lbl_cookie_count.setText(f"🍪 Cookie 数量：{len(cookies)} 条（wr_*: {len(wr_cookies)}）")
        if wr_skey:
            val = str(wr_skey.get("value", ""))
            self._lbl_wr_skey.setText(f"🔑 wr_skey：len={len(val)} {_mask_cookie_value(val)}")
            if len(val) >= 8:
                self._lbl_wr_skey.setStyleSheet("color:#2d9d3c;font-size:12px;font-weight:600;")
            else:
                self._lbl_wr_skey.setStyleSheet("color:#c49100;font-size:12px;")

    def _on_login_success(self, info: dict) -> None:
        cookies = info.get("cookies", [])
        skey_len = info.get("wr_skey_length", 0)
        elapsed = info.get("elapsed", 0)
        count = info.get("cookie_count", 0)
        log.info("[CDPLoginDialog] ✅ 登录成功：wr_skey_len=%d, cookie_count=%d, elapsed=%.1fs",
                 skey_len, count, elapsed)
        self._lbl_cdp_status.setText("✅ 登录成功！正在获取书架...")
        self._lbl_cdp_status.setStyleSheet("color:#2d9d3c;font-size:13px;font-weight:600;")
        self._btn_done.setEnabled(True)

        # 自动触发：持久化 → 书架 → 章节池
        QTimer.singleShot(200, lambda: self._on_login_auto_flow(cookies))

    def _on_login_timeout(self) -> None:
        log.warning("[CDPLoginDialog] 扫码超时")
        self._lbl_cdp_status.setText("⏰ 扫码超时，请重试")
        self._lbl_cdp_status.setStyleSheet("color:#d9534f;font-size:13px;font-weight:600;")
        QMessageBox.warning(
            self,
            "扫码超时",
            "等待扫码超时（3 分钟）。\n\n"
            "可能原因：\n"
            "1. 二维码已过期，请点击「重新加载页面」刷新\n"
            "2. 手机网络问题\n\n"
            "点击 OK 关闭对话框，可重新打开再试。",
        )
        self.reject()

    def _on_worker_error(self, err: str) -> None:
        log.error("[CDPLoginDialog] Worker 错误：%s", err)
        self._lbl_cdp_status.setText(f"❌ CDP 错误：{err[:80]}")
        self._lbl_cdp_status.setStyleSheet("color:#d9534f;font-size:13px;font-weight:600;")

    def _on_worker_log(self, msg: str) -> None:
        """Worker 转发的日志消息（写入 app.log + UI 状态）。"""
        log.info("[CDPLoginDialog] Worker: %s", msg)

    # ------------------------------------------------------------------
    # 自动流程：持久化 → 书架 → 章节池
    # ------------------------------------------------------------------
    def _on_login_auto_flow(self, cookies: list) -> None:
        """登录成功后自动执行完整流程。"""
        log.info("[CDPLoginDialog] 开始自动流程：持久化 → 书架 → 章节池")
        self._login_completed = True

        # Step 1: 持久化 Cookie
        try:
            self._persist_cookies(cookies)
            log.info("[CDPLoginDialog] ✅ Cookie 已持久化")
        except Exception as exc:
            log.error("[CDPLoginDialog] Cookie 持久化失败：%s", exc)
            self._lbl_bookshelf.setText("❌ Cookie 持久化失败")
            return

        # Step 2: 拉取书架（后台线程，避免阻塞 UI）
        self._lbl_bookshelf.setText("📚 正在获取书架...")
        QTimer.singleShot(100, lambda: self._fetch_bookshelf_async(cookies))

    def _fetch_bookshelf_async(self, cookies: list) -> None:
        """后台线程拉取书架。"""
        log.info("[CDPLoginDialog] 开始拉取书架")

        def _worker():
            try:
                books = self._fetch_bookshelf(cookies)
                log.info("[CDPLoginDialog] 书架获取成功：%d 本书", len(books))
                # 构建章节池
                chapter_pools = self._build_chapter_pools(cookies, books[:5])
                log.info("[CDPLoginDialog] 章节池构建完成：%d 本书", len(chapter_pools))
                # 回到 UI 线程
                QTimer.singleShot(0, lambda: self._on_shelf_ready(books, chapter_pools))
            except Exception as exc:
                log.error("[CDPLoginDialog] 书架获取异常：%s", exc)
                QTimer.singleShot(
                    0,
                    lambda: self._lbl_bookshelf.setText(f"❌ 书架获取失败：{exc}"),
                )

        threading.Thread(target=_worker, name="CDP-Bookshelf-Fetch", daemon=True).start()

    def _cdp_evaluate(self, expression: str, *, timeout: float = 15.0) -> dict | None:
        """通过 CDP 在浏览器内执行 JS。必须在 CDP Worker 已启动后调用。"""
        if not self._worker:
            log.warning("[CDPLoginDialog] CDP Worker 未启动，无法执行 JS")
            return None
        if not self._worker._connected_cdp:
            log.warning("[CDPLoginDialog] CDP 未连接，无法执行 JS")
            return None
        return self._worker.evaluate_js(expression, timeout=timeout)

    def _cdp_navigate(self, url: str, *, timeout: float = 15.0) -> bool:
        """通过 CDP Page.navigate 导航到指定 URL。"""
        log.info("[CDPLoginDialog] CDP 导航到 %s", url)
        return self._worker.navigate_to(url, timeout=timeout)

    def _get_api_baggage_headers(self) -> dict:
        """从 WeReadApi Session 提取 baggage 和 sentry-trace header 供浏览器 fetch 使用。"""
        try:
            self._api._augment_headers_baggage()
            headers = {}
            session_headers = self._api._session.headers
            for key in ("baggage", "sentry-trace", "user-agent", "referer", "accept"):
                val = session_headers.get(key) or session_headers.get(key.lower())
                if val:
                    headers[key] = val
            log.info("[CDPLoginDialog] 提取 baggage headers: keys=%s", list(headers.keys()))
            return headers
        except Exception as exc:
            log.warning("[CDPLoginDialog] 提取 baggage headers 失败：%s", exc)
            return {}

    # ============================================================
    # Skill 1.0.5 接口方法
    # ============================================================

    def _call_skill_api(self, api_name: str, params: dict, *, timeout: int = 10) -> dict | None:
        """通用 Skill API 调用方法（1.0.5+ 版本）。"""
        api_key = str(self._config.get("skill.api_key") or "").strip()
        if not api_key:
            log.info("[CDPLoginDialog] Skill API Key 未配置，跳过 %s", api_name)
            return None

        skill_version = str(self._config.get("skill.version") or "1.0.5")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "api_name": api_name,
            "skill_version": skill_version,
        }
        payload.update(params)

        try:
            log.info("[CDPLoginDialog] 🔄 Skill %s 请求：%s", api_name, {k: str(v)[:50] for k, v in params.items()})
            t0 = time.time()
            r = requests.post(SKILL_GATEWAY_URL, headers=headers, json=payload, timeout=timeout, verify=False)
            elapsed = (time.time() - t0) * 1000
            log.info("[CDPLoginDialog] 🔄 Skill %s 响应：HTTP=%d time=%.0fms", api_name, r.status_code, elapsed)

            if r.status_code == 499:
                log.warning("[CDPLoginDialog] Skill %s 触发 499 限流", api_name)
                return None
            if r.status_code == 401 or r.status_code == 403:
                log.warning("[CDPLoginDialog] Skill %s 鉴权失败（HTTP %d）", api_name, r.status_code)
                return None

            r.raise_for_status()
            data = r.json()

            if isinstance(data, dict) and data.get("errcode", 0) != 0:
                log.warning("[CDPLoginDialog] Skill %s 调用失败：errcode=%s msg=%s",
                            api_name, data.get("errcode"), data.get("errmsg"))
                return None

            if isinstance(data, dict) and "upgrade_info" in data:
                log.warning("[CDPLoginDialog] ⚠️ Skill 有新版可用：%s", data.get("upgrade_info"))

            return data if isinstance(data, dict) else None
        except Exception as exc:
            log.warning("[CDPLoginDialog] Skill %s 请求异常：%s", api_name, exc)
            return None

    def _fetch_bookshelf_via_skill(self) -> list[dict]:
        """通过 Skill 1.0.5 /shelf/sync 获取书架。"""
        log.info("[CDPLoginDialog] 📚 通过 Skill /shelf/sync 获取书架...")
        data = self._call_skill_api("/shelf/sync", {})
        if not data:
            return []

        books = data.get("books") or data.get("data") or []
        if not isinstance(books, list):
            return []

        valid = []
        for b in books:
            if not isinstance(b, dict):
                continue
            bid = str(b.get("bookId") or "").strip()
            if bid:
                # 转换为统一格式
                valid.append({
                    "bookId": bid,
                    "title": b.get("title", ""),
                    "author": b.get("author", ""),
                    "cover": b.get("cover", ""),
                    "finishReading": b.get("finishReading", 0),
                    "readUpdateTime": b.get("readUpdateTime", 0),
                })

        log.info("[CDPLoginDialog] 📚 Skill /shelf/sync 返回 %d 本有效书籍", len(valid))
        return valid

    def _fetch_chapters_via_skill(self, book_id: str) -> list[str]:
        """通过 Skill 1.0.5 /book/chapterinfo 获取章节池。"""
        log.info("[CDPLoginDialog] 📖 通过 Skill /book/chapterinfo 获取章节池：bookId=%s", book_id)
        data = self._call_skill_api(
            "/book/chapterinfo",
            {"bookId": book_id},
        )
        if not data:
            return []

        chapters = data.get("chapters") or data.get("chapterInfos") or []
        if not isinstance(chapters, list):
            return []

        # 提取 chapterUid 列表
        chapter_uids = []
        for ch in chapters:
            if isinstance(ch, dict):
                uid = ch.get("chapterUid")
                if uid is not None:
                    chapter_uids.append(str(uid))

        log.info("[CDPLoginDialog] 📖 Skill /book/chapterinfo 返回 %d 个章节", len(chapter_uids))
        return chapter_uids

    def _fetch_bookshelf(self, cookies: list, *, skip_navigate: bool = False) -> list[dict]:
        """获取书架：优先 Skill 1.0.5，回退到 Cookie 方案。"""
        log.info("[CDPLoginDialog] 请求书架 API（skip_navigate=%s）", skip_navigate)

        # 策略0：优先使用 Skill 1.0.5
        skill_books = self._fetch_bookshelf_via_skill()
        if skill_books:
            log.info("[CDPLoginDialog] ✅ Skill 书架获取成功：%d 本", len(skill_books))
            return skill_books

        log.info("[CDPLoginDialog] Skill 书架获取失败，回退到 Cookie 方案...")

        try:
            # 构建 requests.Session，注入 Cookie
            session = self._build_requests_session(cookies)
            if not session:
                log.error("[CDPLoginDialog] 无法构建 requests Session")
                return []

            # 提取 userVid
            user_vid = self._extract_user_vid(cookies)
            log.info("[CDPLoginDialog] 使用 Python requests 拉取书架，userVid=%s", _shorten(user_vid or "", 20))

            # 策略1：移动端 API
            mobile_url = f"https://i.weread.qq.com/shelf/friendCommon"
            params = {"userVid": user_vid} if user_vid else {}
            log.info("[CDPLoginDialog] 尝试移动端 API: %s?userVid=...", mobile_url)

            # 调试：检查 session 中实际会发送的 Cookie
            debug_cookies = session.cookies.get_dict()
            log.info("[CDPLoginDialog] Session Cookie keys: %s", list(debug_cookies.keys()))

            try:
                # 调试：检查请求前的 Cookie 预期（使用 session.prepare_request 才能获取 session 的 cookies）
                req = requests.Request('GET', mobile_url, params=params)
                prepared = session.prepare_request(req)
                sent_cookies = prepared.headers.get('Cookie', '')
                log.info("[CDPLoginDialog] 请求将发送 Cookie: %s", sent_cookies[:200] if sent_cookies else "(无)")

                resp = session.get(mobile_url, params=params, timeout=15, verify=False)
                log.info("[CDPLoginDialog] 移动端 API HTTP=%d body_len=%d", resp.status_code, len(resp.text))

                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        recent_books = data.get("recentBooks") or data.get("data") or []
                        if isinstance(recent_books, list) and len(recent_books) > 0:
                            log.info("[CDPLoginDialog] 移动端 API 成功：%d 本书", len(recent_books))
                            return self._parse_shelf_books(recent_books)
                        else:
                            log.warning("[CDPLoginDialog] 移动端 API 返回空：keys=%s",
                                        list(data.keys()) if isinstance(data, dict) else type(data).__name__)
                    except Exception as exc:
                        log.warning("[CDPLoginDialog] 移动端 API JSON 解析失败：%s", exc)
                elif resp.status_code == 401:
                    log.warning("[CDPLoginDialog] 移动端 API 401：%s", resp.text[:200])
            except requests.RequestException as exc:
                log.warning("[CDPLoginDialog] 移动端 API 请求异常：%s", exc)

            # 策略2：回退 Web API
            log.info("[CDPLoginDialog] 回退 Web API: weread.qq.com/web/shelf/booklist ...")
            return self._fetch_bookshelf_web_requests(session, skip_navigate=skip_navigate)

        except Exception as exc:
            log.error("[CDPLoginDialog] 书架获取异常：%s", exc)
            return []

    def _build_requests_session(self, cookies: list) -> requests.Session:
        """构建 requests.Session，注入 Cookie 和 baggage headers。"""
        session = requests.Session()
        # 设置 headers
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })

        # 收集所有 Cookie
        cookie_dict = {}
        for cookie in cookies:
            if isinstance(cookie, dict):
                name = cookie.get("name", "")
                value = cookie.get("value", "")
                if name and value:
                    cookie_dict[name] = str(value)

        # 打印收集到的 Cookie 名称
        log.info("[CDPLoginDialog] 收集到 Cookie: %s", list(cookie_dict.keys()))

        # 方式1：直接设置 Cookie 到 session（不指定域，让 requests 自动处理）
        # 这会创建无域限制的 Cookie
        session.cookies.update(cookie_dict)

        # 方式2：为 weread.qq.com 域设置 Cookie
        # 使用 CookieJar 直接操作，确保 Cookie 能发送到所有子域
        from requests.cookies import RequestsCookieJar
        for domain in [".weread.qq.com", ".i.weread.qq.com"]:
            for name, value in cookie_dict.items():
                try:
                    cookie_jar = session.cookies
                    cookie_jar.set(
                        name=name,
                        value=value,
                        domain=domain,
                        path="/"
                    )
                except Exception as exc:
                    log.debug("[CDPLoginDialog] 设置 Cookie %s 到 %s 失败：%s", name, domain, exc)

        # 验证 Cookie 是否正确添加
        all_cookies = []
        for cookie in session.cookies:
            all_cookies.append(f"{cookie.name}@{cookie.domain}")
        log.info("[CDPLoginDialog] Session Cookie 列表：%s", all_cookies)

        # 添加 baggage headers (从 WeReadApi 复制)
        try:
            self._api._augment_headers_baggage()
            baggage = self._api._session.headers.get("baggage", "")
            sentry_trace = self._api._session.headers.get("sentry-trace", "")
            if baggage:
                session.headers["baggage"] = baggage
                log.info("[CDPLoginDialog] 添加 baggage header")
            if sentry_trace:
                session.headers["sentry-trace"] = sentry_trace
                log.info("[CDPLoginDialog] 添加 sentry-trace header")
        except Exception as exc:
            log.warning("[CDPLoginDialog] 添加 baggage headers 失败：%s", exc)

        # 添加必要的 headers
        session.headers.update({
            "Referer": "https://weread.qq.com/",
            "Origin": "https://weread.qq.com",
        })

        # 检查关键 Cookie
        has_rk = "rk" in cookie_dict
        has_ptcz = "ptcz" in cookie_dict
        has_wr_skey = "wr_skey" in cookie_dict

        log.info("[CDPLoginDialog] Cookie 检查：rk=%s, ptcz=%s, wr_skey=%s",
                 "✅" if has_rk else "❌",
                 "✅" if has_ptcz else "❌",
                 "✅" if has_wr_skey else "❌")

        if not has_rk or not has_ptcz:
            log.warning("[CDPLoginDialog] ⚠️ 缺少 rk/ptcz Cookie，移动端 API 可能返回 401")

        return session

    def _fetch_bookshelf_web_requests(self, session: requests.Session, *, skip_navigate: bool = False) -> list[dict]:
        """用 Python requests 调用 Web 书架 API。"""
        try:
            url = "https://weread.qq.com/web/shelf/booklist"
            headers = {
                "Referer": "https://weread.qq.com/web/shelf",
                "Origin": "https://weread.qq.com",
            }

            log.info("[CDPLoginDialog] 正在用 requests fetch Web 书架...")
            resp = session.get(url, headers=headers, timeout=15, verify=False)
            log.info("[CDPLoginDialog] Web API HTTP=%d body_len=%d", resp.status_code, len(resp.text))

            if resp.status_code != 200:
                log.warning("[CDPLoginDialog] Web API HTTP=%d", resp.status_code)
                return []

            if not resp.text:
                return []

            try:
                data = resp.json()
            except Exception:
                log.warning("[CDPLoginDialog] Web API 非 JSON：%s", resp.text[:200])
                return []

            # 解析
            books = []
            if isinstance(data, list):
                books = [b for b in data if isinstance(b, dict)]
            elif isinstance(data, dict):
                for k in ("books", "book", "booklist", "shelfBookIds", "data", "recentBooks"):
                    v = data.get(k)
                    if isinstance(v, list):
                        books = [b for b in v if isinstance(b, dict)]
                        if books:
                            log.info("[CDPLoginDialog] 从 '%s' 字段解析到 %d 本书", k, len(books))
                            break

            return self._parse_shelf_books(books)

        except Exception as exc:
            log.error("[CDPLoginDialog] Web 书架获取异常：%s", exc)
            return []

    def _extract_user_vid(self, cookies: list) -> str:
        """从 Cookie 列表中提取 userVid (wr_vid)。"""
        for cookie in cookies:
            if isinstance(cookie, dict):
                name = cookie.get("name", "")
                if name == "wr_vid":
                    return str(cookie.get("value", "")).strip()
        return ""

    def _fetch_bookshelf_web(self, cookies: list, *, skip_navigate: bool = False) -> list[dict]:
        """Web 端书架 API（需要 baggage 风控 header）。"""
        try:
            # 导航到书架页面
            if not skip_navigate:
                log.info("[CDPLoginDialog] 自动导航到书架页面 /web/shelf ...")
                if not self._cdp_navigate("https://weread.qq.com/web/shelf", timeout=15.0):
                    log.warning("[CDPLoginDialog] 导航到书架页面失败")
            else:
                log.info("[CDPLoginDialog] 跳过导航（调试模式）")

            # 获取风控签名 headers
            baggage_headers = self._get_api_baggage_headers()
            baggage_js = json.dumps(baggage_headers, ensure_ascii=False)

            log.info("[CDPLoginDialog] 正在 fetch Web 书架...")
            js_code = f"""
(() => {{
    var hdrs = {baggage_js};
    hdrs['Accept'] = 'application/json, text/plain, */*';
    return fetch('/web/shelf/booklist', {{
        method: 'GET',
        credentials: 'include',
        headers: hdrs
    }}).then(r => r.text().then(t => {{
        return {{status: r.status, body: t}};
    }})).catch(e => ({{_error: String(e)}}));
}})()"""
            result = self._cdp_evaluate(js_code, timeout=15.0)
            log.info("[CDPLoginDialog] Web API 返回：%s",
                     json.dumps(result, ensure_ascii=False)[:300] if result else "None")

            if not result or result.get("_error"):
                log.warning("[CDPLoginDialog] Web fetch 错误：%s", result.get("_error") if result else "None")
                return []

            status = result.get("status", 0)
            body = result.get("body", "")
            if status != 200:
                log.warning("[CDPLoginDialog] Web API HTTP=%d", status)
                return []

            if not body:
                log.warning("[CDPLoginDialog] Web API 返回空 body")
                return []

            try:
                data = json.loads(str(body))
            except (json.JSONDecodeError, TypeError):
                log.warning("[CDPLoginDialog] Web API 非 JSON：%s", str(body)[:200])
                return []

            # 解析（Web API 结构）
            books = []
            if isinstance(data, list):
                books = [b for b in data if isinstance(b, dict)]
            elif isinstance(data, dict):
                for k in ("books", "book", "booklist", "shelfBookIds", "data", "recentBooks"):
                    v = data.get(k)
                    if isinstance(v, list):
                        books = [b for b in v if isinstance(b, dict)]
                        if books:
                            log.info("[CDPLoginDialog] 从 '%s' 字段解析到 %d 本书", k, len(books))
                            break

            return self._parse_shelf_books(books)

        except Exception as exc:
            log.error("[CDPLoginDialog] Web 书架获取异常：%s", exc)
            return []

    def _parse_shelf_books(self, books: list[dict]) -> list[dict]:
        """解析书架书籍列表，过滤有效书籍。"""
        log.info("[CDPLoginDialog] 解析书架：%d 本书", len(books))

        valid = []
        for b in books:
            bid = str(b.get("bookId") or b.get("book_id") or "").strip()
            # 过滤：必须有 bookId
            if not bid:
                continue
            # 可选：过滤非纯数字 bookId（公众号文章等）
            # 但某些书的 bookId 可能不是纯数字，所以不过滤
            valid.append(b)

        log.info("[CDPLoginDialog] 书架获取成功：%d 本书", len(valid))
        for b in valid[:10]:
            title = str(b.get("title", ""))[:40]
            bid = _shorten(str(b.get("bookId", "")), 16)
            log.info("[CDPLoginDialog]   - bookId=%s title=%s", bid, title)

        return valid

    def _build_chapter_pools(self, cookies: list, books: list[dict]) -> dict:
        """对每本书调 chapterInfos，构建章节池。"""
        log.info("[CDPLoginDialog] 开始构建章节池：%d 本书", len(books))
        chapter_pools: dict[str, list[str]] = {}

        for idx, book in enumerate(books):
            bid = str(book.get("bookId", "") or book.get("book_id", "")).strip()
            if not bid:
                log.warning("[CDPLoginDialog] 第 %d 本书无 bookId，跳过", idx)
                continue

            log.info("[CDPLoginDialog] 拉取第 %d/%d 本书章节：bookId=%s",
                     idx + 1, len(books), _shorten(bid, 16))

            try:
                chapters = self._fetch_chapters_for_book(cookies, bid)
                chapter_pools[bid] = chapters
                log.info("[CDPLoginDialog]   → %d 个章节", len(chapters))
            except Exception as exc:
                log.warning("[CDPLoginDialog]   → 章节获取失败：%s", exc)
                chapter_pools[bid] = []

        total_chapters = sum(len(v) for v in chapter_pools.values())
        log.info("[CDPLoginDialog] 章节池构建完成：%d 本书 / %d 章", len(chapter_pools), total_chapters)
        return chapter_pools

    def _fetch_chapters_for_book(self, cookies: list, book_id: str, *, skip_navigate: bool = False) -> list[str]:
        """获取章节池：优先 Skill 1.0.5，回退到 Cookie 方案。"""
        if not book_id:
            return []

        # 策略0：优先使用 Skill 1.0.5
        skill_chapters = self._fetch_chapters_via_skill(book_id)
        if skill_chapters:
            log.info("[CDPLoginDialog] ✅ Skill 章节池获取成功：%d 个章节", len(skill_chapters))
            return skill_chapters

        log.info("[CDPLoginDialog] Skill 章节池获取失败，回退到 Cookie 方案...")

        try:
            # 构建 requests.Session
            session = self._build_requests_session(cookies)
            if not session:
                log.error("[CDPLoginDialog] 无法构建 requests Session")
                return []

            log.info("[CDPLoginDialog] 使用 Python requests 拉取章节，bookId=%s", _shorten(book_id, 16))

            # 策略1：移动端 API (GET /book/info)
            mobile_chapters = self._fetch_chapters_mobile_requests(session, book_id)
            if mobile_chapters:
                return mobile_chapters

            # 策略2：回退 Web API
            log.info("[CDPLoginDialog] 回退 Web 章节 API bookId=%s ...", _shorten(book_id, 16))
            return self._fetch_chapters_web_requests(session, book_id)

        except Exception as exc:
            log.warning("[CDPLoginDialog] 章节获取异常（bookId=%s）：%s", book_id, exc)
            return []

    def _fetch_chapters_mobile_requests(self, session: requests.Session, book_id: str) -> list[str]:
        """用 Python requests 调用移动端章节 API。"""
        try:
            url = f"https://i.weread.qq.com/book/info"
            params = {"bookId": book_id}

            log.info("[CDPLoginDialog] 尝试移动端 /book/info bookId=%s ...", _shorten(book_id, 16))
            resp = session.get(url, params=params, timeout=15, verify=False)
            log.info("[CDPLoginDialog] /book/info HTTP=%d body_len=%d", resp.status_code, len(resp.text))

            if resp.status_code == 401:
                log.warning("[CDPLoginDialog] /book/info 401：%s", resp.text[:200])
                return []

            if resp.status_code != 200:
                log.warning("[CDPLoginDialog] /book/info HTTP=%d", resp.status_code)
                return []

            if not resp.text:
                return []

            try:
                data = resp.json()
            except Exception as exc:
                log.warning("[CDPLoginDialog] /book/info JSON 解析失败：%s", exc)
                return []

            # 解析 chapters 数组
            chapters: list[str] = []
            chapters_list = []

            if isinstance(data, dict):
                # 文档格式：data.chapters[].chapterUid
                if isinstance(data.get("chapters"), list):
                    chapters_list = data["chapters"]
                # 兼容：data.data.chapters
                elif isinstance(data.get("data"), dict) and isinstance(data["data"].get("chapters"), list):
                    chapters_list = data["data"]["chapters"]
                # 兼容：直接 data.updated
                elif isinstance(data.get("updated"), list):
                    chapters_list = data["updated"]
                # 兼容：直接 data 数组
                elif isinstance(data.get("data"), list):
                    for item in data["data"]:
                        if isinstance(item, dict) and isinstance(item.get("updated"), list):
                            chapters_list = item["updated"]
                            break

            for ch in chapters_list:
                if isinstance(ch, dict):
                    uid = str(ch.get("chapterUid") or ch.get("uid") or ch.get("id") or "")
                    if uid:
                        chapters.append(uid)
                elif isinstance(ch, str) and ch:
                    chapters.append(ch)

            log.info("[CDPLoginDialog]   /book/info bookId=%s → %d 个章节", _shorten(book_id, 16), len(chapters))
            return chapters

        except requests.RequestException as exc:
            log.warning("[CDPLoginDialog] /book/info 请求异常：%s", exc)
            return []
        except Exception as exc:
            log.warning("[CDPLoginDialog] /book/info 异常：%s", exc)
            return []

    def _fetch_chapters_web_requests(self, session: requests.Session, book_id: str) -> list[str]:
        """用 Python requests 调用 Web 章节 API。"""
        try:
            import random as _random
            import time as _time
            from app.core.weread_api import cal_hash, _encode_data, KEY

            now = int(_time.time())
            ts = int(now * 1000) + _random.randint(0, 999)
            rn = _random.randint(0, 9999)
            app_id = "wb182564874603h266381671"
            req_body: dict[str, Any] = {
                "bookIds": [book_id],
                "appId": app_id,
                "synckey": 0,
                "onlyBookshelf": 0,
                "ts": ts,
                "rn": rn,
                "sg": hashlib.sha256(f"{ts}{rn}{KEY}".encode()).hexdigest(),
                "ct": now,
            }
            req_body["s"] = cal_hash(_encode_data(req_body))

            url = "https://weread.qq.com/web/book/chapterInfos"
            headers = {
                "Content-Type": "application/json;charset=UTF-8",
                "Accept": "application/json, text/plain, */*",
                "Referer": f"https://weread.qq.com/web/reader/wb{book_id}",
                "Origin": "https://weread.qq.com",
            }

            log.info("[CDPLoginDialog] 回退 Web /book/chapterInfos bookId=%s ...", _shorten(book_id, 16))
            resp = session.post(url, json=req_body, headers=headers, timeout=15, verify=False)
            log.info("[CDPLoginDialog] Web chapterInfos HTTP=%d body_len=%d", resp.status_code, len(resp.text))

            if resp.status_code != 200:
                log.warning("[CDPLoginDialog] Web chapterInfos HTTP=%d", resp.status_code)
                return []

            if not resp.text:
                return []

            try:
                data = resp.json()
            except Exception as exc:
                log.warning("[CDPLoginDialog] Web chapterInfos JSON 解析失败：%s", exc)
                return []

            return self._parse_chapter_data(data, book_id)

        except Exception as exc:
            log.warning("[CDPLoginDialog] Web 章节获取异常：%s", exc)
            return []

    def _fetch_chapters_web(self, cookies: list, book_id: str, *, skip_navigate: bool = False) -> list[str]:
        """Web 端章节 API（需要 baggage 风控 header）。"""
        try:
            # 导航到读书页面
            if not skip_navigate:
                reader_url = f"https://weread.qq.com/web/reader/wb{book_id}"
                log.info("[CDPLoginDialog] 自动导航到读书页：%s", reader_url)
                self._cdp_navigate(reader_url, timeout=10.0)
            else:
                log.info("[CDPLoginDialog] 跳过导航（调试模式）")

            now = int(time.time())
            ts = int(now * 1000) + random.randint(0, 999)
            rn = random.randint(0, 9999)
            app_id = "wb182564874603h266381671"
            req_body: dict[str, Any] = {
                "bookIds": [book_id],
                "appId": app_id,
                "synckey": 0,
                "onlyBookshelf": 0,
                "ts": ts,
                "rn": rn,
                "sg": hashlib.sha256(f"{ts}{rn}{KEY}".encode()).hexdigest(),
                "ct": now,
            }
            req_body["s"] = cal_hash(_encode_data(req_body))
            body_json = json.dumps(req_body, separators=(",", ":"))

            # 获取风控签名 headers
            baggage_headers = self._get_api_baggage_headers()
            baggage_js = json.dumps(baggage_headers, ensure_ascii=False)

            js_code = f"""
(() => {{
    var body = {body_json};
    var hdrs = {baggage_js};
    hdrs['Content-Type'] = 'application/json;charset=UTF-8';
    hdrs['Accept'] = 'application/json, text/plain, */*';
    return fetch('/web/book/chapterInfos', {{
        method: 'POST',
        credentials: 'include',
        headers: hdrs,
        body: JSON.stringify(body)
    }}).then(r => r.text().then(t => {{
        return {{status: r.status, body: t}};
    }})).catch(e => ({{_error: String(e)}}));
}})()"""
            result = self._cdp_evaluate(js_code, timeout=15.0)
            log.info("[CDPLoginDialog] Web 章节 bookId=%s 返回：%s",
                     _shorten(book_id, 16),
                     json.dumps(result, ensure_ascii=False)[:300] if result else "None")

            if not result or result.get("_error"):
                log.warning("[CDPLoginDialog] Web 章节 fetch 错误：%s", result.get("_error") if result else "None")
                return []

            status = result.get("status", 0)
            body = result.get("body", "")
            if status != 200:
                log.warning("[CDPLoginDialog] Web 章节 API HTTP=%d", status)
                return []

            if not body:
                return []

            try:
                data = json.loads(str(body))
            except (json.JSONDecodeError, TypeError):
                log.warning("[CDPLoginDialog] Web 章节 API 非 JSON：%s", str(body)[:200])
                return []

            return self._parse_chapter_data(data, book_id)

        except Exception as exc:
            log.warning("[CDPLoginDialog] Web 章节获取异常：%s", exc)
            return []

    def _parse_chapter_data(self, data: Any, book_id: str) -> list[str]:
        """解析章节数据，提取 chapterUid 列表。"""
        chapters: list[str] = []
        books_list: list[dict] = []

        if isinstance(data, dict):
            if isinstance(data.get("books"), list):
                books_list = [b for b in data["books"] if isinstance(b, dict)]
            if not books_list and isinstance(data.get("data"), list):
                for e in data["data"]:
                    if isinstance(e, dict) and ("updated" in e or "chapters" in e):
                        books_list.append(e)
            if not books_list and isinstance(data.get("chapters"), list):
                books_list = [{"bookId": book_id, "chapters": data["chapters"]}]

        for b in books_list:
            chs = b.get("chapters") or b.get("updated") or []
            if isinstance(chs, list):
                for ch in chs:
                    if isinstance(ch, dict):
                        uid = str(ch.get("chapterUid") or ch.get("uid") or ch.get("id") or "")
                        if uid:
                            chapters.append(uid)
                    elif isinstance(ch, str):
                        if ch:
                            chapters.append(ch)

        log.info("[CDPLoginDialog]   bookId=%s → %d 个章节", _shorten(book_id, 16), len(chapters))
        return chapters

    def _on_shelf_ready(self, books: list, chapter_pools: dict) -> None:
        """书架和章节池都准备好了。"""
        total_chapters = sum(len(v) for v in chapter_pools.values())
        log.info("[CDPLoginDialog] 🎉 书架 %d 本，章节池 %d 章", len(books), total_chapters)

        self._lbl_bookshelf.setText(f"📚 书架：{len(books)} 本书")
        if total_chapters > 0:
            self._lbl_bookshelf.setStyleSheet("color:#2d9d3c;font-size:12px;font-weight:600;")
        else:
            self._lbl_bookshelf.setStyleSheet("color:#c49100;font-size:12px;")

        self._lbl_chapter_pool.setText(
            f"📖 章节池：{len(chapter_pools)} 本书 / {total_chapters} 章"
        )
        if total_chapters > 0:
            self._lbl_chapter_pool.setStyleSheet("color:#2d9d3c;font-size:12px;font-weight:600;")
        else:
            self._lbl_chapter_pool.setStyleSheet("color:#c49100;font-size:12px;")

        # 存储到 config
        try:
            self._config.set("reading.shelf_books", books)
            self._config.set("reading.chapter_pools", chapter_pools)
            log.info("[CDPLoginDialog] ✅ 书架和章节池已写入 config.json")
        except Exception as exc:
            log.error("[CDPLoginDialog] 写入 config 失败：%s", exc)

        # 书架+章节池都拉取完了，现在停止 CDP Worker（关闭 WS 连接）
        log.info("[CDPLoginDialog] 书架/章节池就绪，停止 CDP Worker")
        self._stop_cdp_worker()

        # 发送 login_success 信号（携带书架信息）
        self._btn_done.setText("✅ 全部完成，关闭对话框")
        self._btn_done.setEnabled(True)
        self._btn_done.clicked.disconnect()
        self._btn_done.clicked.connect(self._on_done_after_shelf)

    # ------------------------------------------------------------------
    # 调试按钮：手动操作触发 API 调用
    # ------------------------------------------------------------------
    def _on_debug_fetch_shelf(self) -> None:
        """调试：获取书架（用户已手动导航到书架页面）。"""
        if not self._worker or not self._worker._connected_cdp:
            QMessageBox.warning(self, "调试失败", "CDP 未连接，请先扫码登录")
            return

        self._btn_fetch_shelf.setEnabled(False)
        self._btn_fetch_shelf.setText("⏳ 正在获取...")
        log.info("[CDPLoginDialog] 调试：手动触发书架获取（skip_navigate=True）")

        cookies = self._latest_cookies

        def _worker():
            try:
                books = self._fetch_bookshelf(cookies, skip_navigate=True)
                log.info("[CDPLoginDialog] 调试书架获取：%d 本书", len(books))
                QTimer.singleShot(0, lambda: self._on_debug_shelf_result(books))
            except Exception as exc:
                log.error("[CDPLoginDialog] 调试书架获取异常：%s", exc)
                QTimer.singleShot(0, lambda: self._on_debug_shelf_error(str(exc)))

        threading.Thread(target=_worker, name="CDP-Debug-Shelf", daemon=True).start()

    def _on_debug_shelf_result(self, books: list) -> None:
        """调试书架获取结果回调。"""
        self._btn_fetch_shelf.setEnabled(True)
        self._btn_fetch_shelf.setText("📚 获取书架")

        if not books:
            self._lbl_bookshelf.setText("❌ 书架为空（请确认当前页面是否为 /web/shelf）")
            self._lbl_bookshelf.setStyleSheet("color:#d9534f;font-size:12px;")
            log.warning("[CDPLoginDialog] 调试书架获取 0 本，请检查当前页面 URL")
            return

        self._lbl_bookshelf.setText(f"📚 书架：{len(books)} 本书（调试）")
        self._lbl_bookshelf.setStyleSheet("color:#2d9d3c;font-size:12px;font-weight:600;")

        # 同时存储到 config 供后续使用
        try:
            self._config.set("reading.shelf_books", books)
            log.info("[CDPLoginDialog] 调试书架已写入 config：%d 本", len(books))
        except Exception as exc:
            log.error("[CDPLoginDialog] 调试书架写入 config 失败：%s", exc)

        # 输出前 5 本书到日志
        for b in books[:5]:
            title = str(b.get("title", ""))[:40]
            bid = _shorten(str(b.get("bookId", "")), 16)
            log.info("[CDPLoginDialog]   📖 bookId=%s title=%s", bid, title)

    def _on_debug_shelf_error(self, error: str) -> None:
        """调试书架获取错误回调。"""
        self._btn_fetch_shelf.setEnabled(True)
        self._btn_fetch_shelf.setText("📚 获取书架")
        self._lbl_bookshelf.setText(f"❌ 书架获取失败：{error}")
        self._lbl_bookshelf.setStyleSheet("color:#d9534f;font-size:12px;")

    def _on_debug_fetch_chapters(self) -> None:
        """调试：获取当前书籍的章节池（用户已手动导航到读书页面）。"""
        if not self._worker or not self._worker._connected_cdp:
            QMessageBox.warning(self, "调试失败", "CDP 未连接，请先扫码登录")
            return

        # 先从当前页面 URL 提取 bookId
        diag_js = """(function(){
            var path = location.pathname;
            var m = path.match(/reader\\/(wb)?([a-zA-Z0-9]+)/);
            return {
                url: location.href,
                bookId: m ? m[2] : null,
                match: m ? true : false
            };
        })()"""
        diag = self._cdp_evaluate(diag_js, timeout=5.0)
        log.info("[CDPLoginDialog] 调试章节：页面诊断=%s", diag)

        book_id = ""
        if isinstance(diag, dict):
            book_id = str(diag.get("bookId") or "").strip()

        if not book_id:
            QMessageBox.warning(
                self, "调试失败",
                "无法从当前页面 URL 提取 bookId。\n\n"
                "请先在浏览器中导航到一本书的读书页面，例如：\n"
                "https://weread.qq.com/web/reader/wbXXXXXXXXXXXXXXXX"
            )
            return

        self._btn_fetch_chapters.setEnabled(False)
        self._btn_fetch_chapters.setText("⏳ 正在获取...")
        log.info("[CDPLoginDialog] 调试：手动触发章节获取（bookId=%s, skip_navigate=True）", _shorten(book_id, 16))

        cookies = self._latest_cookies

        def _worker():
            try:
                chapters = self._fetch_chapters_for_book(cookies, book_id, skip_navigate=True)
                log.info("[CDPLoginDialog] 调试章节获取：bookId=%s → %d 章", _shorten(book_id, 16), len(chapters))
                QTimer.singleShot(0, lambda: self._on_debug_chapters_result(book_id, chapters))
            except Exception as exc:
                log.error("[CDPLoginDialog] 调试章节获取异常：%s", exc)
                QTimer.singleShot(0, lambda: self._on_debug_chapters_error(str(exc)))

        threading.Thread(target=_worker, name="CDP-Debug-Chapters", daemon=True).start()

    def _on_debug_chapters_result(self, book_id: str, chapters: list) -> None:
        """调试章节获取结果回调。"""
        self._btn_fetch_chapters.setEnabled(True)
        self._btn_fetch_chapters.setText("📖 获取章节")

        if not chapters:
            self._lbl_chapter_pool.setText(
                f"❌ 章节为空（bookId={_shorten(book_id, 16)}）"
            )
            self._lbl_chapter_pool.setStyleSheet("color:#d9534f;font-size:12px;")
            log.warning("[CDPLoginDialog] 调试章节获取 0 章，请检查当前页面是否为有效读书页")
            return

        self._lbl_chapter_pool.setText(
            f"📖 章节池：{_shorten(book_id, 16)} → {len(chapters)} 章（调试）"
        )
        self._lbl_chapter_pool.setStyleSheet("color:#2d9d3c;font-size:12px;font-weight:600;")

        # 合并到 config chapter_pools
        try:
            existing = self._config.get("reading.chapter_pools", {})
            if not isinstance(existing, dict):
                existing = {}
            existing[book_id] = chapters
            self._config.set("reading.chapter_pools", existing)
            log.info("[CDPLoginDialog] 调试章节已写入 config：bookId=%s → %d 章",
                     _shorten(book_id, 16), len(chapters))
        except Exception as exc:
            log.error("[CDPLoginDialog] 调试章节写入 config 失败：%s", exc)

        # 输出前 5 个 chapter UID 到日志
        for c in chapters[:5]:
            log.info("[CDPLoginDialog]   🔸 chapterUid=%s", _shorten(c, 16))

    def _on_debug_chapters_error(self, error: str) -> None:
        """调试章节获取错误回调。"""
        self._btn_fetch_chapters.setEnabled(True)
        self._btn_fetch_chapters.setText("📖 获取章节")
        self._lbl_chapter_pool.setText(f"❌ 章节获取失败：{error}")
        self._lbl_chapter_pool.setStyleSheet("color:#d9534f;font-size:12px;")

    # ------------------------------------------------------------------
    # Cookie 持久化
    # ------------------------------------------------------------------
    def _persist_cookies(self, cookies: list) -> None:
        """将 Cookie 持久化到 config.json。"""
        log.info("[CDPLoginDialog] 开始持久化 Cookie：%d 条", len(cookies))

        # 构建简单 dict（name → value）
        simple: dict[str, str] = {}
        for c in cookies:
            name = str(c.get("name", ""))
            if name:
                simple[name] = str(c.get("value", ""))

        # 保留 wr_* 前缀的 Cookie
        wr_prefixed = {k: v for k, v in simple.items() if k.startswith("wr_")}
        log.info("[CDPLoginDialog] wr_* Cookie：%d 个", len(wr_prefixed))
        for k, v in wr_prefixed.items():
            log.info("[CDPLoginDialog]   %s = %s", k, _mask_cookie_value(v))

        # 写入 config
        self._config.set("cookies", simple)
        self._config.set("cookies_raw", cookies)
        log.info("[CDPLoginDialog] Cookie 已写入 config.json")

        # 同步到 WeReadApi 的 Session
        self._sync_to_api(cookies)

    def _sync_to_api(self, cookies: list) -> None:
        """将 Cookie 同步到 WeReadApi 的 requests.Session。"""
        try:
            cookie_str = self._cookies_to_header(cookies)
            # 通过 WeReadApi 的方法同步
            if hasattr(self._api, "_session"):
                # 清空旧 Cookie
                self._api._session.cookies.clear()
                # 设置新 Cookie
                for c in cookies:
                    name = str(c.get("name", ""))
                    value = str(c.get("value", ""))
                    domain = str(c.get("domain", ""))
                    path = str(c.get("path", "/"))
                    if name:
                        self._api._session.cookies.set(
                            name, value, domain=domain, path=path
                        )
                log.info("[CDPLoginDialog] ✅ Cookie 已同步到 WeReadApi Session")

                # 尝试刷新 headers
                if hasattr(self._api, "_augment_headers_baggage"):
                    self._api._augment_headers_baggage()
                    log.info("[CDPLoginDialog] ✅ Headers 已刷新")
        except Exception as exc:
            log.error("[CDPLoginDialog] Cookie 同步到 API 失败：%s", exc)

    @staticmethod
    def _cookies_to_header(cookies: list) -> str:
        """将 Cookie 列表转为 Cookie header 字符串。"""
        parts = []
        for c in cookies:
            name = str(c.get("name", ""))
            value = str(c.get("value", ""))
            if name:
                parts.append(f"{name}={value}")
        return "; ".join(parts)

    @staticmethod
    def _get_user_agent() -> str:
        """返回通用 User-Agent。"""
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )

    # ------------------------------------------------------------------
    # 按钮槽
    # ------------------------------------------------------------------
    def _on_reload_page(self) -> None:
        """重新加载浏览器页面。"""
        log.info("[CDPLoginDialog] 重新加载页面")
        if self._web_engine_view:
            self._web_engine_view.reload()
            self._lbl_cdp_status.setText("🔄 页面已刷新，请重新扫码...")
            self._lbl_cdp_status.setStyleSheet("color:#c49100;font-size:13px;font-weight:600;")
            # 重置 CDP Worker（重新轮询）
            self._stop_cdp_worker()
            QTimer.singleShot(500, self._start_cdp_worker)

    def _on_done_clicked(self) -> None:
        """手动点击「扫码完成」按钮。"""
        log.info("[CDPLoginDialog] 用户手动点击完成按钮")
        if self._login_completed:
            self._on_done_after_shelf()
        else:
            # 尝试用当前最新的 Cookie 直接走完成流程
            if self._latest_cookies:
                wr_skey = CDPWorker._find_cookie(self._latest_cookies, "wr_skey")
                if wr_skey and len(str(wr_skey.get("value", ""))) >= 8:
                    self._on_login_auto_flow(self._latest_cookies)
                else:
                    QMessageBox.warning(
                        self,
                        "登录未完成",
                        "未检测到有效的 wr_skey（长度需 ≥ 8）。\n\n请确保已在手机上完成扫码登录。",
                    )
            else:
                QMessageBox.information(
                    self,
                    "等待扫码",
                    "正在等待扫码登录，请使用手机扫描页面上的二维码。",
                )

    def _on_done_after_shelf(self) -> None:
        """全部完成后关闭对话框并发射信号。"""
        log.info("[CDPLoginDialog] ✅ 全部完成，关闭对话框")
        self._stop_cdp_worker()
        self.accept()

    # ------------------------------------------------------------------
    # 对话框关闭
    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802
        """关闭对话框时清理资源。"""
        log.info("[CDPLoginDialog] 关闭对话框")
        self._stop_cdp_worker()
        super().closeEvent(event)

    def reject(self) -> None:
        """取消对话框。"""
        log.info("[CDPLoginDialog] 用户取消")
        self._stop_cdp_worker()
        self.login_failed.emit("用户取消登录")
        super().reject()

    def accept(self) -> None:
        """确认对话框。"""
        log.info("[CDPLoginDialog] 用户确认")
        self._stop_cdp_worker()

        # 发射 login_success 信号（包含所有收集的数据）
        try:
            shelf_books = self._config.get("reading.shelf_books") or []
            chapter_pools = self._config.get("reading.chapter_pools") or {}
            total_chapters = sum(len(v) for v in chapter_pools.values() if isinstance(v, list))
            payload = {
                "cookies": self._latest_cookies,
                "books": shelf_books,
                "chapter_pools": chapter_pools,
                "book_count": len(shelf_books),
                "chapter_count": total_chapters,
            }
            self.login_success.emit(payload)
            log.info("[CDPLoginDialog] login_success 信号已发射：books=%d, chapters=%d",
                     payload["book_count"], payload["chapter_count"])

            # 发送 WxPusher 登录成功推送
            try:
                today = _dt.date.today().isoformat()
                self._notifier.send_async(
                    f"【微信读书助手】CDP 登录成功，书架 {len(shelf_books)} 本，章节 {total_chapters} 章",
                    dedup_key=f"cdp_login_ok_{today}",
                    dedup_window_sec=3600 * 23,
                )
                log.info("[CDPLoginDialog] WxPusher 登录成功推送已发送")
            except Exception as exc:
                log.warning("[CDPLoginDialog] WxPusher 推送异常（可忽略）：%s", exc)

        except Exception as exc:
            log.error("[CDPLoginDialog] 发射 login_success 异常：%s", exc)

        super().accept()
