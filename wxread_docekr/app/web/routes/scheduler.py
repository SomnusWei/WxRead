"""调度器控制路由：start/stop/pause/resume/regenerate-plan/status。"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.licensing import get_status
from app.web.state import GATE_MSGS, has_valid_cookie, start_scheduler, svc

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


def _err(code: str, http_status: int) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content={"ok": False, "code": code, "msg": GATE_MSGS.get(code, code)},
    )


@router.post("/start")
def start():
    ok, code = start_scheduler(require_shelf=True)
    if ok:
        return {"ok": True, "code": code, "msg": "调度器已启动" if code == "STARTED" else "调度器运行中"}
    status = 403 if code == "LICENSE_REQUIRED" else 409
    return _err(code, status)


@router.post("/stop")
def stop():
    s = svc()
    if s.sched.isRunning():
        s.sched.stop()
    return {"ok": True, "msg": "调度器已停止"}


@router.post("/pause")
def pause():
    svc().sched.pause()
    return {"ok": True, "msg": "已暂停"}


@router.post("/resume")
def resume():
    """自动暂停/手动暂停的统一人工恢复入口。"""
    svc().sched.manual_resume()
    return {"ok": True, "msg": "已恢复，失败计数与冷却档位已复位"}


@router.post("/regenerate-plan")
def regenerate_plan():
    svc().sched.regenerate_plan()
    return {"ok": True, "msg": "今日目标已清空，下一轮重新随机生成"}


@router.get("/status")
def status():
    s = svc()
    data = s.sched.get_status()
    cookies = s.cfg.get_cookies_dict()
    skey = str(cookies.get("wr_skey") or "")
    lic = get_status(s.db)
    data["logged_in"] = has_valid_cookie(s.cfg)
    data["cookie"] = {
        "wr_vid": str(cookies.get("wr_vid") or ""),
        "wr_skey_prefix": skey[:4],
        "wr_skey_len": len(skey),
    }
    data["license"] = {
        "licensed": bool(lic.get("licensed")),
        "expired": bool(lic.get("expired")),
        "minutes_left": lic.get("minutes_left", 0),
        "masked_code": lic.get("masked_code", ""),
    }
    data["shelf_count"] = len(s.db.get_shelf())
    return {"ok": True, "data": data}
