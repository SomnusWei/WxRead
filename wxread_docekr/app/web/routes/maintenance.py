"""数据维护路由：清零今日计时 / 登出（停 Scheduler + 清 Cookie，进程不退出）。"""
from __future__ import annotations

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from app.utils.logger import get_logger
from app.web.state import svc

log = get_logger(__name__)
router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])


def _require_confirm(body: dict | None) -> JSONResponse | None:
    if not isinstance(body, dict) or body.get("confirm") is not True:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "msg": "高危操作需要二次确认（confirm=true）"},
        )
    return None


@router.post("/reset-today")
def reset_today(body: dict = Body(default={})):
    """清零本地「今日已读秒数」（只影响本地估算；同步 Skill 后以服务端为准）。"""
    err = _require_confirm(body)
    if err:
        return err
    val = svc().db.reset_today_seconds()
    log.warning("Web 维护：今日计时已手动清零（0 秒）")
    return {"ok": True, "today_seconds": val, "msg": "今日本地计时已清零"}


@router.post("/clear-cookie")
def clear_cookie(body: dict = Body(default={})):
    """登出：先停调度器，再清 Cookie；容器进程不退出，可重新扫码。"""
    err = _require_confirm(body)
    if err:
        return err
    s = svc()
    try:
        if s.sched.isRunning():
            s.sched.stop()
    except Exception as exc:  # noqa: BLE001
        log.warning("停止调度器异常（继续登出）：%s", exc)
    s.api.clear_session()
    log.warning("Web 维护：已登出（调度器停止、Cookie 清除）")
    return {"ok": True, "msg": "已登出，调度器已停止，请重新扫码登录"}
