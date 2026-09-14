"""WxPusher 推送封装（v2 多规则版）。

规则开关（来自 config.push）：
  - notify_daily_start    每日首次开始（包含今日目标）
  - notify_cookie_fail    Cookie 失效通知
  - notify_login_qr       Cookie 失效后自动唤起扫码并推送二维码图片
  - notify_daily_done     任务完成发送
  - notify_login_success  登录成功通知

所有推送异步执行（独立线程），失败带退避重试，独立 dedup_key 节流。
"""
from __future__ import annotations

import base64
import random
import threading
import time
from typing import Callable

import requests

from app.core.config import ConfigStore
from app.utils.logger import get_logger

log = get_logger(__name__)


def format_duration(minutes: int) -> str:
    """把分钟数格式化为「X小时Y分钟」形式（用户可读）。

    规则：492 → 8小时12分钟；480 → 8小时；12 → 12分钟；0 → 0分钟
    """
    minutes = max(0, int(minutes))
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}小时{m}分钟"
    if h:
        return f"{h}小时"
    return f"{m}分钟"


class WxPusherNotifier:
    """WxPusher 推送器（v2 多规则版）。"""

    _endpoint = "https://wxpusher.zjiecode.com/api/send/message/{spt}/{content}"
    # 标准推送端点（AppToken + UID，contentType=2 HTML，用于内嵌二维码图片；
    # SPT 极简推送仅支持纯文本，发图片必须走此通道）
    _std_endpoint = "https://wxpusher.zjiecode.com/api/send/message"

    # WxPusher 单条 content 字符上限 40000，预留 HTML 外壳余量
    _MAX_HTML_BASE64_LEN = 36000

    def image_channel_ready(self) -> bool:
        """图片通道（标准推送 AppToken+UID）是否已配置。"""
        return bool(
            str(self._cfg.get("push.wxpusher_app_token") or "").strip()
            and str(self._cfg.get("push.wxpusher_uid") or "").strip()
        )

    def __init__(self, config: ConfigStore | None = None) -> None:
        self._cfg = config or ConfigStore()
        self._lock = threading.Lock()
        # 去重节流：{dedup_key: last_fired_timestamp}
        self._last_fired_key: dict[str, float] = {}

    # ---------------- 通用发送 ----------------
    def send_async(
        self,
        content: str,
        *,
        max_attempts: int = 3,
        dedup_key: str | None = None,
        dedup_window_sec: int = 3600,
        on_done: Callable[[bool], None] | None = None,
    ) -> None:
        """异步发送：立即返回；发送完成通过 on_done 回调（可能在任意线程）。"""
        threading.Thread(
            target=self._run_send,
            args=(content, max_attempts, dedup_key, dedup_window_sec, on_done),
            name="WxPusher-Send",
            daemon=True,
        ).start()

    def send_sync(self, content: str, *, max_attempts: int = 3) -> bool:
        """同步发送（阻塞当前线程直到完成）。"""
        return self._do_send(content, max_attempts)

    # ---------------- 多规则便捷方法 ----------------
    def notify_daily_start(self, target_minutes: int) -> None:
        """每日首次开始：包含今日目标。dedup_key=daily_start_<date> 当日仅一次。"""
        if not self._cfg.get("push.notify_daily_start", False):
            return
        from datetime import date
        dedup = f"daily_start_{date.today().isoformat()}"
        content = (
            f"📚 微信读书助手已启动\n"
            f"📅 日期：{date.today().isoformat()}\n"
            f"🎯 今日目标：{format_duration(target_minutes)}"
        )
        self.send_async(content, dedup_key=dedup, dedup_window_sec=86400)

    def notify_cookie_fail(self, severity: str, reason: str) -> None:
        """Cookie 失效通知。dedup_key=cookie_fail_<severity> 1 小时内仅一次。"""
        if not self._cfg.get("push.notify_cookie_fail", False):
            return
        dedup = f"cookie_fail_{severity}"
        tag = "硬失效（必须重新扫码）" if severity == "HARD_INVALID" else "软失效（可重试）"
        content = (
            f"⚠️ 登录态失效\n"
            f"级别：{tag}\n"
            f"原因：{reason}\n"
            f"请尽快重新扫码登录"
        )
        self.send_async(content, dedup_key=dedup, dedup_window_sec=3600)

    def notify_daily_done(self, today_minutes: int, target_minutes: int) -> None:
        """任务完成发送。dedup_key=daily_done_<date> 当日仅一次。"""
        if not self._cfg.get("push.notify_daily_done", False):
            return
        from datetime import date
        dedup = f"daily_done_{date.today().isoformat()}"
        content = (
            f"✅ 今日阅读目标已完成\n"
            f"📅 日期：{date.today().isoformat()}\n"
            f"📊 今日已读：{format_duration(today_minutes)}\n"
            f"🎯 目标：{format_duration(target_minutes)}"
        )
        self.send_async(content, dedup_key=dedup, dedup_window_sec=86400)

    def notify_login_success(self, cookie_count: int, wr_skey_len: int) -> None:
        """登录成功通知。dedup_key=login_success 1 小时内仅一次。"""
        if not self._cfg.get("push.notify_login_success", False):
            return
        dedup = "login_success"
        content = (
            f"🔐 扫码登录成功\n"
            f"🍪 Cookie 数量：{cookie_count}\n"
            f"🔑 wr_skey 长度：{wr_skey_len}"
        )
        self.send_async(content, dedup_key=dedup, dedup_window_sec=3600)

    def notify_login_qr(self, image_png: bytes, reason: str = "") -> bool:
        """Cookie 失效后推送登录二维码图片（手机长按识别即可重新登录）。

        图片走标准推送通道（AppToken+UID，contentType=2 HTML 内嵌 base64）；
        SPT 极简推送只支持纯文本，无法承载图片。
        未配置图片凭证 / 图片发送失败 / 图片过大时，自动降级 SPT 纯文本提醒。
        由自动扫码会话在拿到二维码时调用（会话节流已保证不会轰炸）。
        """
        if not self._cfg.get("push.notify_login_qr", True):
            return False
        app_token = str(self._cfg.get("push.wxpusher_app_token") or "").strip()
        uid = str(self._cfg.get("push.wxpusher_uid") or "").strip()
        if not app_token or not uid:
            log.info("未配置 AppToken/UID，二维码图片改走纯文本提醒")
            return self._send_qr_fallback_text(reason, image_too_large=False)
        try:
            b64 = base64.b64encode(image_png).decode("ascii")
        except Exception:  # noqa: BLE001
            log.exception("二维码图片 base64 编码失败")
            return self._send_qr_fallback_text(reason, image_too_large=True)
        if len(b64) > self._MAX_HTML_BASE64_LEN:
            log.warning(
                "二维码 PNG 过大（base64=%d 字符），改用纯文本提醒", len(b64)
            )
            return self._send_qr_fallback_text(reason, image_too_large=True)

        reason_line = f"<p>原因：{self._html_escape(reason)}</p>" if reason else ""
        html = (
            '<div style="font-size:16px;line-height:1.6;color:#333;">'
            '<p><b>📲 微信读书登录态已失效</b></p>'
            f'{reason_line}'
            '<p>请在 <b>90 秒内</b>长按下方二维码 → 识别图中二维码，'
            "完成登录后助手将自动恢复运行：</p>"
            '<p style="text-align:center;">'
            '<img alt="登录二维码" '
            'src="data:image/png;base64,' + b64 + '" '
            'style="width:240px;height:240px;border:8px solid #f2f2f2;'
            'border-radius:8px;"/></p>'
            '<p style="color:#999;font-size:13px;">'
            "若图片无法识别，请打开 5911 管理面板手动扫码</p>"
            "</div>"
        )
        ok, msg = self._do_send_html(
            app_token, uid, html, summary="📲 读书助手登录失效，请长按扫码"
        )
        if ok:
            log.info("登录二维码已通过 WxPusher 标准通道推送")
            return True
        log.warning("登录二维码图片推送失败（%s），降级纯文本", msg)
        return self._send_qr_fallback_text(reason, image_too_large=False)

    def send_test_image(self, image_png: bytes | None = None) -> tuple[bool, str]:
        """验证标准图片通道：发一条带图测试消息（供配置页「测试推送」使用）。"""
        app_token = str(self._cfg.get("push.wxpusher_app_token") or "").strip()
        uid = str(self._cfg.get("push.wxpusher_uid") or "").strip()
        if not app_token or not uid:
            return False, "未配置 AppToken/UID（图片通道）"
        if image_png is None:
            # 1x1 透明 PNG（无需 Pillow 等图像依赖）
            image_png = (
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
                b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
                b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00"
                b"\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
            )
        b64 = base64.b64encode(image_png).decode("ascii")
        html = (
            '<div style="font-size:15px;line-height:1.7;">'
            "<p><b>WxReadAssistant 图片通道测试</b></p>"
            '<p>若能看到下方图片（哪怕是一个小点），'
            "说明 Cookie 失效二维码推送可用：</p>"
            '<p><img src="data:image/png;base64,' + b64
            + '" alt="test" style="width:80px;height:80px;background:#f0f0f0;"/></p></div>'
        )
        return self._do_send_html(
            app_token, uid, html, summary="WxRead图片通道测试"
        )

    def notify_login_qr_timeout(self) -> None:
        """自动推送的二维码 90 秒内未被扫描，提醒用户去面板手动操作。"""
        if not self._cfg.get("push.notify_login_qr", True):
            return
        content = (
            "⌛ 登录二维码已过期（90 秒内未扫码）\n"
            "助手稍后会在登录态再次失效时重新推送；"
            "如需立即恢复，请打开 5911 管理面板手动扫码"
        )
        self.send_async(content, dedup_key="qr_timeout", dedup_window_sec=600)

    @staticmethod
    def _html_escape(text: str) -> str:
        return (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    def _send_qr_fallback_text(self, reason: str, *, image_too_large: bool) -> bool:
        """图片推送不可达时的保底：纯文本提醒去面板扫码。"""
        detail = "二维码图片过大" if image_too_large else "图片通道暂不可用"
        content = (
            f"⚠️ 微信读书登录态已失效（{detail}）\n"
            f"原因：{reason or 'Cookie 失效'}\n"
            "请打开 5911 管理面板手动扫码登录，登录后自动恢复运行"
        )
        return self.send_sync(content, max_attempts=2)

    def _do_send_html(
        self, app_token: str, uid: str, html: str, *, summary: str,
        max_attempts: int = 2,
    ) -> tuple[bool, str]:
        """标准推送 POST JSON（AppToken+UID，contentType=2 HTML）。

        返回 (是否受理, 说明)。判定：HTTP 200 且业务 code=1000。
        """
        url = self._std_endpoint
        payload = {
            "appToken": app_token,
            "uids": [uid],
            "content": html,
            "summary": summary[:20],
            "contentType": 2,
        }
        last_msg = "未知错误"
        for i in range(max_attempts):
            try:
                r = requests.post(url, json=payload, timeout=15)
                r.raise_for_status()
                data = r.json()
                if isinstance(data, dict) and data.get("code") == 1000:
                    return True, "ok"
                last_msg = f"code={data.get('code')} msg={data.get('msg')}"
                # 业务错误码（参数问题等）重试无意义
                break
            except requests.RequestException as exc:
                last_msg = str(exc)
            except ValueError:
                # 非 JSON 但 HTTP 成功：保守视为受理
                return True, "non-json"
            if i < max_attempts - 1:
                time.sleep(random.randint(2, 5))
        return False, last_msg

    def notify_risk_control(
        self, kind: str, summary: str, detail: str = ""
    ) -> bool:
        """阅读风控通知（软风控/连续失败）。

        与其他通知不同：限频时间戳持久化在 config.risk_alerts.<kind>，
        程序重启后限频仍然生效，不会重复轰炸（冷却时长取
        risk.alert_cooldown_min）。
        返回 True 表示已实际派发发送。
        """
        if not self._cfg.get("push.notify_risk_control", False):
            return False
        # 未配置 SPT 时直接返回（不占限频名额），否则整个限频窗口内都会静默漏报
        if not str(self._cfg.get("push.wxpusher_spt") or "").strip():
            log.info("未配置 WxPusher SPT，风控告警跳过且不占用限频名额")
            return False
        try:
            window = max(60, int(self._cfg.get("risk.alert_cooldown_min", 180))) * 60
        except (TypeError, ValueError):
            window = 180 * 60

        dedup = f"risk_{kind}"
        cfg_key = f"risk_alerts.{kind}"
        now = time.time()
        try:
            last = float(self._cfg.get(cfg_key, 0) or 0)
        except (TypeError, ValueError):
            last = 0.0
        if now - last < window:
            log.info("风控告警持久化限频命中：%s，跳过发送", dedup)
            return False
        # 先落盘时间戳再发送：发送线程崩溃/程序退出也不会击穿限频
        self._cfg.set(cfg_key, now)

        kind_label = {
            "soft_read": "阅读接口软风控（空响应）",
            "read_fail": "阅读接口连续失败",
        }.get(kind, kind)
        content = (
            f"🚨 微信读书风控提醒\n"
            f"类型：{kind_label}\n"
            f"情况：{summary}"
        )
        if detail:
            content += f"\n详情：{detail}"
        content += (
            "\n处置：已自动停止上报进入冷却，冷却结束后自动重试；"
            "若反复触发，请暂停运行并用官方客户端正常阅读一段时间"
        )
        self.send_async(content, dedup_key=dedup, dedup_window_sec=window)
        return True

    def notify_auto_paused(self, fail_count: int, last_kind: str) -> None:
        """第二级保护：连续失败自动暂停通知。

        dedup_key=auto_paused_<date>_<count>，每次新触发暂停仅推一次；
        手动恢复后再次触发（count 变化）可重新发送。
        """
        if not self._cfg.get("push.notify_auto_pause", True):
            return
        from datetime import date
        dedup = f"auto_paused_{date.today().isoformat()}_{fail_count}"
        content = (
            f"⛔ 阅读已自动暂停\n"
            f"原因：read 接口连续失败 {fail_count} 次（最近类型：{last_kind}）\n"
            f"处置：已停止全部上报且不会自动恢复（跨天也不恢复），"
            f"请打开 5911 面板查看日志排查，确认后手动点击「恢复」按钮"
        )
        self.send_async(content, dedup_key=dedup, dedup_window_sec=86400)

    # ---------------- 内部 ----------------
    def _run_send(
        self,
        content: str,
        max_attempts: int,
        dedup_key: str | None,
        dedup_window_sec: int,
        on_done: Callable[[bool], None] | None,
    ) -> None:
        try:
            if dedup_key:
                with self._lock:
                    now = time.time()
                    last = self._last_fired_key.get(dedup_key, 0.0)
                    if now - last < dedup_window_sec:
                        log.info("WxPusher 去重命中：%s，跳过发送", dedup_key)
                        if on_done:
                            on_done(False)
                        return
                    self._last_fired_key[dedup_key] = now
            ok = self._do_send(content, max_attempts)
        except Exception as exc:  # noqa: BLE001
            log.exception("WxPusher 发送线程异常：%s", exc)
            ok = False
        if on_done:
            try:
                on_done(ok)
            except Exception:  # noqa: BLE001
                log.exception("WxPusher on_done 回调异常")

    def _do_send(self, content: str, max_attempts: int) -> bool:
        spt: str = str(self._cfg.get("push.wxpusher_spt") or "").strip()
        if not spt:
            log.info("未配置 WxPusher SPT，跳过推送")
            return False
        url = self._endpoint.format(spt=spt, content=content)
        last_exc: Exception | None = None
        for i in range(max_attempts):
            try:
                r = requests.get(url, timeout=10)
                r.raise_for_status()
                try:
                    data = r.json()
                    if isinstance(data, dict) and data.get("code") == 1000:
                        log.info("WxPusher 推送成功")
                        return True
                    log.warning("WxPusher 返回异常：%s", data)
                except ValueError:
                    log.info("WxPusher 响应非 JSON，文本=%s", r.text[:200])
                    return True
            except requests.RequestException as exc:
                last_exc = exc
                log.warning(
                    "WxPusher 推送失败（第 %d/%d 次）：%s", i + 1, max_attempts, exc
                )
            if i < max_attempts - 1:
                time.sleep(random.randint(30, 90))
        if last_exc:
            log.error("WxPusher 推送最终失败：%s", last_exc)
        return False
