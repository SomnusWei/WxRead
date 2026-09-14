"""WxPusher 路由：发送测试通知（文本 SPT 通道 + 图片标准通道）。"""
from __future__ import annotations

from fastapi import APIRouter

from app.utils.logger import get_logger
from app.web.state import svc

log = get_logger(__name__)
router = APIRouter(prefix="/api/push", tags=["push"])


@router.post("/test")
def test():
    s = svc()
    push = s.cfg.get("push", {}) or {}
    spt = str(push.get("wxpusher_spt") or "").strip()
    results = []

    # ① 文本通道（SPT 极简推送）
    if spt:
        ok = s.notifier.send_sync(
            "WxReadAssistant 测试通知：文本推送通道正常 ✅"
        )
        results.append(("文本通道(SPT)", ok))
    else:
        results.append(("文本通道(SPT)", None))

    # ② 图片通道（标准推送 AppToken+UID）
    if s.notifier.image_channel_ready():
        ok_img, msg_img = s.notifier.send_test_image()
        results.append(("图片通道(AppToken/UID)", ok_img))
        if not ok_img:
            log.warning("图片通道测试失败：%s", msg_img)
    else:
        results.append(("图片通道(AppToken/UID)", None))

    lines = []
    for name, ok in results:
        if ok is True:
            lines.append(f"✅ {name}：已发送")
        elif ok is False:
            lines.append(f"❌ {name}：发送失败，请看日志")
        else:
            lines.append(f"⚪ {name}：未配置")
    any_ok = any(ok is True for _, ok in results)
    return {
        "ok": any_ok,
        "msg": "；".join(lines),
        "detail": [{"channel": n, "status": ok} for n, ok in results],
    }
