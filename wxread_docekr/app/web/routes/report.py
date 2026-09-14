"""年度报告路由：聚合 16 分区（5 分钟内存缓存，refresh=1 强制重建）。"""
from __future__ import annotations

import threading
import time

from fastapi import APIRouter

from app.core.report_aggregator import ReportAggregator
from app.utils.logger import get_logger
from app.web.state import svc

log = get_logger(__name__)
router = APIRouter(prefix="/api/report", tags=["report"])

_CACHE_TTL = 300
_cache_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "data": None}


@router.get("")
def get_report(refresh: int = 0):
    # 同步 def：FastAPI 自动丢到线程池执行（build 内可能调 Skill ≤30s）
    now = time.time()
    if not refresh:
        with _cache_lock:
            if _cache["data"] is not None and now - _cache["ts"] < _CACHE_TTL:
                return {"ok": True, "data": _cache["data"], "cached": True}
    s = svc()
    data = ReportAggregator(s.db, s.cfg, s.skill).build()
    with _cache_lock:
        _cache["ts"] = time.time()
        _cache["data"] = data
    return {"ok": True, "data": data, "cached": False}
