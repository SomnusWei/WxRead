"""WxPusher 极简推送封装：失败带退避重试（独立线程执行，避免阻塞 UI）。"""
from __future__ import annotations

import random
import threading
import time
from typing import Callable

import requests

from .config import ConfigStore
from app.utils.logger import get_logger

log = get_logger(__name__)


class WxPusherNotifier:
    _endpoint = "https://wxpusher.zjiecode.com/api/send/message/{spt}/{content}"

    def __init__(self, config: ConfigStore | None = None) -> None:
        self._cfg = config or ConfigStore()
        self._lock = threading.Lock()
        self._last_fired_key: dict[str, float] = {}  # 去重节流

    # ---------------- public ----------------
    def send_async(
        self,
        content: str,
        *,
        max_attempts: int = 3,
        dedup_key: str | None = None,
        dedup_window_sec: int = 3600,
        on_done: Callable[[bool], None] | None = None,
    ) -> None:
        """异步发送：返回立即；发送完成通过 on_done 回调（可能在任意线程）。"""
        threading.Thread(
            target=self._run_send,
            args=(content, max_attempts, dedup_key, dedup_window_sec, on_done),
            name="WxPusher-Send",
            daemon=True,
        ).start()

    def send_sync(self, content: str, *, max_attempts: int = 3) -> bool:
        return self._do_send(content, max_attempts)

    # ---------------- internal ----------------
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
        spt: str = (self._cfg.get("push.wxpusher_spt") or "").strip()
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
                log.warning("WxPusher 推送失败（第 %d/%d 次）：%s", i + 1, max_attempts, exc)
            if i < max_attempts - 1:
                time.sleep(random.randint(30, 90))
        if last_exc:
            log.error("WxPusher 推送最终失败：%s", last_exc)
        return False
