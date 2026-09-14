"""配置中心路由：GET 全量 / PUT 部分更新（深合并，受保护段禁止改写）。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from app.core.config import DEFAULT_CONFIG
from app.utils.logger import set_retention_days
from app.web.state import svc

router = APIRouter(prefix="/api/config", tags=["config"])

# 不允许通过配置中心写入的顶层段（走专用接口/运行态）
_PROTECTED = {"cookies", "cookies_raw", "headers", "scheduler", "daily_plan",
              "risk_alerts"}


@router.get("")
def get_config():
    return {"ok": True, "data": svc().cfg.data, "defaults": DEFAULT_CONFIG}


@router.put("")
def update_config(patch: dict[str, Any] = Body(...)):
    if not isinstance(patch, dict) or not patch:
        return JSONResponse(status_code=400,
                            content={"ok": False, "msg": "请求体必须是非空对象"})
    bad = [k for k in patch if k in _PROTECTED]
    if bad:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "msg": f"以下配置段受保护不可改写：{', '.join(bad)}"},
        )
    unknown = [k for k in patch if k not in DEFAULT_CONFIG]
    if unknown:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "msg": f"未知配置段：{', '.join(unknown)}"},
        )
    cfg = svc().cfg
    for top, value in patch.items():
        default_section = DEFAULT_CONFIG.get(top)
        if isinstance(default_section, dict) and isinstance(value, dict):
            # 仅允许默认结构内的叶子键，防止写进脏键
            clean = {k: v for k, v in value.items() if k in default_section}
            cfg.update_dict(top, clean)
        else:
            cfg.set(top, value)
    # 热生效副作用
    try:
        retention = cfg.get("app.log_retention_days")
        if retention:
            set_retention_days(int(retention))
    except (TypeError, ValueError):
        pass
    return {"ok": True, "msg": "已保存（部分参数需重启调度器生效，表单项已标注）"}
