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
    "cookies": {},          # 登录后从浏览器提取（向后兼容的简化 dict）
    "cookies_raw": [],      # 登录后 cookies 的完整列表 [{name,value,domain,path,secure,httpOnly,sameSite}]
    "headers": {            # 默认请求头（登录后也会从浏览器请求中补充）
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
        ),
        "referer": "https://weread.qq.com/",
        "origin": "https://weread.qq.com",
        # 关键技术点.txt 硬性要求：Sentry Baggage 监控追踪头
        # （和抓包时看到的浏览器一致，否则部分签名噪声源/反作弊指纹不通过）
        # 实际值在 WeReadApi._augment_headers_baggage() 中按采集到的 _qimei_uuid42 / qimei36 动态覆写。
        "baggage": (
            "sentry-public_key=9e103f84d02c4b05a318c0a2d72d3e3f,"
            "sentry-trace_id=00000000000000000000000000000000,"
            "sentry-environment=production,"
            "sentry-release=weread-web%401.0.0"
        ),
    },
    "reading": {
        "min_hours": 1.5,          # 每日最少小时数（风控建议：1.5~3h，避免 >6h 单日高危）
        "max_hours": 3,            # 每日最多小时数（风控建议：>6h 平台开始对超长段打折不计）
        "min_interval_sec": 30,    # 单次 /read 请求后等待下限（风控建议：30~45s，真人平均翻页节奏）
        "max_interval_sec": 45,    # 单次 /read 请求后等待上限
        "daily_random_hour_start": 0,   # 每天随机开始小时（0-23），0=启用就跑/按计划跑
        "health_check_first_min": 3,    # 调度器启动后首次登录态巡检间隔（分钟）
        "health_check_min": 12,          # 之后每多少分钟一次登录态巡检
        # ===== 启动随机延迟（机器指纹伪装）=====
        "startup_delay_min_sec": 30,     # 点击开始后首次阅读前随机等待下限
        "startup_delay_max_sec": 90,     # 点击开始后首次阅读前随机等待上限
        # ===== 风控：书籍/章节多样性 =====
        "switch_book_every_min": 20,     # 阅读多少次后换一本书，0=不换
        "switch_book_every_max": 40,     # 换书区间上限（在 [min,max] 随机换书点）
        "same_chapter_max_reuse": 5,     # 同一章节最多连续使用多少次，超过强制换章
        # ===== CDP 模式：书架跳读配置 =====
        "shelf_books": [],               # CDP 登录获取的用户书架 [{bookId, title, ...}]
        "chapter_pools": {},             # CDP 登录获取的章节池 {book_id: [chapter_uid, ...]}
        "max_shelf_books_for_chapters": 5,  # 章节池构建时取前 N 本书
        "book_switch_recent_exclude": 3,     # 换书时排除最近 N 本
        "chapter_switch_recent_exclude": 5,  # 换章时排除最近 N 章
        # 今日目标当天持久化：当天首次启动随机取值后写入，当天多次启动复用，次日重取
        "daily_plan": {"date": "", "target_minutes": 0},
        # ===== 自动化抓取工作流配置（保留兼容）=====
        "capture_timeout_sec": 15,        # 单次抓取超时秒数
        "workflow_retry_count": 2,       # 抓取失败重试次数
        "santi_book_url": "https://weread.qq.com/web/reader/ce032b305a9bc1ce0b0dd2a",
        "santi_chapter_url": "https://weread.qq.com/web/reader/ce032b305a9bc1ce0b0dd2ak92c3210025c92cc22753209",
    },
    "push": {
        "wxpusher_spt": "",        # WxPusher 极简推送 SPT
        "notify_cookie_fail": True,
        "notify_daily_done": True,
    },
    "app": {
        "auto_start": False,       # 开机自启
        "minimize_to_tray": True,  # 关闭按钮最小化到托盘
        "start_minimized": False,  # 启动直接进托盘
        "start_maximized": True,   # 启动后窗口默认最大化
    },
    "skill": {
        "api_key": "",                # WEREAD_API_KEY (wrk-xxxxxxxx)，申请：https://weread.qq.com/r/weread-skills
        "version": "1.0.5",          # Skill 版本号
        "summary_cache_ttl": 180,    # 阅读统计缓存秒数（默认 3 分钟）
    },
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


def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base
