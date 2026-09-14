"""登录路由：Playwright 扫码（SSE）+ Cookie 粘贴兜底 + 登录态查询/导出。"""
from __future__ import annotations

import base64
import json
import os
import queue
import threading
import time
import uuid
from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse, StreamingResponse

from app.utils.logger import get_logger
from app.web.state import has_valid_cookie, start_scheduler, svc

log = get_logger(__name__)
router = APIRouter(prefix="/api/login", tags=["login"])

_TERMINAL_EVENTS = {"login_ok", "error", "expired"}
_SSE_DONE = object()


class _QRSession:
    def __init__(self, sid: str, *, source: str = "manual") -> None:
        self.sid = sid
        self.source = source  # manual=面板手动发起；auto=cookie 失效自动唤起
        self.events: list[dict] = []
        self.q: queue.Queue = queue.Queue()
        self.done = False
        self.lock = threading.Lock()

    def emit(self, ev: dict) -> None:
        with self.lock:
            self.events.append(ev)
            self.q.put(ev)

    def finish(self) -> None:
        with self.lock:
            self.done = True
            self.q.put(_SSE_DONE)


_sessions: dict[str, _QRSession] = {}
_sessions_lock = threading.Lock()


def _apply_login(cookies: dict[str, str], raw: list[dict]) -> tuple[bool, str]:
    """写入登录态 + 登录成功通知 + 按门禁拉起 Scheduler。"""
    s = svc()
    headers = s.cfg.get("headers", {}) or {}
    s.api.set_session(headers, cookies, raw)
    skey = str(cookies.get("wr_skey") or "")
    try:
        s.notifier.notify_login_success(len(cookies), len(skey))
    except Exception as exc:  # noqa: BLE001
        log.warning("登录成功通知异常：%s", exc)
    return start_scheduler(require_shelf=False)


# 自动扫码节流状态：last_ts=上次自动唤起时间戳
_auto_state = {"last_ts": 0.0}


def request_auto_login(reason: str = "") -> dict:
    """Cookie 失效后自动唤起一次扫码会话，并把二维码推送到微信。

    - SPT（文本告警）或标准通道（图片二维码）任一配置即唤起；
    - 任意会话（手动/自动）进行中时不重复唤起，手动操作永远优先；
    - 冷却 push.auto_qr_cooldown_min（默认 30 分钟）内不重复唤起；
    - 会话同样登记进 _sessions：面板 SSE 可订阅同一张二维码。
    返回 {"ok":..., "code":...} 供调用方记录日志。
    """
    s = svc()
    has_spt = bool(str(s.cfg.get("push.wxpusher_spt") or "").strip())
    if not has_spt and not s.notifier.image_channel_ready():
        return {"ok": False, "code": "NO_CHANNEL"}
    if not s.cfg.get("push.notify_login_qr", True):
        return {"ok": False, "code": "DISABLED"}
    try:
        cooldown = max(
            1, int(s.cfg.get("push.auto_qr_cooldown_min", 30))
        ) * 60
    except (TypeError, ValueError):
        cooldown = 30 * 60
    with _sessions_lock:
        if any(not x.done for x in _sessions.values()):
            return {"ok": False, "code": "SESSION_ACTIVE"}
        now = time.time()
        if now - _auto_state["last_ts"] < cooldown:
            return {"ok": False, "code": "COOLDOWN"}
        sid = uuid.uuid4().hex
        sess = _QRSession(sid, source="auto")
        _sessions[sid] = sess
        if len(_sessions) > 8:
            for old_sid in list(_sessions.keys())[:-8]:
                _sessions.pop(old_sid, None)
        _auto_state["last_ts"] = now
    log.warning("登录态失效，自动唤起扫码会话并推送二维码：%s", reason or "未注明")
    _start_worker(sess, auto=True, reason=reason)
    return {"ok": True, "code": "STARTED", "session_id": sid}


def _start_worker(sess: _QRSession, *, auto: bool = False, reason: str = "") -> None:
    def _worker() -> None:
        qr_pushed = False
        try:
            from tools.playwright_login import run_login_session

            proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None

            def emit(ev: dict) -> None:
                nonlocal qr_pushed
                sess.emit(ev)
                # 自动会话：二维码一出现就推微信；超时/失败补发过期提醒
                if not auto:
                    return
                if ev.get("event") == "qr" and ev.get("image_png"):
                    try:
                        svc().notifier.notify_login_qr(ev["image_png"], reason)
                        qr_pushed = True
                    except Exception:  # noqa: BLE001
                        log.exception("二维码推送异常")
                elif ev.get("event") == "error" and qr_pushed:
                    try:
                        svc().notifier.notify_login_qr_timeout()
                    except Exception:  # noqa: BLE001
                        log.exception("二维码过期提醒异常")

            result = run_login_session(emit, timeout=95, proxy=proxy)
            if result:
                # 硬失效场景下旧调度线程刚从 run() 返回，等其真正结束
                # 再走门禁启动，避免 isRunning() 竞态导致 start() 被吞
                if auto:
                    time.sleep(3)
                try:
                    started, code = _apply_login(
                        result["cookies"], result.get("cookies_raw") or []
                    )
                    sess.emit({"event": "applied", "scheduler_started": started,
                               "code": code, "source": sess.source})
                except Exception as exc:  # noqa: BLE001
                    log.exception("扫码登录态写入失败：%s", exc)
                    sess.emit({"event": "error", "msg": f"登录态写入失败：{exc}"})
        except Exception as exc:  # noqa: BLE001
            log.exception("扫码会话异常：%s", exc)
            sess.emit({"event": "error", "msg": f"扫码会话异常：{exc}"})
        finally:
            sess.finish()

    threading.Thread(
        target=_worker, name="QR-Login-Auto" if auto else "QR-Login", daemon=True
    ).start()


@router.post("/qr")
def qr_start():
    """启动一次扫码会话（同时只允许一个；旧会话收到 expired）。"""
    with _sessions_lock:
        for old in list(_sessions.values()):
            if not old.done:
                old.emit({"event": "expired", "msg": "已在别处发起新的扫码会话"})
        sid = uuid.uuid4().hex
        sess = _QRSession(sid)
        _sessions[sid] = sess
        # 防止无限堆积：只保留最近 8 个会话记录
        if len(_sessions) > 8:
            for old_sid in list(_sessions.keys())[:-8]:
                _sessions.pop(old_sid, None)
    _start_worker(sess)
    return {"ok": True, "session_id": sid, "timeout_sec": 95}


@router.get("/qr/stream")
def qr_stream(session_id: str):
    sess = _sessions.get(session_id)
    if sess is None:
        return JSONResponse(status_code=404,
                            content={"ok": False, "msg": "会话不存在或已清理"})

    def gen():
        # 重放历史事件（POST 与 GET 之间可能已有二维码产生）
        with sess.lock:
            replay = list(sess.events)
        sent = 0
        for ev in replay:
            sent += 1
            if ev.get("image_png"):
                ev = dict(ev)
                ev["image"] = "data:image/png;base64," + base64.b64encode(
                    ev.pop("image_png")).decode("ascii")
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if ev.get("event") in _TERMINAL_EVENTS:
                return
        while True:
            # 0.5s 粒度轮询：断连时 GeneratorExit 能及时注入（见 logs.stream）
            ev = None
            for _ in range(40):
                try:
                    ev = sess.q.get(timeout=0.5)
                    break
                except queue.Empty:
                    continue
            if ev is None:
                yield ": heartbeat\n\n"
                continue
            if ev is _SSE_DONE:
                return
            out = dict(ev)
            if out.get("image_png"):
                out["image"] = "data:image/png;base64," + base64.b64encode(
                    out.pop("image_png")).decode("ascii")
            yield f"data: {json.dumps(out, ensure_ascii=False)}\n\n"
            if out.get("event") in _TERMINAL_EVENTS:
                return

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/inject")
def inject(body: dict[str, Any] = Body(...)):
    """Cookie 粘贴兜底登录。"""
    cookies = body.get("cookies")
    raw = body.get("cookies_raw")
    if not isinstance(cookies, dict):
        return JSONResponse(status_code=400,
                            content={"ok": False, "msg": "cookies 必须是对象"})
    cookies = {str(k): str(v) for k, v in cookies.items() if v}
    skey = str(cookies.get("wr_skey") or "")
    if len(skey) < 8 or not cookies.get("wr_vid"):
        return JSONResponse(
            status_code=400,
            content={"ok": False,
                     "msg": "缺少 wr_vid 或 wr_skey 长度不足（至少 8 位）"},
        )
    if not isinstance(raw, list):
        # 仅有简化 dict 时合成最小 raw 记录
        raw = []
    clean_raw: list[dict] = []
    for r in raw:
        if isinstance(r, dict) and r.get("name") and r.get("value"):
            clean_raw.append(dict(r))
    seen = {str(r.get("name")) for r in clean_raw}
    for k, v in cookies.items():
        if k not in seen:
            clean_raw.append({
                "name": k, "value": v, "domain": ".weread.qq.com",
                "path": "/", "secure": True,
            })
    started, code = _apply_login(cookies, clean_raw)
    return {"ok": True, "msg": "Cookie 已写入并热加载",
            "scheduler_started": started, "code": code}


@router.get("/status")
def status():
    s = svc()
    cookies = s.cfg.get_cookies_dict()
    skey = str(cookies.get("wr_skey") or "")
    st = s.sched.get_status()
    return {
        "ok": True,
        "data": {
            "logged_in": has_valid_cookie(s.cfg),
            "wr_vid": str(cookies.get("wr_vid") or ""),
            "wr_skey_prefix": skey[:4],
            "wr_skey_len": len(skey),
            "scheduler_running": st.get("running"),
            "scheduler_state": st.get("state"),
            "auto_paused": st.get("auto_paused"),
        },
    }


@router.get("/export")
def export():
    """导出当前 Cookie JSON（迁移/排障用；受 Bearer Token 保护）。"""
    s = svc()
    return {
        "ok": True,
        "data": {
            "cookies": s.cfg.get_cookies_dict(),
            "cookies_raw": s.cfg.get_cookies_raw(),
            "headers": s.cfg.get("headers", {}),
        },
    }
