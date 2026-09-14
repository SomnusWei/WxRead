# -*- coding: utf-8 -*-
"""真实 uvicorn + httpx 的 SSE 实测（TestClient 对流式响应会假死）。

覆盖：
  1. /api/logs/stream      收到 ready、主动断连、断后服务可用
  2. /api/login/qr + SSE   本机无 Playwright → 应在 SSE 收到 terminal error 事件
  3. 未知 session_id → 404

用法： python tests/test_sse_live.py   （退出码 0=全过）
"""
import os, sys, pathlib, time, threading, json
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["APPDATA"] = str(ROOT / "tests" / "_tmp_web2")
os.environ["WXREAD_RUNTIME"] = "pure"

import httpx, uvicorn
from app.core.config import ConfigStore
from app.core.local_db import LocalDB
from app.core.notifier import WxPusherNotifier
from app.core.scheduler import Scheduler
from app.core.skill_api import SkillAPI
from app.core.weread_api import WeReadApi
from app.web.app import create_app

_failed = 0


def check(name, cond, detail=""):
    global _failed
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  {detail}" if not cond else ""),
          flush=True)
    if not cond:
        _failed += 1


def main():
    import shutil
    shutil.rmtree(ROOT / "tests" / "_tmp_web2", ignore_errors=True)
    ConfigStore._instance = None
    from app.utils.logger import setup_logging, get_logger
    setup_logging()
    slog = get_logger("sse_live")
    for i in range(5):
        slog.info("SSE 实测历史日志 %d", i)
    cfg = ConfigStore(); db = LocalDB()
    api = WeReadApi(cfg); skill = SkillAPI(cfg)
    app = create_app(cfg=cfg, db=db, api=api, skill=skill,
                     notifier=WxPusherNotifier(cfg),
                     sched=Scheduler(api, skill, db, cfg))

    config = uvicorn.Config(app, host="127.0.0.1", port=59511, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    base = "http://127.0.0.1:59511"

    # 1 日志 SSE
    got_ready = got_backlog = False
    t0 = time.time()
    with httpx.stream("GET", base + "/api/logs/stream?level=DEBUG", timeout=10) as r:
        check("logs SSE 200", r.status_code == 200
              and "text/event-stream" in r.headers.get("content-type", ""))
        for line in r.iter_lines():
            if line.startswith("data: ") and "backlog" in line:
                got_backlog = True
            if line.startswith("event: ready"):
                got_ready = True
                break
    check("logs SSE backlog+ready（%.1fs）" % (time.time() - t0), got_ready)
    check("logs SSE 补发了历史日志", got_backlog)
    check("断连后服务可用",
          httpx.get(base + "/api/scheduler/status", timeout=5).status_code == 200)

    # 2 扫码 SSE（本机无 Playwright，期望快速收到 error 终态）
    r = httpx.post(base + "/api/login/qr", timeout=10)
    check("qr POST 200 带 session_id",
          r.status_code == 200 and bool(r.json().get("session_id")), r.text)
    sid = r.json()["session_id"]

    terminal = None
    with httpx.stream("GET", base + f"/api/login/qr/stream?session_id={sid}",
                      timeout=20) as resp:
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            if ev.get("event") in ("error", "login_ok", "expired"):
                terminal = ev.get("event")
                break
    check("扫码 SSE 收到终态（本机无 playwright 应为 error）",
          terminal == "error", f"terminal={terminal}")

    # 3 未知会话 404
    r = httpx.get(base + "/api/login/qr/stream?session_id=deadbeef", timeout=5)
    check("未知扫码会话 404", r.status_code == 404, r.text)

    server.should_exit = True
    print(f"\n失败 {_failed}", flush=True)
    os._exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
