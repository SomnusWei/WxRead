"""CDP 扫码登录对话框（v2 精简版）。

v2 改造：
- 仅负责 Cookie 抓取（Network.getCookies + Storage.getCookies）
- 不在对话框内拉取书架/章节池（交给 SkillAPI + LocalDB）
- 登录成功后：持久化 Cookie + 同步到 WeReadApi Session + 发射 login_success 信号
- 丝滑 UI：淡入淡出动画
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import traceback
from typing import Any

import requests
import websocket
from PySide6.QtCore import (
    QObject,
    QPropertyAnimation,
    QThread,
    QTimer,
    Signal,
    Qt,
    QEasingCurve,
)
from PySide6.QtWidgets import (
    QDialog,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtCore import QUrl

from app.core.config import ConfigStore
from app.core.weread_api import WeReadApi
from app.utils.logger import get_logger

log = get_logger("wxread.ui.cdp_login_dialog")

# —— CDP 端口：用 9223 避免与 test_extension 的 9222 冲突 ——
CDP_PORT = 9223
WEREAD_URL = "https://weread.qq.com/"

# Chromium flags：启用 CDP
_chromium_flags = (
    f"--remote-debugging-port={CDP_PORT} "
    f"--disable-features=ExtensionsBrowserActivity,DesktopCaptureNotifications"
)
_existing_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
    f"{_existing_flags} {_chromium_flags}".strip()
    if _existing_flags
    else _chromium_flags
)


def _shorten(s: str, n: int = 8) -> str:
    if not s:
        return "(empty)"
    if len(s) <= n * 2:
        return s
    return s[:n] + "***" + s[-4:]


def _mask_cookie_value(value: str) -> str:
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
      connected(dict)        → CDP 连接成功，payload={port, browser_info}
      cookies_detected(list) → 检测到 Cookie（每次数量变化时发送）
      login_success(dict)    → 检测到有效登录态，payload={cookies, wr_skey_length, ...}
      login_timeout()        → 超时仍未检测到登录态
      error(str)             → 发生错误
      log_msg(str)           → 日志消息（转发给 UI）
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
        self._poll_interval = 2.0
        self._timeout = 180.0
        self._last_cookie_count = 0
        log.info("[CDPWorker] 初始化：port=%d", port)

    def stop(self) -> None:
        log.info("[CDPWorker] 收到停止请求")
        self._stop_event.set()
        self._close_ws()

    def run(self) -> None:
        """主循环：CDP 连接 + Cookie 轮询。"""
        log.info("[CDPWorker] 开始运行，尝试连接 CDP 端口 %d...", self._port)
        self.log_msg.emit("🔌 正在连接 CDP...")
        try:
            if not self._wait_for_port(timeout=30.0):
                err = f"CDP 端口 {self._port} 未就绪（30s 超时）"
                log.error("[CDPWorker] %s", err)
                self.error.emit(err)
                return
            log.info("[CDPWorker] CDP 端口已就绪")
            self.log_msg.emit("✅ CDP 端口已就绪")

            targets = self._get_targets()
            if not targets:
                self.error.emit("CDP 未返回任何 targets")
                return

            log.info("[CDPWorker] 获取到 %d 个 targets", len(targets))
            for t in targets:
                log.info(
                    "[CDPWorker]   - type=%s id=%s title=%s",
                    t.get("type", "?"),
                    _shorten(str(t.get("id", "")), 12),
                    str(t.get("title", ""))[:60],
                )

            browser = self._find_browser_target(targets)
            if not browser:
                self.error.emit("未找到 browser 类型的 CDP target")
                return

            ws_url = browser.get("webSocketDebuggerUrl", "")
            log.info(
                "[CDPWorker] 使用 browser target：ws_url=%s", ws_url[:120]
            )
            if not self._connect_ws(ws_url):
                return

            log.info(
                "[CDPWorker] 开始 Cookie 轮询（间隔=%.1fs，超时=%.1fs）",
                self._poll_interval, self._timeout,
            )
            self.log_msg.emit("🔍 正在轮询 Cookie，请扫码登录...")

            start_ts = time.time()
            tick_count = 0
            while not self._stop_event.is_set():
                elapsed = time.time() - start_ts
                if elapsed > self._timeout:
                    log.warning(
                        "[CDPWorker] 总超时 %.0fs，仍未检测到登录态", self._timeout
                    )
                    self.login_timeout.emit()
                    return
                tick_count += 1
                try:
                    cookies = self._fetch_cookies_once()
                    wr_skey = self._find_cookie(cookies, "wr_skey")
                    wr_vid = self._find_cookie(cookies, "wr_vid")

                    if len(cookies) != self._last_cookie_count:
                        self._last_cookie_count = len(cookies)
                        log.info("[CDPWorker] 检测到 %d 条 Cookie", len(cookies))
                        self.log_msg.emit(f"🍪 已获取 {len(cookies)} 条 Cookie")
                        self.cookies_detected.emit(cookies)

                    if wr_skey:
                        skey_val = wr_skey.get("value", "")
                        log.info(
                            "[CDPWorker] wr_skey 已获取：len=%d %s",
                            len(skey_val), _mask_cookie_value(skey_val),
                        )
                        if len(str(skey_val)) >= 8:
                            log.info(
                                "[CDPWorker] ✅ 登录态有效（wr_skey len=%d）",
                                len(skey_val),
                            )
                            self.log_msg.emit("✅ 登录成功！")
                            self.login_success.emit({
                                "cookies": cookies,
                                "wr_skey_length": len(str(skey_val)),
                                "has_wr_vid": wr_vid is not None,
                                "elapsed": round(elapsed, 1),
                                "cookie_count": len(cookies),
                            })
                            # 登录成功后待命，保持 WS 连接，直到外部 stop
                            log.info("[CDPWorker] 登录成功，进入待命模式")
                            self.log_msg.emit("⏳ Cookie 已获取，可点击「完成」")
                            self._stop_event.wait()
                            log.info("[CDPWorker] 待命结束，关闭 WS")
                            return
                        else:
                            log.warning(
                                "[CDPWorker] wr_skey 长度不足 (%d < 8)，等待完整值...",
                                len(str(skey_val)),
                            )
                            self.log_msg.emit("⏳ wr_skey 不完整，继续等待...")
                    else:
                        if tick_count % 10 == 0:
                            log.info(
                                "[CDPWorker] 等待扫码中... (#%d, elapsed=%.0fs)",
                                tick_count, elapsed,
                            )
                except Exception as exc:  # noqa: BLE001
                    log.warning("[CDPWorker] Cookie 轮询异常：%s", exc)
                    if tick_count % 5 == 0:
                        self.log_msg.emit(f"⚠️ 连接异常，重试中... ({exc})")

                remaining = self._timeout - elapsed
                sleep_time = min(self._poll_interval, max(0.5, remaining))
                if self._stop_event.wait(sleep_time):
                    log.info("[CDPWorker] 轮询被中断")
                    break
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            log.error("[CDPWorker] 未处理异常：%s\n%s", exc, tb)
            self.error.emit(f"CDP 工作线程异常：{exc}")
        finally:
            self._close_ws()
            log.info("[CDPWorker] 线程结束")

    # ---- CDP 基础操作 ----
    def _wait_for_port(self, timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        attempts = 0
        while time.time() < deadline and not self._stop_event.is_set():
            attempts += 1
            try:
                with socket.create_connection(("127.0.0.1", self._port), timeout=0.5):
                    return True
            except (ConnectionRefusedError, OSError, socket.timeout):
                pass
            time.sleep(0.3)
        return False

    def _get_targets(self) -> list[dict]:
        url = f"http://127.0.0.1:{self._port}/json"
        try:
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            log.error("[CDPWorker] 获取 targets 失败：%s", exc)
            raise

    def _find_browser_target(self, targets: list[dict]) -> dict | None:
        for t in targets:
            if t.get("type") == "browser":
                return t
        for t in targets:
            if t.get("type") == "page":
                return t
        return None

    def _connect_ws(self, ws_url: str) -> bool:
        log.info("[CDPWorker] 连接 WebSocket：%s", ws_url[:120])
        self.log_msg.emit("🔌 正在建立 WebSocket 连接...")
        try:
            self._ws = websocket.create_connection(
                ws_url, timeout=10, enable_multithread=True
            )
            self._msg_id += 1
            test_cmd = {"id": self._msg_id, "method": "Browser.getVersion"}
            self._ws.send(json.dumps(test_cmd))
            raw = self._ws.recv()
            resp = json.loads(raw)
            result = resp.get("result", {})
            log.info(
                "[CDPWorker] Browser.getVersion: %s",
                json.dumps(result, ensure_ascii=False)[:200],
            )
            self._connected_cdp = True
            self.connected.emit(result)
            self.log_msg.emit("✅ CDP 连接成功")
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("[CDPWorker] WebSocket 连接失败：%s", exc)
            self.error.emit(f"WebSocket 连接失败：{exc}")
            self._connected_cdp = False
            return False

    def _close_ws(self) -> None:
        if self._ws:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        self._connected_cdp = False

    def _send_and_receive(
        self, method: str, params: dict | None = None, timeout: float = 10.0
    ) -> dict:
        if not self._ws or not self._connected_cdp:
            raise RuntimeError("CDP 未连接")
        self._msg_id += 1
        cmd = {"id": self._msg_id, "method": method}
        if params:
            cmd["params"] = params
        self._ws.send(json.dumps(cmd))
        deadline = time.time() + timeout
        while time.time() < deadline and not self._stop_event.is_set():
            try:
                self._ws.settimeout(timeout)
                raw = self._ws.recv()
                msg = json.loads(raw)
                if msg.get("id") == self._msg_id:
                    return msg
            except websocket.WebSocketTimeoutException:
                raise
        raise TimeoutError(f"CDP {method} 超时")

    def _fetch_cookies_once(self) -> list[dict]:
        """获取当前所有 Cookie（Network.getCookies + Storage.getCookies 合并）。"""
        cookies: list[dict] = []
        try:
            resp = self._send_and_receive("Network.getCookies", {})
            cookies = resp.get("result", {}).get("cookies", [])
        except Exception as exc:  # noqa: BLE001
            log.warning("[CDPWorker] Network.getCookies 失败：%s", exc)
        try:
            resp2 = self._send_and_receive("Storage.getCookies", {}, timeout=5.0)
            storage_cookies = resp2.get("result", {}).get("cookies", [])
            for c in storage_cookies:
                if c not in cookies:
                    cookies.append(c)
        except Exception:  # noqa: BLE001
            pass
        return cookies

    @staticmethod
    def _find_cookie(cookies: list[dict], name: str) -> dict | None:
        for c in cookies:
            if c.get("name") == name:
                return c
        return None


# ============================================================================
# CDP 登录对话框（QDialog）
# ============================================================================
class CDPLoginDialog(QDialog):
    """CDP 扫码登录对话框（v2 精简版）。

    信号：
      login_success(dict) → 登录成功，payload={cookies, cookies_dict, ...}
      login_failed(str)   → 登录失败/取消
    """

    login_success = Signal(dict)
    login_failed = Signal(str)

    def __init__(
        self,
        config: ConfigStore,
        api: WeReadApi,
        parent: QWidget | None = None,
    ) -> None:
        # 关键：parent=None + WindowType.Tool（与「查看报告」按钮完全一致），
        # 彻底避免 Windows 下点击扫码登录时主窗口「先最小化再弹起」的闪烁。
        # 注：Window + parent=None 在某些 DPI/系统主题下仍会触发模态层主窗隐藏，
        # 而 Tool 标志明确告知 Windows：这是工具窗口，主窗是独立可交互的。
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint,
        )
        # 保留用户传入的 parent 引用（用于消息框定位、QWidget 层级相对），不参与 Qt 父子所有权
        self._caller_ref = parent
        self._config = config
        self._api = api
        self._worker: CDPWorker | None = None
        self._thread: QThread | None = None
        self._latest_cookies: list[dict] = []
        self._login_completed = False
        self._web_engine_view: QWebEngineView | None = None
        self._fade_anim: QPropertyAnimation | None = None

        log.info("[CDPLoginDialog] 初始化")
        self._build_ui()
        self._apply_fade_in()
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
            self._web_engine_view = QWebEngineView(self)
            self._web_engine_view.load(QUrl(WEREAD_URL))
            self._web_engine_view.setMinimumHeight(400)
            self._web_engine_view.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )
            root.addWidget(self._web_engine_view, 1)
            log.info(
                "[CDPLoginDialog] QWebEngineView 已创建，加载 %s", WEREAD_URL
            )
        except Exception as exc:  # noqa: BLE001
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
        self._lbl_cdp_status.setStyleSheet(
            "color:#2d6cdf;font-size:13px;font-weight:600;"
        )
        status_layout.addWidget(self._lbl_cdp_status)

        self._lbl_cookie_count = QLabel("🍪 Cookie 数量：等待检测...")
        self._lbl_cookie_count.setStyleSheet("color:#5f6c85;font-size:12px;")
        status_layout.addWidget(self._lbl_cookie_count)

        self._lbl_wr_skey = QLabel("🔑 wr_skey：等待扫码...")
        self._lbl_wr_skey.setStyleSheet("color:#5f6c85;font-size:12px;")
        status_layout.addWidget(self._lbl_wr_skey)

        self._lbl_log_hint = QLabel("💡 提示：打开 app.log 查看详细 CDP 调试日志")
        self._lbl_log_hint.setStyleSheet("color:#93a0b8;font-size:11px;")
        status_layout.addWidget(self._lbl_log_hint)

        root.addWidget(status_frame)

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

        # 阴影效果
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setColor(Qt.GlobalColor.black)
        shadow.setOffset(0, 2)
        status_frame.setGraphicsEffect(shadow)

    def _apply_fade_in(self) -> None:
        """对话框淡入动画（200ms）。"""
        try:
            effect = QGraphicsOpacityEffect(self)
            self.setGraphicsEffect(effect)
            anim = QPropertyAnimation(effect, b"opacity", self)
            anim.setDuration(200)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
            self._fade_anim = anim
        except Exception as exc:  # noqa: BLE001
            log.warning("[CDPLoginDialog] 淡入动画失败：%s", exc)

    @staticmethod
    def _primary_btn() -> str:
        return (
            "QPushButton{background:#2d6cdf;color:#fff;border:none;border-radius:8px;"
            "padding:8px 20px;font-size:13px;font-weight:600;}"
            "QPushButton:hover{background:#265bc0;}"
            "QPushButton:disabled{background:#b9c9eb;color:#fff;}"
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
        log.info("[CDPLoginDialog] 启动 CDP Worker 线程")
        self._worker = CDPWorker(port=CDP_PORT)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.connected.connect(self._on_cdp_connected)
        self._worker.cookies_detected.connect(self._on_cookies_detected)
        self._worker.login_success.connect(self._on_login_success)
        self._worker.login_timeout.connect(self._on_login_timeout)
        self._worker.error.connect(self._on_worker_error)
        self._worker.log_msg.connect(self._on_worker_log)

        self._worker.login_success.connect(self._thread.quit)
        self._worker.login_timeout.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)

        self._thread.start()

    def _stop_cdp_worker(self) -> None:
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
        log.info(
            "[CDPLoginDialog] CDP 已连接：%s",
            json.dumps(payload, ensure_ascii=False)[:200],
        )
        self._lbl_cdp_status.setText("✅ CDP 已连接，等待扫码...")
        self._lbl_cdp_status.setStyleSheet(
            "color:#2d6cdf;font-size:13px;font-weight:600;"
        )

    def _on_cookies_detected(self, cookies: list) -> None:
        self._latest_cookies = cookies
        wr_cookies = [c for c in cookies if str(c.get("name", "")).startswith("wr_")]
        wr_skey = CDPWorker._find_cookie(cookies, "wr_skey")
        log.info(
            "[CDPLoginDialog] Cookie 检测更新：共 %d 条，wr_*: %d 条",
            len(cookies), len(wr_cookies),
        )
        self._lbl_cookie_count.setText(
            f"🍪 Cookie 数量：{len(cookies)} 条（wr_*: {len(wr_cookies)}）"
        )
        if wr_skey:
            val = str(wr_skey.get("value", ""))
            self._lbl_wr_skey.setText(
                f"🔑 wr_skey：len={len(val)} {_mask_cookie_value(val)}"
            )
            if len(val) >= 8:
                self._lbl_wr_skey.setStyleSheet(
                    "color:#2d9d3c;font-size:12px;font-weight:600;"
                )
            else:
                self._lbl_wr_skey.setStyleSheet("color:#c49100;font-size:12px;")

    def _on_login_success(self, info: dict) -> None:
        cookies = info.get("cookies", [])
        skey_len = info.get("wr_skey_length", 0)
        elapsed = info.get("elapsed", 0)
        count = info.get("cookie_count", 0)
        log.info(
            "[CDPLoginDialog] ✅ 登录成功：wr_skey_len=%d, cookie_count=%d, elapsed=%.1fs",
            skey_len, count, elapsed,
        )
        self._lbl_cdp_status.setText("✅ 登录成功！可点击「完成」继续")
        self._lbl_cdp_status.setStyleSheet(
            "color:#2d9d3c;font-size:13px;font-weight:600;"
        )
        self._btn_done.setEnabled(True)
        # 自动触发完成流程（用户也可手动点）
        QTimer.singleShot(500, lambda: self._finalize_login(cookies))

    def _on_login_timeout(self) -> None:
        log.warning("[CDPLoginDialog] 扫码超时")
        self._lbl_cdp_status.setText("⏰ 扫码超时，请重试")
        self._lbl_cdp_status.setStyleSheet(
            "color:#d9534f;font-size:13px;font-weight:600;"
        )
        QMessageBox.warning(
            self,
            "扫码超时",
            "等待扫码超时（3 分钟）。\n\n可能原因：\n"
            "1. 二维码已过期，请点击「重新加载页面」刷新\n"
            "2. 手机网络问题\n\n点击 OK 关闭对话框，可重新打开再试。",
        )
        self.reject()

    def _on_worker_error(self, err: str) -> None:
        log.error("[CDPLoginDialog] Worker 错误：%s", err)
        self._lbl_cdp_status.setText(f"❌ CDP 错误：{err[:80]}")
        self._lbl_cdp_status.setStyleSheet(
            "color:#d9534f;font-size:13px;font-weight:600;"
        )

    def _on_worker_log(self, msg: str) -> None:
        log.info("[CDPLoginDialog] Worker: %s", msg)

    # ------------------------------------------------------------------
    # 登录完成流程
    # ------------------------------------------------------------------
    def _finalize_login(self, cookies: list) -> None:
        """登录成功后：持久化 Cookie + 同步 API + 发射信号。"""
        if self._login_completed:
            return
        self._login_completed = True
        log.info("[CDPLoginDialog] 开始持久化 Cookie：%d 条", len(cookies))

        # 构建 dict 视图
        cookies_dict: dict[str, str] = {}
        for c in cookies:
            name = str(c.get("name", ""))
            value = str(c.get("value", ""))
            if name and value:
                cookies_dict[name] = value

        # 提取常用 headers（从 config 读取默认 + UA）
        default_headers = self._config.get("headers", {}) or {}
        # 确保用浏览器实际 UA（如能拿到）
        ua = (
            next(
                (str(c.get("value", ""))
                 for c in cookies if str(c.get("name", "")).lower() == "user-agent"),
                None,
            )
            if False
            else None
        )
        if ua:
            default_headers["User-Agent"] = ua

        # 持久化到 config + 同步到 WeReadApi Session
        try:
            self._api.set_session(default_headers, cookies_dict, cookies)
            log.info("[CDPLoginDialog] ✅ Cookie 已持久化并同步到 API Session")
        except Exception as exc:  # noqa: BLE001
            log.error("[CDPLoginDialog] Cookie 持久化失败：%s", exc)
            self._lbl_cdp_status.setText(f"❌ Cookie 持久化失败：{exc}")
            return

        # 发射信号通知主程序
        self.login_success.emit({
            "cookies": cookies,
            "cookies_dict": cookies_dict,
            "wr_skey_length": info_skey_len(cookies),
            "cookie_count": len(cookies),
        })

        # 关闭对话框
        self._stop_cdp_worker()
        self.accept()

    # ------------------------------------------------------------------
    # 按钮槽
    # ------------------------------------------------------------------
    def _on_reload_page(self) -> None:
        log.info("[CDPLoginDialog] 重新加载页面")
        if self._web_engine_view:
            self._web_engine_view.reload()
            self._lbl_cdp_status.setText("🔄 页面已刷新，请重新扫码...")
            self._lbl_cdp_status.setStyleSheet(
                "color:#c49100;font-size:13px;font-weight:600;"
            )
            self._stop_cdp_worker()
            QTimer.singleShot(500, self._start_cdp_worker)

    def _on_done_clicked(self) -> None:
        """手动点击「扫码完成」按钮。"""
        log.info("[CDPLoginDialog] 用户手动点击完成按钮")
        if self._login_completed:
            return
        if self._latest_cookies:
            wr_skey = CDPWorker._find_cookie(self._latest_cookies, "wr_skey")
            if wr_skey and len(str(wr_skey.get("value", ""))) >= 8:
                self._finalize_login(self._latest_cookies)
            else:
                QMessageBox.warning(
                    self,
                    "登录未完成",
                    "未检测到有效的 wr_skey（长度需 ≥ 8）。\n\n"
                    "请确保已在手机上完成扫码登录。",
                )
        else:
            QMessageBox.information(
                self,
                "等待扫码",
                "正在等待扫码登录，请使用手机扫描页面上的二维码。",
            )

    # ------------------------------------------------------------------
    # 对话框关闭
    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802
        log.info("[CDPLoginDialog] 关闭对话框")
        self._stop_cdp_worker()
        if not self._login_completed:
            self.login_failed.emit("用户关闭对话框")
        super().closeEvent(event)

    def reject(self) -> None:
        log.info("[CDPLoginDialog] 用户取消")
        self._stop_cdp_worker()
        if not self._login_completed:
            self.login_failed.emit("用户取消登录")
        super().reject()


def info_skey_len(cookies: list[dict]) -> int:
    """从 Cookie 列表提取 wr_skey 长度。"""
    for c in cookies:
        if str(c.get("name", "")) == "wr_skey":
            return len(str(c.get("value", "")))
    return 0
