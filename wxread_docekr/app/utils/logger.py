"""日志工具（Docker headless 版）：stdout + /data/logs/app.log 按天轮转。

与桌面版的差异（Docker 独立副本，互不影响）：
  - LogBus 为纯 Python 信号总线（app.core.runtime.Signal），Web SSE 订阅
  - 只保留一个文件 handler（get_log_dir()/app.log），不再写 cwd app.log
  - backupCount 可配（config.app.log_retention_days，默认 7；
    环境变量 WXREAD_LOG_RETENTION_DAYS 可在启动时覆盖）
  - purge_old_logs() 双保险清理：启动时 + 每日 00:05（start_log_maintenance）
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from app.core.config import get_log_dir
from app.core.runtime import QObject, Signal

DEFAULT_RETENTION_DAYS = 7
_LOG_NAME = "app.log"
_DATED_RE = re.compile(r"^app\.log\.(\d{4}-\d{2}-\d{2})$")


class LogBus(QObject):
    """全局日志转发总线（Web SSE / 其他订阅者连接 log_emitted）。"""

    log_emitted = Signal(str, str)  # (level_name, message)


log_bus = LogBus()


class _SignalHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        log_bus.log_emitted.emit(record.levelname, msg)


def _wrap_flush(h: logging.Handler) -> None:
    """每条写完必 flush，避免长阻塞/崩溃时日志文件 0 字节。"""
    orig = h.handle

    def new_handle(record: logging.LogRecord) -> bool:
        try:
            return orig(record)
        finally:
            try:
                h.flush()
            except Exception:  # pragma: no cover
                pass

    h.handle = new_handle  # type: ignore[method-assign]


def resolve_retention_days() -> int:
    """保留天数解析：环境变量 WXREAD_LOG_RETENTION_DAYS > 配置文件 > 默认 7。"""
    env = os.environ.get("WXREAD_LOG_RETENTION_DAYS", "").strip()
    if env:
        try:
            return max(1, int(float(env)))
        except ValueError:
            pass
    try:
        from app.core.config import ConfigStore

        val = int(ConfigStore().get("app.log_retention_days", DEFAULT_RETENTION_DAYS) or 0)
        return max(1, val)
    except Exception:  # pragma: no cover
        return DEFAULT_RETENTION_DAYS


def _find_file_handler(logger: logging.Logger) -> logging.handlers.TimedRotatingFileHandler | None:
    for h in logger.handlers:
        if isinstance(h, logging.handlers.TimedRotatingFileHandler):
            return h
    return None


def set_retention_days(days: int) -> None:
    """热更新已安装文件 handler 的 backupCount（配置中心保存时调用）。"""
    days = max(1, int(days or DEFAULT_RETENTION_DAYS))
    root = logging.getLogger("wxread")
    fh = _find_file_handler(root)
    if fh is not None:
        fh.backupCount = days


def purge_old_logs(log_dir: str | os.PathLike[str] | None = None,
                   retention_days: int | None = None) -> list[str]:
    """清理超期/孤儿日志文件，返回被删除的文件名列表。

    规则：
      - app.log.YYYY-MM-DD（TimedRotatingFileHandler when=D 产物）：
        保留「今天 + 最近 retention_days-1 天」，更早删除
      - 其他以 app.log 开头的孤儿文件：按 mtime 判定，超 retention_days 天删除
      - 当日 app.log 永不删除
    """
    log_dir = Path(log_dir) if log_dir else get_log_dir()
    if retention_days is None:
        retention_days = resolve_retention_days()
    retention_days = max(1, int(retention_days))
    today = datetime.now().date()
    keep_from = today - timedelta(days=retention_days - 1)
    mtime_cutoff = time.time() - retention_days * 86400
    removed: list[str] = []
    if not log_dir.exists():
        return removed
    for p in log_dir.iterdir():
        if not p.is_file() or not p.name.startswith(_LOG_NAME):
            continue
        if p.name == _LOG_NAME:
            continue
        m = _DATED_RE.match(p.name)
        do_delete = False
        if m:
            try:
                file_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
                # 未来/非法日期（strptime 已限格式）不删
                do_delete = file_date < keep_from and file_date <= today
            except ValueError:
                do_delete = False
        else:
            # 孤儿文件：仅按 mtime 清理
            try:
                do_delete = p.stat().st_mtime < mtime_cutoff
            except OSError:
                do_delete = False
        if do_delete:
            try:
                p.unlink()
                removed.append(p.name)
            except OSError:
                pass
    return removed


def _install_handlers_on(logger: logging.Logger, level: int,
                         fmt: logging.Formatter, retention_days: int) -> None:
    # 控制台（docker logs）
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(level)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    # 文件：按天轮转
    log_dir = get_log_dir()
    fh = logging.handlers.TimedRotatingFileHandler(
        log_dir / _LOG_NAME, when="D", interval=1, backupCount=retention_days,
        encoding="utf-8", delay=False,
    )
    fh.setLevel(level)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # 信号转发（Web SSE 订阅同一总线）
    qh = _SignalHandler()
    qh.setLevel(level)
    qh.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(qh)

    for h in (sh, fh, qh):
        _wrap_flush(h)


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    root = logging.getLogger("wxread")
    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    already_installed = any(
        getattr(h, "__wxread_core__", False) for h in root.handlers
    )
    if not already_installed:
        retention_days = resolve_retention_days()
        _install_handlers_on(root, level, fmt, retention_days)
        for h in root.handlers:
            try:
                setattr(h, "__wxread_core__", True)
            except Exception:  # pragma: no cover
                pass
    root.propagate = False

    # lastResort 兜底到 stdout
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


def start_log_maintenance() -> None:
    """启动日志清理守护：立即清理一次 + 每天 00:05 再清理（双保险）。"""

    def _daily_loop() -> None:
        while True:
            try:
                removed = purge_old_logs()
                if removed:
                    logging.getLogger("wxread").info(
                        "日志清理：已删除 %d 个超期文件（%s）",
                        len(removed), ", ".join(removed[:5]),
                    )
            except Exception as exc:  # pragma: no cover
                logging.getLogger("wxread").warning("日志清理异常：%s", exc)
            now = datetime.now()
            nxt = (now + timedelta(days=1)).replace(
                hour=0, minute=5, second=0, microsecond=0
            )
            time.sleep(max(60.0, (nxt - now).total_seconds()))

    t = threading.Thread(target=_daily_loop, name="LogPurge", daemon=True)
    t.start()


def get_logger(name: str = "wxread") -> logging.Logger:
    setup_logging()
    if not name:
        mapped = "wxread"
    elif name == "wxread":
        mapped = "wxread"
    elif name.startswith("wxread."):
        mapped = name
    elif name.startswith("app."):
        mapped = "wxread" + name[3:]  # app.core.scheduler → wxread.core.scheduler
    else:
        mapped = "wxread." + name
    logger = logging.getLogger(mapped)
    if mapped != "wxread":
        logger.propagate = True
    return logger
