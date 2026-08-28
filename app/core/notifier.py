"""WxPusher 推送封装（v2 多规则版）。

规则开关（来自 config.push）：
  - notify_daily_start    每日首次开始（包含今日目标）
  - notify_cookie_fail    Cookie 失效通知
  - notify_daily_done     任务完成发送
  - notify_login_success  登录成功通知

所有推送异步执行（独立线程），失败带退避重试，独立 dedup_key 节流。
"""
from __future__ import annotations

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
