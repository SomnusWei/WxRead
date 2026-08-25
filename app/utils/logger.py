"""日志工具：写文件 + 事件总线，UI 端可订阅 Qt 信号显示。"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from app.core.config import get_log_dir


class LogBus(QObject):
    """全局日志转发到 UI 的轻量信号总线。"""

    log_emitted = Signal(str, str)  # (level_name, message)


log_bus = LogBus()


class _QtSignalHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        log_bus.log_emitted.emit(record.levelname, msg)


def _wrap_flush(h: logging.Handler) -> None:
    """对一个已经安装好的 handler，加"每条写完必 flush"包裹，避免进程崩溃
    或业务长时间阻塞时 app.log 还是 0 字节。"""
    orig = h.handle

    def new_handle(record: logging.LogRecord) -> bool:
        try:
            return orig(record)
        finally:
            try:
                h.flush()
            except Exception:  # pragma: no cover - 写盘失败时不影响业务
                pass

    h.handle = new_handle  # type: ignore[method-assign]


def _install_handlers_on(logger: logging.Logger, level: int, fmt: logging.Formatter) -> None:
    # 控制台（关闭缓冲立即 flush）
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(level)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    # 文件 1：%APPDATA%/WxReadAssistant/logs/app.log（官方长期持久化路径）
    log_dir: Path = get_log_dir()
    fh = logging.handlers.TimedRotatingFileHandler(
        log_dir / "app.log", when="D", interval=1, backupCount=14,
        encoding="utf-8", delay=False,
    )
    fh.setLevel(level)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # 文件 2：项目根目录/当前工作目录下 app.log（兼容用户查看习惯，
    # 避免用户翻到 APPDATA 找不到日志）。和文件 1 内容完全一致。
    try:
        cwd_log: Path = Path.cwd() / "app.log"
        fh_cwd = logging.handlers.TimedRotatingFileHandler(
            str(cwd_log), when="D", interval=1, backupCount=3,
            encoding="utf-8", delay=False,
        )
        fh_cwd.setLevel(level)
        fh_cwd.setFormatter(fmt)
        logger.addHandler(fh_cwd)
    except Exception:  # pragma: no cover - 只读目录/权限不足兜底
        fh_cwd = None

    # UI 信号转发（只在根 logger 装一次，避免 emit 多次）
    qh = _QtSignalHandler()
    qh.setLevel(level)
    qh.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(qh)

    all_handlers = [sh, fh, qh]
    if fh_cwd is not None:
        all_handlers.append(fh_cwd)
    for h in all_handlers:
        _wrap_flush(h)


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    root = logging.getLogger("wxread")
    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    already_installed = any(
        getattr(h, "__wxread_core__", False) for h in root.handlers
    )
    if not already_installed:
        _install_handlers_on(root, level, fmt)
        for h in root.handlers:
            try:
                setattr(h, "__wxread_core__", True)
            except Exception:  # pragma: no cover
                pass
    root.propagate = False  # 根 logger 不再向 Python 根冒泡（避免重复输出 stderr）

    # 顺手把 logging.lastResort 降级
    class _SoftLastResort(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
            try:
                sys.stdout.write(self.format(record) + "\n")
                sys.stdout.flush()
            except Exception:
                pass
    if not getattr(logging.lastResort, "__wxread__", False):
        lr = _SoftLastResort()
        lr.setLevel(logging.WARNING)
        lr.setFormatter(logging.Formatter("[lastResort] %(levelname)s %(name)s: %(message)s"))
        setattr(lr, "__wxread__", True)
        logging.lastResort = lr  # type: ignore[assignment]

    return root


def get_logger(name: str = "wxread") -> logging.Logger:
    setup_logging()
    if not name:
        mapped = "wxread"
    elif name == "wxread":
        mapped = "wxread"
    elif name.startswith("wxread."):
        mapped = name
    elif name.startswith("app."):
        mapped = "wxread" + name[3:]  # app.ui.login_page → wxread.ui.login_page
    else:
        mapped = "wxread." + name
    logger = logging.getLogger(mapped)
    if mapped != "wxread":
        logger.propagate = True
    return logger
