"""FastAPI 应用工厂：装配中间件、路由、静态资源。"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.utils.logger import get_logger
from app.web.auth import BearerTokenMiddleware
from app.web.routes import register_routers
from app.web.state import Services, init_services

log = get_logger(__name__)

APP_VERSION = "2.4.0"


def create_app(
    *,
    cfg,
    db,
    api,
    skill,
    notifier,
    sched,
    license_status: dict | None = None,
) -> FastAPI:
    init_services(Services(
        cfg=cfg, db=db, api=api, skill=skill, notifier=notifier, sched=sched,
    ))

    app = FastAPI(title="WxReadAssistant Docker", version=APP_VERSION)
    app.add_middleware(BearerTokenMiddleware)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": APP_VERSION}

    @app.get("/api/version")
    def version():
        return {
            "ok": True,
            "data": {
                "version": APP_VERSION,
                "build_id": os.environ.get("WXREAD_BUILD_ID", ""),
                "runtime": "pure",
            },
        }

    register_routers(app)

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app
