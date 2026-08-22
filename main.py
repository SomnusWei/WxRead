"""入口脚本：启动 Qt 应用并显示主窗口。

支持命令行参数：
  --minimized   启动后立即最小化到托盘（配合开机自启使用）
  --no-tray     禁用系统托盘（用于无头环境调试）

单例机制（Singleton）：
  启动时先用 QLocalSocket 连接到命名本地服务器 LOCAL_SERVER_NAME；
  · 如果能连上 → 说明已有实例在跑：发送 b"SHOW" 请求其激活窗口，然后本实例立即退出（不打开第二个）。
  · 如果连不上 → 本实例是第一个：listen 本地服务器，并在收到 SHOW 请求时把托盘/最小化的主界面弹到前台。
"""
from __future__ import annotations

import argparse
import atexit
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

# 本地命名服务器名（单例心跳）。加后缀 hash 避免和同名程序冲突
LOCAL_SERVER_NAME = "WxReadAssistant-Singleton-{8d31b27a-9c3a-4a05-9217-032b9d6e1e4a}"

TRACE_FILE = Path(os.environ.get("APPDATA", str(Path.home()))) / "WxReadAssistant" / "start_trace.log"


def _trace(msg: str) -> None:
    try:
        TRACE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with TRACE_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%H:%M:%S.%f')}] {msg}\n")
    except OSError:
        pass


def main() -> int:
    try:
        TRACE_FILE.parent.mkdir(parents=True, exist_ok=True)
        TRACE_FILE.write_text("", encoding="utf-8")
    except OSError:
        pass
    _trace("boot: starting")
    atexit.register(lambda: _trace(f"atexit: sys.exitcode={getattr(sys, 'exitcode', None)}"))

    parser = argparse.ArgumentParser(description="微信读书助手（Win桌面版）")
    parser.add_argument("--minimized", action="store_true", help="启动后立即最小化到托盘")
    parser.add_argument("--no-tray", action="store_true", help=argparse.SUPPRESS)
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
    app.setQuitOnLastWindowClosed(False)  # 关闭主窗口后由托盘/程序自行管理退出

    # ================================ 单例检测（在 MainWindow 构造之前）===============================
    from PySide6.QtNetwork import QLocalSocket, QLocalServer

    # 1) 先尝试连接到已运行实例
    _trace("singleton: trying to connect existing instance ...")
    probe_socket = QLocalSocket()
    probe_socket.connectToServer(LOCAL_SERVER_NAME)
    connected_to_existing = probe_socket.waitForConnected(500)
    if connected_to_existing:
        _trace("singleton: existing instance found, sending SHOW activation request")
        probe_socket.write(b"SHOW")
        probe_socket.flush()
        written_ok = probe_socket.waitForBytesWritten(1500)
        probe_socket.disconnectFromServer()
        probe_socket.deleteLater()
        _trace(f"singleton: request sent, bytesWritten OK={written_ok}. Exiting new instance.")
        try:
            # 给用户一个简短提示（stderr 输出 + 极短 Toast），如果 QApplication 已在运行就弹消息框
            QMessageBox.information(
                None,
                "微信读书助手",
                "检测到程序已经在运行中，已为您激活已打开的窗口。\n\n"
                "（如果它被最小化到托盘，也会自动恢复显示。）",
                QMessageBox.StandardButton.Ok,
                QMessageBox.StandardButton.Ok,
            )
        except Exception:  # noqa: BLE001
            pass
        return 0

    # 2) 连不上 → 我是第一个实例。先移除崩溃残留的旧名字，再 listen。
    QLocalServer.removeServer(LOCAL_SERVER_NAME)
    singleton_server = QLocalServer(None)  # 生命周期跟着 app 走即可
    if not singleton_server.listen(LOCAL_SERVER_NAME):
        _trace(f"singleton: QLocalServer.listen FAILED: {singleton_server.errorString()}."
               " (fallback: continue running, singleton guard disabled for this launch)")
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

    try:
        _trace("importing MainWindow")
        from app.ui.main_window import MainWindow
        _trace("constructing MainWindow")
        win = MainWindow(start_minimized=args.minimized)
        _trace("MainWindow constructed")
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        _trace(f"EXCEPTION during construct: {exc}\n{tb}")
        try:
            QMessageBox.critical(
                None,
                "启动失败",
                f"程序启动时发生异常：\n{exc}\n\n{tb}",
            )
        except Exception:  # noqa: BLE001
            pass
        return 1

    # ====== 把单例 server 的 newConnection 连到 MainWindow.activate_and_restore ======
    if singleton_server.isListening():
        def _on_singleton_request():
            # 把新实例发来的 SHOW 请求 → 交给主窗口弹到前台
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
            # 兜底：即使对方没发消息就断开（老版本/探测），也在 1s 后强制激活
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
