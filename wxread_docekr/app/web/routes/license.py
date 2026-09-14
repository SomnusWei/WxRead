"""授权路由：status / activate / deactivate（deactivate 需 WXREAD_LICENSE_ADV=1）。"""
from __future__ import annotations

import os

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from app.core.licensing import activate as _activate
from app.core.licensing import deactivate as _deactivate
from app.core.licensing import get_status
from app.web.state import start_scheduler, svc

router = APIRouter(prefix="/api/license", tags=["license"])


@router.get("/status")
def status():
    lic = get_status(svc().db)
    # 公开字段（不含任何密钥材料）
    return {
        "ok": True,
        "data": {
            "licensed": bool(lic.get("licensed")),
            "expired": bool(lic.get("expired")),
            "trial": bool(lic.get("trial")),
            "minutes_left": lic.get("minutes_left", 0),
            "hours_left": lic.get("hours_left", 0.0),
            "first_run_iso": lic.get("first_run_iso", ""),
            "masked_code": lic.get("masked_code", ""),
            "adv_mode": os.environ.get("WXREAD_LICENSE_ADV", "") == "1",
        },
    }


@router.post("/activate")
def activate(body: dict = Body(...)):
    code = str((body or {}).get("code") or "").strip()
    if not code:
        return JSONResponse(status_code=400,
                            content={"ok": False, "msg": "注册码不能为空"})
    ok, msg = _activate(code, svc().db)
    if not ok:
        return JSONResponse(status_code=200,
                            content={"ok": False, "msg": msg})
    # 激活成功后尝试自动拉起 Scheduler（书架未同步/未登录不影响激活结果）
    started, gate = start_scheduler(require_shelf=False)
    return {"ok": True, "msg": msg,
            "scheduler": {"started": started, "code": gate}}


if os.environ.get("WXREAD_LICENSE_ADV", "") == "1":
    @router.post("/deactivate")
    def deactivate():
        _deactivate(svc().db)
        return {"ok": True, "msg": "已取消激活"}
else:
    @router.post("/deactivate")
    def deactivate_disabled():
        # 显式 404（设计 §12.5）：避免落到静态挂载被解释成 405，也不泄露端点是否存在
        return JSONResponse(
            status_code=404,
            content={"ok": False, "code": "NOT_FOUND",
                     "msg": "未启用授权高级模式，该接口不可用"},
        )
