"""Web 层共享服务容器与启动门禁（零业务逻辑，只做装配/判定）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.config import ConfigStore
from app.core.licensing import get_status as _license_status
from app.core.local_db import LocalDB
from app.core.notifier import WxPusherNotifier
from app.core.scheduler import Scheduler, is_valid_auto_paused
from app.core.skill_api import SkillAPI
from app.core.weread_api import WeReadApi


@dataclass
class Services:
    cfg: ConfigStore
    db: LocalDB
    api: WeReadApi
    skill: SkillAPI
    notifier: WxPusherNotifier
    sched: Scheduler


_services: Services | None = None


def init_services(services: Services) -> None:
    global _services
    _services = services


def svc() -> Services:
    if _services is None:
        raise RuntimeError("Services 尚未初始化（create_app 未调用）")
    return _services


def has_valid_cookie(cfg: ConfigStore) -> bool:
    cookies = cfg.get_cookies_dict()
    skey = str(cookies.get("wr_skey") or "")
    return bool(cookies.get("wr_vid")) and len(skey) >= 8


def license_gate_ok(db: LocalDB) -> bool:
    """已激活 或 试用未过期。"""
    lic = _license_status(db)
    return bool(lic.get("licensed") or not lic.get("expired"))


def get_persisted_auto_paused(cfg: ConfigStore) -> dict[str, Any] | None:
    """读取持久化暂停标志；脏值视为 None 并清掉。"""
    info = cfg.get("scheduler.auto_paused")
    if info is None:
        return None
    if is_valid_auto_paused(info):
        return info
    cfg.set("scheduler.auto_paused", None)
    return None


# 启动/手动启动门禁的错误码（前端据此给提示）
GATE_MSGS = {
    "LICENSE_REQUIRED": "授权已过期，请先在激活页输入注册码",
    "NOT_LOGGED_IN": "尚未登录，请先扫码登录或粘贴 Cookie",
    "AUTO_PAUSED": "调度器处于连续失败自动暂停状态，请排查日志后手动恢复",
    "NO_SHELF": "书架为空，请先点「获取数据」同步书架",
}


def start_scheduler(*, require_shelf: bool = True) -> tuple[bool, str]:
    """按门禁启动 Scheduler。返回 (是否已启动/运行中, 代码)。

    代码：STARTED / RUNNING / LICENSE_REQUIRED / NOT_LOGGED_IN /
          AUTO_PAUSED / NO_SHELF
    """
    s = svc()
    if not license_gate_ok(s.db):
        return False, "LICENSE_REQUIRED"
    if not has_valid_cookie(s.cfg):
        return False, "NOT_LOGGED_IN"
    if get_persisted_auto_paused(s.cfg) is not None:
        return False, "AUTO_PAUSED"
    if require_shelf and not s.db.get_shelf():
        return False, "NO_SHELF"
    if s.sched.isRunning():
        return True, "RUNNING"
    s.sched.start()
    # 启动瞬间若持久标志被重新写入，立即恢复暂停态
    info = get_persisted_auto_paused(s.cfg)
    if info is not None:
        s.sched.apply_persisted_pause(info)
        return False, "AUTO_PAUSED"
    return True, "STARTED"
