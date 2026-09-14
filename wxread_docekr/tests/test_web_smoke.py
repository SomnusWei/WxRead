# -*- coding: utf-8 -*-
"""Web 层本机冒烟（不依赖 Docker/Playwright/真实网络）。

用法： set APPDATA=tests/_tmp_web ; python tests/test_web_smoke.py
脚本内自己设置 APPDATA（必须在导入 app.* 之前）。
"""
import os
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["APPDATA"] = str(ROOT / "tests" / "_tmp_web")
os.environ["WXREAD_RUNTIME"] = "pure"

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import ConfigStore  # noqa: E402
from app.core.local_db import LocalDB  # noqa: E402
from app.core.notifier import WxPusherNotifier  # noqa: E402
from app.core.scheduler import Scheduler  # noqa: E402
from app.core.skill_api import SkillAPI  # noqa: E402
from app.core.weread_api import WeReadApi  # noqa: E402
from app.web.app import create_app  # noqa: E402
from app.core import runtime  # noqa: E402

_passed = 0
_failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


def main():
    # 每次从零开始（上次运行会留下书架/Cookie/授权）
    import shutil
    shutil.rmtree(ROOT / "tests" / "_tmp_web", ignore_errors=True)
    ConfigStore._instance = None
    cfg = ConfigStore()
    db = LocalDB()
    # 本机 Windows 注册表可能存有桌面端遗留的试用时间戳（会判定过期），
    # Web 冒烟直接生成一枚合法注册码激活（Linux 容器 winreg 不存在，无此问题）
    from app.core.licensing import activate as _activate, generate_code
    _activate(generate_code(), db)
    api = WeReadApi(cfg)
    skill = SkillAPI(cfg)
    notifier = WxPusherNotifier(cfg)
    sched = Scheduler(api, skill, db, cfg, notifier)
    app = create_app(cfg=cfg, db=db, api=api, skill=skill,
                     notifier=notifier, sched=sched)
    client = TestClient(app)

    # 1 健康检查/版本
    r = client.get("/healthz")
    check("healthz 200", r.status_code == 200 and r.json()["ok"], r.text)
    r = client.get("/api/version")
    check("version 2.4.0", r.json()["data"]["version"] == "2.4.0", r.text)

    # 2 PySide6 未加载
    check("PySide6 未被加载", "PySide6" not in sys.modules)

    # 3 无 Cookie → start 409 NOT_LOGGED_IN（试用未过期，门禁先卡登录）
    r = client.post("/api/scheduler/start")
    check("start 未登录 409",
          r.status_code == 409 and r.json()["code"] == "NOT_LOGGED_IN", r.text)

    # 4 status 快照字段
    r = client.get("/api/scheduler/status")
    j = r.json()
    st = j["data"]
    need = {"running", "state", "paused", "auto_paused", "target_minutes",
            "today_seconds", "progress_pct", "success_count", "fail_count",
            "logged_in", "cookie", "license", "shelf_count", "stats"}
    check("status 字段齐全", need.issubset(st.keys()), f"缺 {need - set(st.keys())}")
    check("status logged_in=false", st["logged_in"] is False)

    # 5 注入残缺 Cookie → 400
    r = client.post("/api/login/inject",
                    json={"cookies": {"wr_vid": "12345", "wr_skey": "abc"}})
    check("inject 短 skey 400", r.status_code == 400, r.text)

    # 6 注入完整 Cookie（不触发真实网络；set_session 只写配置+session）
    cookies = {"wr_vid": "12345678", "wr_skey": "abcdefgh12345678",
               "wr_pf": "x"}
    r = client.post("/api/login/inject", json={"cookies": cookies})
    check("inject 完整 cookie",
          r.status_code == 200 and r.json()["ok"] is True, r.text)
    r = client.get("/api/login/status")
    check("login status logged_in",
          r.json()["data"]["logged_in"] is True
          and r.json()["data"]["wr_skey_prefix"] == "abcd", r.text)

    # 7 未设 token 时鉴权中间件放行
    r2 = client.get("/api/login/status")
    check("无 token 模式放行", r2.status_code == 200)

    # 8 无书架 → start 409 NO_SHELF
    r = client.post("/api/scheduler/start")
    check("start 无书架 409 NO_SHELF",
          r.status_code == 409 and r.json()["code"] == "NO_SHELF", r.text)

    # 9 放假书架后 start → STARTED，再 start → RUNNING
    db.update_shelf([
        {"bookId": "b1", "title": "测试书甲", "author": "作者A", "progress": 10},
        {"bookId": "b2", "title": "读完的书", "author": "作者B", "progress": 100},
    ])
    r = client.post("/api/scheduler/start")
    check("start 三门禁通过",
          r.status_code == 200 and r.json()["code"] in ("STARTED", "RUNNING"), r.text)
    r = client.post("/api/scheduler/start")
    check("重复 start 返回 RUNNING", r.json()["code"] == "RUNNING", r.text)

    # 10 暂停 / 恢复 / 停止
    r = client.post("/api/scheduler/pause")
    check("pause", r.json()["ok"] and sched.get_status()["paused"] is True, r.text)
    r = client.post("/api/scheduler/resume")
    check("resume", r.json()["ok"] and sched.get_status()["paused"] is False, r.text)
    r = client.post("/api/scheduler/stop")
    # stop() 内 wait(3000) 请求线程退出，忙机器上收尾可能略超，轮询等待保证确定性
    import time as _time
    _deadline = _time.time() + 6
    while sched.isRunning() and _time.time() < _deadline:
        _time.sleep(0.2)
    check("stop", r.json()["ok"] and sched.isRunning() is False,
          f"{r.text} running={sched.isRunning()}")

    # 11 书架查询/筛选
    r = client.get("/api/shelf?filter=finished")
    j = r.json()
    check("shelf finished=1", j["total"] == 1 and j["data"][0]["title"] == "读完的书",
          r.text)
    r = client.get("/api/shelf?keyword=甲")
    check("shelf keyword", r.json()["total"] == 1, r.text)
    r = client.get("/api/shelf?filter=reading")
    check("shelf reading=1", r.json()["total"] == 1, r.text)

    # 12 黑名单（空）
    r = client.get("/api/books/blacklist")
    check("blacklist empty", r.json()["count"] == 0, r.text)

    # 13 配置 GET/PUT
    r = client.get("/api/config")
    check("config GET", r.json()["data"]["risk"]["fail_pause_threshold"] == 15, r.text)
    r = client.put("/api/config", json={"reading": {"min_hours": 2.5}})
    check("config PUT 部分更新",
          r.status_code == 200 and cfg.get("reading.min_hours") == 2.5, r.text)
    r = client.put("/api/config", json={"cookies": {"wr_skey": "x"}})
    check("config PUT 保护段 400", r.status_code == 400, r.text)
    r = client.put("/api/config", json={"hacker_section": {"x": 1}})
    check("config PUT 未知段 400", r.status_code == 400, r.text)

    # 14 授权状态
    r = client.get("/api/license/status")
    lj = r.json()["data"]
    check("license status",
          "licensed" in lj and "minutes_left" in lj and lj["adv_mode"] is False, r.text)

    # 15 日志接口
    r = client.get("/api/logs/files")
    check("logs files 列表", r.status_code == 200 and isinstance(r.json()["data"], list),
          r.text)
    r = client.get("/api/logs/file?date=2099-01-01")
    check("logs 未来日期 404", r.status_code == 404, r.text)
    r = client.get("/api/logs/file?date=../etc/passwd")
    check("logs 路径穿越 404", r.status_code == 404, r.text)
    r = client.get("/api/logs/tail?lines=50")
    check("logs tail 200", r.status_code == 200 and "content" in r.json()["data"], r.text)

    # 16 维护高危操作需确认
    r = client.post("/api/maintenance/reset-today", json={})
    check("reset-today 需确认", r.status_code == 400, r.text)
    r = client.post("/api/maintenance/clear-cookie", json={"confirm": True})
    check("clear-cookie 停调度+登出",
          r.status_code == 200 and sched.isRunning() is False
          and client.get("/api/login/status").json()["data"]["logged_in"] is False,
          r.text)

    # 17 push 未配置
    r = client.post("/api/push/test")
    check("push 未配置提示", r.json()["ok"] is False, r.text)

    # 18 报告兜底聚合（本地数据，不依赖 Skill 网络）
    db.update_shelf([{"bookId": "b1", "title": "测试书甲",
                      "author": "作者A", "progress": 10}])
    r = client.get("/api/report")
    j = r.json()
    check("report build 200",
          r.status_code == 200 and "kpi" in j["data"], r.text[:300])

    # 19 Bearer Token 鉴权（中间件请求时读环境变量，可进程内切换）
    os.environ["WXREAD_WEB_TOKEN"] = "secret-token-123"
    try:
        r = client.get("/api/login/status")
        check("无 token → 401", r.status_code == 401, r.text)
        r = client.get("/healthz")
        check("healthz 免鉴权", r.status_code == 200, r.text)
        r = client.get("/api/login/status",
                       headers={"Authorization": "Bearer secret-token-123"})
        check("Bearer 正确 → 200", r.status_code == 200, r.text)
        r = client.get("/api/login/status?access_token=secret-token-123")
        check("query token（SSE 兜底）→ 200", r.status_code == 200, r.text)
        r = client.get("/api/login/status?access_token=wrong")
        check("错误 token → 401", r.status_code == 401, r.text)
        # 静态页/资源必须匿名可达，否则浏览器无法加载令牌输入页（鸡生蛋问题）
        r = client.get("/")
        check("令牌模式下首页仍匿名可开", r.status_code == 200
              and "text/html" in r.headers.get("content-type", ""), r.text[:120])
        r = client.get("/app.js")
        check("静态资源 app.js 匿名可开", r.status_code == 200, r.text[:80])
        r = client.get("/api/version")
        check("version 始终公开", r.status_code == 200, r.text)
        r = client.post("/api/license/deactivate",
                        headers={"Authorization": "Bearer secret-token-123"})
        check("未开 ADV 时 deactivate 404", r.status_code == 404, r.text)
    finally:
        os.environ.pop("WXREAD_WEB_TOKEN", None)
    # 注：SSE 流式（日志/扫码）由 tests/test_sse_live.py 在真实 uvicorn 下验证，
    # Starlette TestClient 对无限流式响应的支持有缺陷会假死

    # 20 Scheduler 信号桥仍可用（runtime 实现）
    got = []
    sched.log_emitted.connect(lambda m: got.append(m))
    sched._emit_log("冒烟消息")  # noqa: SLF001
    check("scheduler 信号 emit", got == ["冒烟消息"], str(got))

    print(f"\n{'=' * 50}\n通过 {_passed} / 失败 {_failed}")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
