"""全局配置管理：JSON 持久化，线程安全。

配置文件位置：%APPDATA%/WxReadAssistant/config.json
"""
from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any


def get_app_dir() -> Path:
    base = os.environ.get("APPDATA")
    if not base:
        base = str(Path.home() / ".config")
    path = Path(base) / "WxReadAssistant"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_log_dir() -> Path:
    p = get_app_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


CONFIG_PATH = get_app_dir() / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "cookies": {},          # 登录后从浏览器提取（简化 dict {name: value}）
    "cookies_raw": [],      # 登录后 cookies 的完整列表 [{name,value,domain,...}]
    "headers": {            # 默认请求头
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
        ),
        "referer": "https://weread.qq.com/",
        "origin": "https://weread.qq.com",
        "baggage": (
            "sentry-public_key=9e103f84d02c4b05a318c0a2d72d3e3f,"
            "sentry-trace_id=00000000000000000000000000000000,"
            "sentry-environment=production,"
            "sentry-release=weread-web%401.0.0"
        ),
    },
    "reading": {
        "min_hours": 1.5,              # 每日最少阅读时长（小时）
        "max_hours": 3.0,              # 每日最多阅读时长（小时）
        "min_interval_sec": 30,        # 单页停留下限（秒）
        "max_interval_sec": 45,        # 单页停留上限（秒）
        "chapter_read_min": 4,         # 单章最少阅读次数
        "chapter_read_max": 5,         # 单章最多阅读次数
        "startup_delay_min_sec": 15,   # 启动随机延迟下限
        "startup_delay_max_sec": 25,   # 启动随机延迟上限
        "health_check_first_min": 3,   # 首次健康巡检间隔（分钟）
        "health_check_min": 12,        # 之后每多少分钟一次巡检
        "long_rest_every": 20,         # 每 N 次阅读长休息
        "long_rest_min_sec": 60,       # 长休息下限
        "long_rest_max_sec": 180,      # 长休息上限
        "fail_cooldown_min_sec": 30,   # 失败冷却下限
        "fail_cooldown_max_sec": 60,   # 失败冷却上限
    },
    "risk": {
        # read 端点软风控：HTTP 200 + 空 {}（succ 字段缺失），连续 N 次即判定
        "empty_read_threshold": 3,
        # 第一级：登录态正常但 read 连续失败（任意原因）超过此次数
        # → 告警 + 指数冷却，冷却结束【自动恢复】
        "fail_streak_threshold": 8,
        # 第二级：连续失败达到此次数 → 真正暂停（不自动恢复、跨天不恢复、
        # 重启保持暂停），仅 5911 面板手动恢复；0 = 禁用第二级
        "fail_pause_threshold": 15,
        # 命中风控后自动冷却（停止一切阅读上报）的基准时长/退避上限（分钟）
        "soft_cooldown_min": 60,
        "cooldown_max_min": 360,
        # 同类风控推送的最小间隔（分钟）；时间戳持久化，重启后限频仍生效
        "alert_cooldown_min": 180,
    },
    "skill": {
        "api_key": "",                 # WEREAD_API_KEY (wrk-xxxxxxxx)
        "version": "1.0.5",            # Skill 版本号
        "summary_cache_ttl": 180,      # 阅读统计缓存秒数（默认 3 分钟）
        "refresh_interval_min": 30,    # Skill 数据刷新间隔（分钟）
    },
    "push": {
        "wxpusher_spt": "",
        # 标准推送凭证（AppToken + UID）：SPT 极简推送仅支持纯文本，
        # Cookie 失效推送二维码图片必须使用标准通道（后台 wxpusher.zjiecode.com 创建应用）
        "wxpusher_app_token": "",
        "wxpusher_uid": "",
        "notify_daily_start": True,    # 每日首次开始
        "notify_cookie_fail": True,    # Cookie 失效通知
        "notify_login_qr": True,       # Cookie 失效后自动扫码并把二维码推送到微信
        "auto_qr_cooldown_min": 30,    # 自动扫码推送冷却（分钟），防止登录态抖动时轰炸
        "notify_daily_done": True,     # 任务完成发送
        "notify_login_success": False,  # 登录成功通知
        "notify_risk_control": True,   # 阅读风控通知（软风控/连续失败，自动冷却）
        "notify_auto_pause": True,     # 连续失败达第二级阈值被自动暂停时通知
    },
    "app": {
        "log_retention_days": 7,       # 日志按天轮转保留天数
    },
    # Scheduler 运行态持久化（auto_paused：第二级失败暂停标志，见 scheduler.py）
    "scheduler": {
        "auto_paused": None,           # {reason, fail_count, last_kind, ts} 或 None
    },
    "daily_plan": {
        "date": "",                    # 日期键 YYYY-MM-DD
        "target_minutes": 0,           # 当日随机目标（分钟）
    },
    "risk_alerts": {},                 # 风控告警限频时间戳（{kind: ts}，持久化）
}


class ConfigStore:
    """线程安全的 JSON 配置读写单例。"""

    _instance: "ConfigStore | None" = None
    _init_lock = threading.Lock()

    def __new__(cls, *args: Any, **kwargs: Any) -> "ConfigStore":
        with cls._init_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._loaded = False  # type: ignore[attr-defined]
                cls._instance._lock = threading.RLock()  # type: ignore[attr-defined]
                cls._instance._data = deepcopy(DEFAULT_CONFIG)  # type: ignore[attr-defined]
            return cls._instance

    def __init__(self) -> None:
        if self._loaded:  # type: ignore[has-type]
            return
        self.load()
        self._loaded = True

    # ---------- IO ----------
    def load(self) -> None:
        with self._lock:
            if CONFIG_PATH.exists():
                try:
                    with CONFIG_PATH.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                    self._data = _deep_merge(deepcopy(DEFAULT_CONFIG), data)
                    return
                except (OSError, json.JSONDecodeError):
                    pass
            self._data = deepcopy(DEFAULT_CONFIG)
            self._save_unlocked()

    def save(self) -> None:
        with self._lock:
            self._save_unlocked()

    def _save_unlocked(self) -> None:
        tmp = CONFIG_PATH.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            tmp.replace(CONFIG_PATH)
        except OSError:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            raise

    # ---------- 访问 ----------
    @property
    def data(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._data)

    def get(self, dotted_key: str, default: Any = None) -> Any:
        with self._lock:
            node: Any = self._data
            for part in dotted_key.split("."):
                if isinstance(node, dict) and part in node:
                    node = node[part]
                else:
                    return default
            return deepcopy(node) if isinstance(node, (dict, list)) else node

    def set(self, dotted_key: str, value: Any, *, auto_save: bool = True) -> None:
        with self._lock:
            node = self._data
            parts = dotted_key.split(".")
            for part in parts[:-1]:
                nxt = node.get(part)
                if not isinstance(nxt, dict):
                    nxt = {}
                    node[part] = nxt
                node = nxt
            node[parts[-1]] = value
            if auto_save:
                self._save_unlocked()

    def update_dict(self, dotted_key: str, patch: dict[str, Any], *, auto_save: bool = True) -> None:
        with self._lock:
            node: Any = self._data
            parts = dotted_key.split(".")
            for part in parts:
                nxt = node.get(part)
                if not isinstance(nxt, dict):
                    nxt = {}
                    node[part] = nxt
                node = nxt
            node.update(patch)
            if auto_save:
                self._save_unlocked()

    # ---------- Cookie 便捷方法 ----------
    def get_cookies_dict(self) -> dict[str, str]:
        """返回 {name: value} 格式的 Cookie 字典。"""
        with self._lock:
            cookies = self._data.get("cookies", {})
            return deepcopy(cookies) if isinstance(cookies, dict) else {}

    def get_cookies_raw(self) -> list[dict]:
        """返回 CDP 抓取的完整 Cookie 列表。"""
        with self._lock:
            raw = self._data.get("cookies_raw", [])
            return deepcopy(raw) if isinstance(raw, list) else []

    def clear_cookies(self) -> None:
        """清空所有 Cookie 数据并保存。"""
        with self._lock:
            self._data["cookies"] = {}
            self._data["cookies_raw"] = []
            self._save_unlocked()


def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base
