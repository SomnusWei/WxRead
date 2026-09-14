"""路由注册表（按 §12.5 端点表组织）。"""
from __future__ import annotations

from fastapi import FastAPI

from app.web.routes import (
    books,
    config as config_routes,
    license as license_routes,
    logs as logs_routes,
    login as login_routes,
    maintenance as maintenance_routes,
    push as push_routes,
    report as report_routes,
    scheduler as scheduler_routes,
    shelf as shelf_routes,
    skill as skill_routes,
    sync as sync_routes,
)


def register_routers(app: FastAPI) -> None:
    app.include_router(login_routes.router)
    app.include_router(config_routes.router)
    app.include_router(license_routes.router)
    app.include_router(skill_routes.router)
    app.include_router(push_routes.router)
    app.include_router(shelf_routes.router)
    app.include_router(books.router)
    app.include_router(sync_routes.router)
    app.include_router(report_routes.router)
    app.include_router(maintenance_routes.router)
    app.include_router(logs_routes.router)
    app.include_router(scheduler_routes.router)
