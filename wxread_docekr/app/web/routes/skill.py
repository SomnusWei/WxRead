"""Skill 服务路由：Key 校验 + 阅读统计拉取（同步线程池，阻塞 ≤ ~35s）。"""
from __future__ import annotations

from fastapi import APIRouter

from app.utils.logger import get_logger
from app.web.state import svc

log = get_logger(__name__)
router = APIRouter(prefix="/api/skill", tags=["skill"])


@router.post("/verify")
def verify():
    s = svc()
    try:
        ok, msg = s.skill.verify_api_key()
    except Exception as exc:  # noqa: BLE001
        log.exception("Skill Key 校验异常：%s", exc)
        return {"ok": False, "msg": f"校验异常：{exc}"}
    return {"ok": bool(ok), "msg": msg}


@router.get("/stats")
def stats():
    """从 Skill 拉取统计并落库（服务端为权威基线）。"""
    s = svc()
    data = s.skill.fetch_reading_stats()
    s.db.update_reading_stats(data)
    return {"ok": True, "data": s.db.get_reading_stats()}
