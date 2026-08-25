"""WxReadAssistant v2.0 入口脚本。

支持命令行参数：
  --minimized   启动后立即最小化到托盘（配合开机自启使用）

单例机制（Singleton）：
  启动时先用 QLocalSocket 连接到命名本地服务器 LOCAL_SERVER_NAME；
  · 如果能连上 → 说明已有实例在跑：发送 b"SHOW" 请求其激活窗口，然后本实例立即退出。
  · 如果连不上 → 本实例是第一个：listen 本地服务器，并在收到 SHOW 请求时激活主窗口。
"""
from __future__ import annotations

import argparse
import atexit
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMainWindow, QLabel, QMessageBox, QVBoxLayout, QWidget

# 本地命名服务器名（单例心跳）
LOCAL_SERVER_NAME = "WxReadAssistant-v2-Singleton-{8d31b27a-9c3a-4a05-9217-032b9d6e1e4a}"

TRACE_FILE = Path(os.environ.get("APPDATA", str(Path.home()))) / "WxReadAssistant" / "start_trace.log"


def _trace(msg: str) -> None:
    try:
        TRACE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with TRACE_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%H:%M:%S.%f')}] {msg}\n")
    except OSError:
        pass


class _PlaceholderWindow(QMainWindow):
    """Phase 1 占位窗口，Phase 8 替换为真正的 MainWindow。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("WxReadAssistant v2.0")
        self.resize(800, 600)
        central = _PlaceholderWidget()
        self.setCentralWidget(central)

    def activate_and_restore(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()


class _PlaceholderWidget(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        label = QLabel("WxReadAssistant v2.0\n\nPhase 1 框架就绪\n\n后续 Phase 将逐步填充主界面。")
        label.setAlignment(0x0084)  # AlignCenter
        layout.addWidget(label)


def main() -> int:
    try:
        TRACE_FILE.parent.mkdir(parents=True, exist_ok=True)
        TRACE_FILE.write_text("", encoding="utf-8")
    except OSError:
        pass
    _trace("boot: starting v2.0")
    atexit.register(lambda: _trace(f"atexit: sys.exitcode={getattr(sys, 'exitcode', None)}"))

    parser = argparse.ArgumentParser(description="微信读书助手 v2.0（Win桌面版）")
    parser.add_argument("--minimized", action="store_true", help="启动后立即最小化到托盘")
    args = parser.parse_args()

    try:
        app = QApplication(sys.argv)
        _trace(f"QApplication created; platformName={app.platformName()}")
    except Exception as exc:  # noqa: BLE001
        _trace(f"FAILED create QApplication: {exc}")
        print(f"QApplication failed: {exc}", file=sys.stderr)
        return 1

    app.setApplicationName("WxReadAssistant")
    app.setOrganizationName("WxReadAssistant")
    app.setQuitOnLastWindowClosed(False)

    # ================================ 单例检测 ================================
    from PySide6.QtNetwork import QLocalSocket, QLocalServer

    _trace("singleton: trying to connect existing instance ...")
    probe_socket = QLocalSocket()
    probe_socket.connectToServer(LOCAL_SERVER_NAME)
    connected_to_existing = probe_socket.waitForConnected(500)
    if connected_to_existing:
        _trace("singleton: existing instance found, sending SHOW activation request")
        probe_socket.write(b"SHOW")
        probe_socket.flush()
        probe_socket.waitForBytesWritten(1500)
        probe_socket.disconnectFromServer()
        probe_socket.deleteLater()
        try:
            QMessageBox.information(
                None,
                "微信读书助手",
                "检测到程序已经在运行中，已为您激活已打开的窗口。",
                QMessageBox.StandardButton.Ok,
                QMessageBox.StandardButton.Ok,
            )
        except Exception:  # noqa: BLE001
            pass
        return 0

    QLocalServer.removeServer(LOCAL_SERVER_NAME)
    singleton_server = QLocalServer(None)
    if not singleton_server.listen(LOCAL_SERVER_NAME):
        _trace(f"singleton: QLocalServer.listen FAILED: {singleton_server.errorString()}")
    else:
        _trace("singleton: I am the primary instance, local server is listening")

    # 先加载样式
    _trace("loading global qss")
    from app.ui.styles import apply_global_qss
    apply_global_qss(app)

    # 初始化日志
    _trace("setting up logging")
    from app.utils.logger import setup_logging
    setup_logging()
    log = __import__("app.utils.logger", fromlist=["get_logger"]).get_logger(__name__)

    # 记录启动版本/build（打包后也能通过日志追溯二进制）
    from app.ui.icon_store import APP_VERSION, app_version_display
    log.info(
        "===== WxReadAssistant v%s 启动 =====",
        app_version_display(),
    )
    log.info("APP_VERSION=%s ; APP_BUILD_ID will be injected by build_release.py", APP_VERSION)

    # 构造主窗口（Phase 8 替换为真正的 MainWindow）
    try:
        _trace("importing MainWindow")
        try:
            from app.ui.main_window import MainWindow
            _trace("constructing MainWindow (real)")
            win = MainWindow(start_minimized=args.minimized)
        except ImportError:
            _trace("MainWindow not yet implemented, using placeholder")
            win = _PlaceholderWindow()
        _trace("window constructed")
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        _trace(f"EXCEPTION during construct: {exc}\n{tb}")
        try:
            QMessageBox.critical(None, "启动失败", f"程序启动时发生异常：\n{exc}\n\n{tb}")
        except Exception:  # noqa: BLE001
            pass
        return 1

    # 单例 server 的 newConnection → 激活主窗口
    if singleton_server.isListening():
        def _on_singleton_request():
            sock = singleton_server.nextPendingConnection()
            if sock is None:
                return

            def _handle_ready_read():
                try:
                    raw = bytes(sock.readAll())
                    cmd = raw.decode("utf-8", errors="ignore")
                except Exception:  # noqa: BLE001
                    cmd = ""
                sock.close()
                sock.deleteLater()
                if cmd == "SHOW":
                    try:
                        win.activate_and_restore()
                    except Exception as exc2:  # noqa: BLE001
                        _trace(f"singleton: activate_and_restore raised: {exc2}")

            sock.readyRead.connect(_handle_ready_read)
            from PySide6.QtCore import QTimer
            QTimer.singleShot(600, lambda: (
                win.activate_and_restore() if sock.state() != sock.SocketState.UnconnectedState else None
            ))

        singleton_server.newConnection.connect(_on_singleton_request)

    try:
        if not args.minimized:
            win.show()
            _trace(f"win.show() done; visible={win.isVisible()}")
        else:
            _trace("skipped win.show() (--minimized)")
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        _trace(f"EXCEPTION during show: {exc}\n{tb}")
        return 1

    _trace("entering app.exec()")
    rc = app.exec()
    _trace(f"app.exec() returned rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
