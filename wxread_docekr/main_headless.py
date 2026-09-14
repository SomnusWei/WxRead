"""WxReadAssistant Docker v2.4.0 —— 无界面入口（uvicorn 5911 + 后台调度器）。

环境变量：
  WXREAD_WEB_TOKEN      设置后监听 0.0.0.0 并启用 Bearer 鉴权；
                        未设置时只监听 127.0.0.1（仅本机/SSH 隧道可达）
  WXREAD_LICENSE_CODE   启动时自动激活的注册码（可选）
  WXREAD_LICENSE_ADV=1  开放 /api/license/deactivate（默认关闭）
  WXREAD_LOG_RETENTION_DAYS  日志保留天数（默认 7）
  APPDATA               数据目录（镜像内默认 /data）
  TZ                    建议 Asia/Shanghai
"""
from __future__ import annotations

# —— 关键：任何 app.* 导入前先固定运行时与数据目录 ——
import os

os.environ.setdefault("WXREAD_RUNTIME", "pure")
os.environ.setdefault("APPDATA", "/data")

import json
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

APP_VERSION = "2.4.0"
DATA_DIR = Path(os.environ["APPDATA"])
HEARTBEAT_PATH = DATA_DIR / "heartbeat"
PORT = int(os.environ.get("WXREAD_PORT", "5911"))


def _bridge_signals(sched, api, log) -> None:
    """把 core 的纯 Python 信号桥接到日志（Web 状态走 HTTP 轮询/SSE）。"""
    sched.state_changed.connect(lambda text: log.info("[调度器] %s", text))
    sched.log_emitted.connect(lambda msg: log.info("[调度器] %s", msg))
    sched.progress.connect(
        lambda d: log.debug("[进度] %s", json.dumps(d, ensure_ascii=False))
    )
    sched.book_progress_updated.connect(
        lambda bid, pct, title, ch: log.info("《%s》进度更新 %d%%", title, pct)
    )
    sched.current_book_changed.connect(
        lambda title, idx: log.info("当前书籍：%s（shelf_index=%s）", title, idx)
    )
    sched.current_chapter_changed.connect(
        lambda title, uid: log.debug("当前章节：%s（uid=%s）", title, uid)
    )
    sched.reading_stats_updated.connect(
        lambda d: log.info("阅读统计已更新：today=%s", (d or {}).get("today_seconds"))
    )
    sched.cookie_broken.connect(
        lambda: log.warning("调度器报告：登录态已失效（cookie_broken）")
    )

    def _auto_qr_login() -> None:
        """Cookie 失效 → 自动唤起扫码并把二维码推送到微信。"""
        def _run() -> None:
            try:
                from app.web.routes.login import request_auto_login

                r = request_auto_login(reason="调度器巡检/启动检测到 Cookie 失效")
                if not r.get("ok"):
                    log.info("自动扫码未唤起：%s", r.get("code"))
            except Exception:  # noqa: BLE001
                log.exception("自动扫码唤起异常")

        threading.Thread(target=_run, name="Auto-QR-Trigger", daemon=True).start()

    sched.cookie_broken.connect(_auto_qr_login)
    sched.cookie_hard_invalid.connect(
        lambda reason: log.warning("登录态硬失效：%s（需重新扫码）", reason)
    )
    sched.task_completed.connect(
        lambda minutes: log.info("今日目标达成：%d 分钟", minutes)
    )

    api.message.connect(lambda m: log.info("[API] %s", m))
    api.warning.connect(lambda m: log.warning("[API] %s", m))
    api.error.connect(lambda m: log.error("[API] %s", m))
    api.cookie_invalid.connect(
        lambda severity, reason: log.warning("登录态失效信号 %s：%s", severity, reason)
    )


def _start_heartbeat(sched, stop_event: threading.Event) -> threading.Thread:
    def _loop() -> None:
        while not stop_event.is_set():
            try:
                st = sched.get_status()
                payload = {
                    "version": APP_VERSION,
                    "ts": datetime.now(timezone.utc).astimezone().isoformat(),
                    "running": bool(st.get("running")),
                    "paused": bool(st.get("paused")),
                    "auto_paused": bool(st.get("auto_paused")),
                    "state": st.get("state"),
                    "today_seconds": st.get("today_seconds"),
                    "success_count": st.get("success_count"),
                    "fail_count": st.get("fail_count"),
                }
                tmp = HEARTBEAT_PATH.with_suffix(".tmp")
                tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                tmp.replace(HEARTBEAT_PATH)
            except Exception:  # noqa: BLE001
                pass
            stop_event.wait(60)

    t = threading.Thread(target=_loop, name="Heartbeat", daemon=True)
    t.start()
    return t


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    from app.core.config import ConfigStore
    from app.core.licensing import activate as license_activate
    from app.core.licensing import get_status as license_get_status
    from app.core.local_db import LocalDB
    from app.core.notifier import WxPusherNotifier
    from app.core.scheduler import Scheduler
    from app.core.skill_api import SkillAPI
    from app.core.weread_api import WeReadApi
    from app.utils.logger import get_logger, setup_logging, start_log_maintenance
    from app.web.app import create_app
    from app.web.state import (
        get_persisted_auto_paused,
        has_valid_cookie,
        license_gate_ok,
    )

    setup_logging()
    start_log_maintenance()
    log = get_logger("wxread.headless")
    log.info("=" * 60)
    log.info("WxReadAssistant Docker v%s 启动（runtime=pure, data=%s）",
             APP_VERSION, DATA_DIR)

    cfg = ConfigStore()
    db = LocalDB()
    api = WeReadApi(cfg)
    skill = SkillAPI(cfg)
    notifier = WxPusherNotifier(cfg)
    sched = Scheduler(api, skill, db, cfg, notifier)
    _bridge_signals(sched, api, log)

    # 1) 启动时自动激活（如提供了注册码；失败/过期都不退出）
    code = os.environ.get("WXREAD_LICENSE_CODE", "").strip()
    if code:
        try:
            ok, msg = license_activate(code, db)
            (log.info if ok else log.warning)("启动自动激活：%s", msg)
        except Exception as exc:  # noqa: BLE001
            log.warning("启动自动激活异常：%s", exc)

    lic = license_get_status(db)
    log.info("授权状态：licensed=%s expired=%s minutes_left=%s",
             lic.get("licensed"), lic.get("expired"), lic.get("minutes_left"))

    # 2) 三门禁自动启动（不满足则保持待机，Web 可手动拉起）
    paused_info = get_persisted_auto_paused(cfg)
    if paused_info is not None:
        sched.apply_persisted_pause(paused_info)
        log.warning("存在持久化失败暂停标志，调度器保持暂停，等待人工恢复")
    elif not license_gate_ok(db):
        log.warning("授权不可用（过期且未激活），调度器待机；请在 Web 激活页输入注册码")
    elif not has_valid_cookie(cfg):
        log.warning("未检测到有效登录 Cookie，调度器待机；请先在 Web 扫码登录")
    elif not db.get_shelf():
        log.warning("本地书架为空，调度器待机；请先在 Web 点「同步书架」")
    else:
        sched.start()
        log.info("三门禁通过，调度器已自动启动")

    app = create_app(
        cfg=cfg, db=db, api=api, skill=skill, notifier=notifier, sched=sched,
        license_status=lic,
    )

    stop_event = threading.Event()
    _start_heartbeat(sched, stop_event)

    # 3) uvicorn 绑定策略：
    #    - WXREAD_WEB_HOST 显式指定时最高优先（反代/host 网络等场景）
    #    - 设置了 WXREAD_WEB_TOKEN → 0.0.0.0（有 Bearer 鉴权）
    #    - 容器内（WXREAD_IN_CONTAINER=1，镜像内置）未设 token → 仍绑
    #      0.0.0.0，否则 bridge 网络下端口映射不可达；安全边界由 compose
    #      默认只发布到宿主机 127.0.0.1 保证，启动日志红字提醒
    #    - 裸机直接运行且未设 token → 127.0.0.1（仅本机/SSH 隧道）
    token = os.environ.get("WXREAD_WEB_TOKEN", "").strip()
    forced_host = os.environ.get("WXREAD_WEB_HOST", "").strip()
    in_container = os.environ.get("WXREAD_IN_CONTAINER", "") == "1"
    if forced_host:
        host = forced_host
        host_reason = "WXREAD_WEB_HOST 显式指定"
    elif token:
        host = "0.0.0.0"
        host_reason = "已设置 Bearer Token"
    elif in_container:
        host = "0.0.0.0"
        host_reason = "容器内（依赖端口发布范围，compose 默认仅宿主机回环）"
    else:
        host = "127.0.0.1"
        host_reason = "未设置 Token，仅本机回环"
    import uvicorn

    config = uvicorn.Config(
        app, host=host, port=PORT, log_level="info",
        timeout_graceful_shutdown=30,
    )
    server = uvicorn.Server(config)

    def _graceful(signame: str) -> None:
        log.warning("收到 %s，开始优雅停机（≤30s）…", signame)
        stop_event.set()
        try:
            sched.requestInterruption()
            # _pause_event 反语义：set=放行，确保暂停中的循环也能看到中断标志
            try:
                sched._pause_event.set()  # noqa: SLF001
            except Exception:  # noqa: BLE001
                pass
            sched.wait(25000)
        except Exception as exc:  # noqa: BLE001
            log.warning("调度器停机异常：%s", exc)
        server.should_exit = True

    def _sigterm(_signum, _frame) -> None:
        _graceful("SIGTERM")

    def _sigint(_signum, _frame) -> None:
        _graceful("SIGINT")

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigint)

    log.info("Web 监听 http://%s:%d （%s）", host, PORT, host_reason)
    if in_container and not token and not forced_host:
        log.warning("⚠️ 未设置 WXREAD_WEB_TOKEN：当前仅在 compose 把端口发布到"
                    "宿主机 127.0.0.1 时安全；禁止把端口改为 0.0.0.0/公网发布")
    try:
        server.run()
    finally:
        log.info("Web 服务已退出，进程结束")


if __name__ == "__main__":
    main()
