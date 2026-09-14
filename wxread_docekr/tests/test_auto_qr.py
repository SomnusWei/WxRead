# -*- coding: utf-8 -*-
"""Cookie 失效自动扫码+二维码 WxPusher 推送 冒烟（不起浏览器、不走真实网络）。

用法：set APPDATA=tests/_tmp_autoqr ; python tests/test_auto_qr.py
"""
import os
import sys
import pathlib
import shutil
import threading
import time
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["APPDATA"] = str(ROOT / "tests" / "_tmp_autoqr")
os.environ["WXREAD_RUNTIME"] = "pure"

from app.core.config import ConfigStore  # noqa: E402
from app.core.local_db import LocalDB  # noqa: E402
from app.core.notifier import WxPusherNotifier  # noqa: E402
from app.core.scheduler import Scheduler  # noqa: E402
from app.core.skill_api import SkillAPI  # noqa: E402
from app.core.weread_api import WeReadApi  # noqa: E402
from app.web.app import create_app  # noqa: E402
from app.web.routes import login as login_route  # noqa: E402

_passed = 0
_failed = 0
PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


def wait_until(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def install_fake_playwright(run_impl):
    """注入假 tools.playwright_login 模块（worker 内延迟 import 会命中）。"""
    mod = types.ModuleType("tools.playwright_login")
    mod.run_login_session = run_impl
    sys.modules["tools.playwright_login"] = mod


def main():
    shutil.rmtree(ROOT / "tests" / "_tmp_autoqr", ignore_errors=True)
    ConfigStore._instance = None
    cfg = ConfigStore()
    # Windows 下高频写盘易撞杀软文件锁；本测试只读内存态，统一不写盘
    _orig_cfg_set = cfg.set

    def _set_mem(key, value, *, auto_save=True):
        _orig_cfg_set(key, value, auto_save=False)

    cfg.set = _set_mem
    db = LocalDB()
    from app.core.licensing import activate as _activate, generate_code
    _activate(generate_code(), db)
    api = WeReadApi(cfg)
    skill = SkillAPI(cfg)
    notifier = WxPusherNotifier(cfg)
    sched = Scheduler(api, skill, db, cfg, notifier)
    app = create_app(cfg=cfg, db=db, api=api, skill=skill,
                     notifier=notifier, sched=sched)

    # 纯文本兜底统一 mock，避免真实网络
    fallback_calls = []
    notifier._send_qr_fallback_text = lambda reason, image_too_large: (
        fallback_calls.append((reason, image_too_large)) or True
    )

    # ---------- 1. notifier：无任何凭证 / 开关关闭 ----------
    check("无凭证走兜底且返回 False 语义", notifier.notify_login_qr(PNG_1X1))
    check("缺图片凭证即触发文本兜底",
          len(fallback_calls) == 1 and fallback_calls[0][1] is False,
          str(fallback_calls))
    cfg.set("push.wxpusher_spt", "SPT_testxxx")
    cfg.set("push.notify_login_qr", False)
    fallback_calls.clear()
    check("开关关闭不推", notifier.notify_login_qr(PNG_1X1) is False)
    check("开关关闭不触发兜底", len(fallback_calls) == 0)
    cfg.set("push.notify_login_qr", True)

    # ---------- 2. 标准通道 HTML POST payload 正确（mock requests.post） ----------
    cfg.set("push.wxpusher_app_token", "AT_testxxx")
    cfg.set("push.wxpusher_uid", "UID_testuid")
    check("image_channel_ready", notifier.image_channel_ready() is True)
    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"code": 1000, "msg": "成功", "data": 123}

    def fake_post(url, json=None, timeout=0):
        captured["url"] = url
        captured["json"] = json
        return _Resp()

    import app.core.notifier as notmod
    orig_post = notmod.requests.post
    notmod.requests.post = fake_post
    try:
        ok = notifier.notify_login_qr(PNG_1X1, reason="测试原因")
        check("HTML 图片推送受理", ok, str(captured))
        check("POST 走标准端点",
              captured["url"].endswith("/api/send/message"),
              captured.get("url"))
        body = captured["json"]
        check("带 appToken", body.get("appToken") == "AT_testxxx")
        check("带 uids 数组", body.get("uids") == ["UID_testuid"])
        check("contentType=2", body.get("contentType") == 2)
        check("HTML 内嵌 data:image/png base64",
              "data:image/png;base64," in body.get("content", ""))
        check("summary 非空且≤20",
              bool(body.get("summary")) and len(body["summary"]) <= 20)
        # send_test_image 同通道
        ok2, msg2 = notifier.send_test_image()
        check("send_test_image 受理", ok2, msg2)
    finally:
        notmod.requests.post = orig_post

    # ---------- 3. 超大 PNG → 纯文本兜底（不抛异常） ----------
    fallback_calls.clear()
    big_png = b"\x89PNG\r\n" + b"a" * 40000
    check("超大图走兜底", notifier.notify_login_qr(big_png, "r") is True)
    check("兜底标记 image_too_large",
          fallback_calls and fallback_calls[0][1] is True,
          str(fallback_calls))

    # ---------- 4. request_auto_login 门禁分支 ----------
    cfg.set("push.wxpusher_spt", "")
    cfg.set("push.wxpusher_app_token", "")
    cfg.set("push.wxpusher_uid", "")
    r = login_route.request_auto_login("t")
    check("无任何通道 → NO_CHANNEL", r["code"] == "NO_CHANNEL", str(r))
    cfg.set("push.wxpusher_spt", "SPT_testxxx")

    # 记录 notifier 调用（避免真实网络）
    pushed_qr = []
    pushed_timeout = []
    notifier.notify_login_qr = lambda png, reason="": pushed_qr.append((png, reason)) or True
    notifier.notify_login_qr_timeout = lambda: pushed_timeout.append(1)

    # _apply_login mock（避免真实门禁/调度器线程）
    applied = []

    def fake_apply(cookies, raw):
        applied.append(cookies)
        return True, "STARTED"

    login_route._apply_login = fake_apply

    gate = threading.Event()

    def fake_run_gated(emit, timeout=95, proxy=None):
        emit({"event": "qr", "image_png": PNG_1X1})
        gate.wait(8)
        return None

    install_fake_playwright(fake_run_gated)
    r = login_route.request_auto_login("硬失效")
    check("首次自动唤起 STARTED", r.get("code") == "STARTED", str(r))
    check("自动会话 source=auto 入池",
          any(x.source == "auto" and not x.done
              for x in login_route._sessions.values()))
    r2 = login_route.request_auto_login("再次失效")
    check("会话进行中 → SESSION_ACTIVE", r2["code"] == "SESSION_ACTIVE", str(r2))
    check("二维码已交给 notifier",
          wait_until(lambda: len(pushed_qr) == 1) and pushed_qr[0][0] == PNG_1X1
          and pushed_qr[0][1] == "硬失效",
          str(pushed_qr))
    # 放行 gated 会话（返回 None = 超时失败）
    gate.set()
    check("会话结束落 done",
          wait_until(lambda: all(x.done for x in login_route._sessions.values())))
    r3 = login_route.request_auto_login("冷却期")
    check("冷却期 → COOLDOWN", r3["code"] == "COOLDOWN", str(r3))

    # ---------- 5. 成功路径：qr→login_ok→写登录态 ----------
    login_route._auto_state["last_ts"] = 0.0
    success_session = {
        "cookies": {"wr_vid": "12345678", "wr_skey": "abcdefgh12345678"},
        "cookies_raw": [{"name": "wr_vid", "value": "12345678"}],
    }

    def fake_run_ok(emit, timeout=95, proxy=None):
        emit({"event": "status", "msg": "打开登录页"})
        emit({"event": "qr", "image_png": PNG_1X1})
        emit({"event": "login_ok"})
        return success_session

    install_fake_playwright(fake_run_ok)
    r = login_route.request_auto_login("恢复测试")
    check("成功路径 STARTED", r.get("code") == "STARTED", str(r))
    check("_apply_login 被调用（自动恢复）",
          wait_until(lambda: len(applied) == 1)
          and applied[0].get("wr_skey") == "abcdefgh12345678",
          str(applied))
    check("无超时提醒（成功路径）", len(pushed_timeout) == 0, str(pushed_timeout))

    # ---------- 6. 超时路径：qr→error → 过期提醒 ----------
    login_route._auto_state["last_ts"] = 0.0

    def fake_run_timeout(emit, timeout=95, proxy=None):
        emit({"event": "qr", "image_png": PNG_1X1})
        emit({"event": "error", "msg": "扫码超时（约 95 秒）"})
        return None

    install_fake_playwright(fake_run_timeout)
    r = login_route.request_auto_login("超时测试")
    check("超时路径 STARTED", r.get("code") == "STARTED", str(r))
    check("超时补发过期提醒", wait_until(lambda: len(pushed_timeout) == 1),
          str(pushed_timeout))

    # ---------- 7. 手动会话优先：手动进行中自动请求被拒 ----------
    login_route._auto_state["last_ts"] = 0.0
    manual_gate = threading.Event()
    install_fake_playwright(
        lambda emit, timeout=95, proxy=None: (manual_gate.wait(8), None)[1]
    )
    from fastapi.testclient import TestClient
    client = TestClient(app)
    rr = client.post("/api/login/qr")
    check("手动扫码 200", rr.status_code == 200 and rr.json()["ok"], rr.text)
    r = login_route.request_auto_login("手动优先")
    check("手动会话进行中 → SESSION_ACTIVE", r["code"] == "SESSION_ACTIVE", str(r))
    manual_gate.set()
    wait_until(lambda: all(x.done for x in login_route._sessions.values()))

    print(f"\n==== 自动扫码冒烟：{_passed} passed / {_failed} failed ====")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
