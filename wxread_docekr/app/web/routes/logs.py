"""日志路由：文件列表（日期白名单）/ 读取 / 下载 / tail / SSE 实时流。

安全约束：
  - 日期参数强制 ^\\d{4}-\\d{2}-\\d{2}$，文件名只允许 app.log / app.log.<date>
  - 不提供任何删除/清空接口（保留期由 TimedRotatingFileHandler 自动轮转）
"""
from __future__ import annotations

import json
import queue
import re
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.core.config import get_log_dir
from app.utils.logger import log_bus

router = APIRouter(prefix="/api/logs", tags=["logs"])

_LOG_NAME = "app.log"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATED_RE = re.compile(r"^app\.log\.(\d{4}-\d{2}-\d{2})$")
_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _log_dir() -> Path:
    return Path(get_log_dir())


def _resolve_log_file(day: str | None) -> Path | None:
    """把日期参数映射为日志文件，非法/不存在返回 None。"""
    today = date.today().isoformat()
    day = (day or today).strip()
    if not _DATE_RE.match(day):
        return None
    name = _LOG_NAME if day == today else f"{_LOG_NAME}.{day}"
    path = (_log_dir() / name).resolve()
    # 二次防穿越：解析后必须仍在日志目录、文件名精确匹配
    if path.parent != _log_dir().resolve() or path.name != name or not path.is_file():
        return None
    return path


def _list_files() -> list[dict]:
    d = _log_dir()
    if not d.exists():
        return []
    today = date.today().isoformat()
    out = []
    for p in d.iterdir():
        if not p.is_file():
            continue
        if p.name == _LOG_NAME:
            out.append({"date": today, "current": True, "size": p.stat().st_size})
        else:
            m = _DATED_RE.match(p.name)
            if m:
                out.append({"date": m.group(1), "current": False,
                            "size": p.stat().st_size})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


@router.get("/files")
def files():
    return {"ok": True, "data": _list_files()}


@router.get("/file")
def read_file(
    date: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=2000, ge=1, le=20000),
    download: int = Query(default=0),
):
    path = _resolve_log_file(date)
    if path is None:
        return JSONResponse(status_code=404,
                            content={"ok": False, "msg": "日志文件不存在或日期非法"})
    if download:
        return FileResponse(
            path=str(path), filename=path.name,
            media_type="text/plain; charset=utf-8",
        )
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        chunk = fh.read(limit)
        new_offset = fh.tell()
    return {
        "ok": True,
        "data": {
            "date": date or date.today().isoformat(),
            "total_size": path.stat().st_size,
            "offset": offset,
            "next_offset": new_offset,
            "eof": new_offset >= path.stat().st_size,
            "content": chunk,
        },
    }


def _tail_lines(path: Path, max_lines: int) -> str:
    # 日志单文件不大（按天轮转），直接读尾部后按行裁剪
    size = path.stat().st_size
    block = min(size, max_lines * 400 + 1024 * 512)
    with path.open("rb") as fh:
        fh.seek(size - block)
        raw = fh.read()
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


@router.get("/tail")
def tail(lines: int = Query(default=200, ge=1, le=2000),
         date: str | None = Query(default=None)):
    path = _resolve_log_file(date)
    if path is None:
        return JSONResponse(status_code=404,
                            content={"ok": False, "msg": "日志文件不存在或日期非法"})
    return {"ok": True, "data": {"content": _tail_lines(path, lines),
                                 "total_size": path.stat().st_size}}


@router.get("/stream")
def stream(level: str = Query(default="INFO")):
    """实时日志 SSE。level: DEBUG/INFO/WARNING/ERROR/CRITICAL（含更高级别）。"""
    lvl = (level or "INFO").upper()
    if lvl not in _LEVELS:
        lvl = "INFO"
    min_rank = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].index(lvl)
    q: queue.Queue = queue.Queue(maxsize=1000)

    def _on_log(level_name: str, message: str) -> None:
        try:
            rank = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].index(level_name)
        except ValueError:
            rank = 1
        if rank >= min_rank:
            try:
                q.put_nowait({"level": level_name, "msg": message})
            except queue.Full:
                pass

    log_bus.log_emitted.connect(_on_log)

    def gen():
        # 先补发当前文件尾部 200 行，便于打开即见上下文
        path = _resolve_log_file(None)
        if path is not None:
            try:
                for ln in _tail_lines(path, 200).splitlines():
                    yield f"data: {json.dumps({'level': 'INFO', 'msg': ln, 'backlog': True}, ensure_ascii=False)}\n\n"
            except Exception:  # noqa: BLE001
                pass
        yield "event: ready\ndata: {}\n\n"
        # 0.5s 粒度轮询：客户端断连时 GeneratorExit 能在下个边界及时注入，
        # 避免在 queue.get(20s) 长阻塞里残留僵尸线程；每 20s 发心跳。
        idle = 0.0
        try:
            while True:
                try:
                    item = q.get(timeout=0.5)
                    idle = 0.0
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    idle += 0.5
                    if idle >= 20:
                        idle = 0.0
                        yield ": heartbeat\n\n"
        finally:
            log_bus.log_emitted.disconnect(_on_log)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
