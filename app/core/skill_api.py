"""Skill 1.0.5 API 调用器。

统一入口：POST https://i.weread.qq.com/api/agent/gateway
Bearer wrk- 鉴权，提供书架/章节/进度/统计查询能力。
不依赖登录 Cookie，仅依赖 Skill API Key。
"""
from __future__ import annotations

import datetime as _dt
import time
from typing import Any

import requests

from app.core.config import ConfigStore
from app.utils.logger import get_logger

log = get_logger(__name__)

SKILL_GATEWAY_URL = "https://i.weread.qq.com/api/agent/gateway"
BEIJING_TZ = _dt.timezone(_dt.timedelta(hours=8))


class SkillAPI:
    """Skill 1.0.5 API 调用器。

    方法返回纯数据结构，持久化交给调用方（scheduler 或 LocalDB）。
    所有方法在调用线程同步执行，UI 层应通过 QThread 包装。
    """

    def __init__(self, config: ConfigStore) -> None:
        self._cfg = config
        # Skill API 不依赖登录 Cookie，使用独立 Session
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
        })

    # ---------- 通用调用 ----------
    def _call(
        self,
        api_name: str,
        params: dict | None = None,
        *,
        timeout: int = 10,
    ) -> dict | None:
        """通用 Skill API 调用。

        Args:
            api_name: API 名称，如 /shelf/sync, /book/chapterinfo
            params: 业务参数（平铺在 body 顶层）
            timeout: 请求超时秒数

        Returns:
            响应数据 dict（成功），None（失败/限流/鉴权失败）
        """
        api_key = str(self._cfg.get("skill.api_key") or "").strip()
        if not api_key:
            log.warning("Skill %s 调用失败：API Key 未配置", api_name)
            return None

        skill_version = str(self._cfg.get("skill.version") or "1.0.5")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "api_name": api_name,
            "skill_version": skill_version,
        }
        if params:
            payload.update(params)

        try:
            masked = {k: str(v)[:50] for k, v in (params or {}).items()}
            log.info("🔄 Skill %s 请求：%s", api_name, masked or "{}")
            t0 = time.time()
            r = self._session.post(
                SKILL_GATEWAY_URL, headers=headers, json=payload, timeout=timeout
            )
            elapsed = (time.time() - t0) * 1000
            log.info(
                "🔄 Skill %s 响应：HTTP=%d time=%.0fms",
                api_name, r.status_code, elapsed,
            )

            if r.status_code == 499:
                log.warning("Skill %s 触发 499 限流", api_name)
                return None
            if r.status_code in (401, 403):
                log.warning("Skill %s 鉴权失败（HTTP %d）", api_name, r.status_code)
                return None

            r.raise_for_status()
            data = r.json()

            if isinstance(data, dict) and data.get("errcode", 0) != 0:
                log.warning(
                    "Skill %s 调用失败：errcode=%s msg=%s",
                    api_name, data.get("errcode"), data.get("errmsg"),
                )
                return None

            if isinstance(data, dict) and "upgrade_info" in data:
                log.warning("⚠️ Skill 有新版可用：%s", data.get("upgrade_info"))

            return data if isinstance(data, dict) else None
        except Exception as exc:  # noqa: BLE001
            log.warning("Skill %s 请求异常：%s", api_name, exc)
            return None

    def _call_readdata(self, mode: str, timeout: int = 10) -> dict:
        """单次 /readdata/detail 调用，含 499 重试 1 次。

        /readdata/detail 直接返回 {readTimes, readDays, readLongest}，
        没有 data 包装层。
        """
        api_key = str(self._cfg.get("skill.api_key") or "").strip()
        if not api_key:
            log.warning("Skill /readdata/detail 调用失败：API Key 未配置")
            return {}

        skill_version = str(self._cfg.get("skill.version") or "1.0.5")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "api_name": "/readdata/detail",
            "skill_version": skill_version,
            "mode": mode,
            "baseTime": 0,
        }
        for attempt in range(2):
            try:
                log.info(
                    "🔄 Skill /readdata/detail 请求：mode=%s attempt=%d",
                    mode, attempt + 1,
                )
                t0 = time.time()
                r = self._session.post(
                    SKILL_GATEWAY_URL, headers=headers, json=payload, timeout=timeout
                )
                elapsed = (time.time() - t0) * 1000
                log.info(
                    "🔄 Skill /readdata/detail 响应：mode=%s HTTP=%d time=%.0fms",
                    mode, r.status_code, elapsed,
                )
                if r.status_code == 499 and attempt == 0:
                    log.warning("Skill %s 触发 499 限流，800ms 后重试 1 次", mode)
                    time.sleep(0.8)
                    continue
                r.raise_for_status()
                data = r.json()
                if isinstance(data, dict) and data.get("errcode", 0) != 0:
                    log.warning(
                        "Skill %s 调用失败：errcode=%s msg=%s",
                        mode, data.get("errcode"), data.get("errmsg"),
                    )
                    return {}
                if isinstance(data, dict) and "upgrade_info" in data:
                    log.warning("⚠️ Skill 有新版：%s", data.get("upgrade_info"))
                if isinstance(data, dict) and "readTimes" in data:
                    log.info(
                        "✅ Skill /readdata/detail mode=%s 返回：readTimes 桶数=%d readDays=%s",
                        mode,
                        len(data.get("readTimes") or {}),
                        data.get("readDays"),
                    )
                return data if isinstance(data, dict) else {}
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Skill %s 请求异常（attempt=%d）：%s", mode, attempt, exc
                )
                if attempt == 0:
                    time.sleep(0.8)
                    continue
                return {}
        return {}

    # ---------- 业务接口 ----------
    def fetch_shelf(self, *, timeout: int = 10) -> list[dict]:
        """获取完整书架 /shelf/sync。"""
        log.info("📚 通过 Skill /shelf/sync 获取书架...")
        data = self._call("/shelf/sync", {}, timeout=timeout)
        if not data:
            return []
        books = data.get("books") or data.get("data") or []
        if not isinstance(books, list):
            return []
        valid = [
            b for b in books
            if isinstance(b, dict) and str(b.get("bookId") or "").strip()
        ]
        log.info("📚 Skill /shelf/sync 返回 %d 本有效书籍", len(valid))
        return valid

    def fetch_chapters(self, book_id: str, *, timeout: int = 10) -> list[dict]:
        """获取章节池 /book/chapterinfo。"""
        log.info("📖 通过 Skill /book/chapterinfo 获取章节池：bookId=%s", book_id)
        data = self._call("/book/chapterinfo", {"bookId": book_id}, timeout=timeout)
        if not data:
            return []
        chapters = data.get("chapters") or data.get("chapterInfos") or []
        if not isinstance(chapters, list):
            return []
        log.info("📖 Skill /book/chapterinfo 返回 %d 个章节", len(chapters))
        return chapters

    def fetch_progress(self, book_id: str, *, timeout: int = 10) -> dict | None:
        """获取阅读进度 /book/getprogress。"""
        log.info("📊 通过 Skill /book/getprogress 获取阅读进度：bookId=%s", book_id)
        data = self._call("/book/getprogress", {"bookId": book_id}, timeout=timeout)
        return data

    def fetch_reading_stats(self, *, timeout: int = 10) -> dict[str, Any]:
        """获取阅读统计 /readdata/detail（3 次：overall/weekly/monthly）。

        返回统一字段：
          today_seconds / week_seconds / month_seconds / total_seconds
          （任一项失败时为 None，表示数据缺失）
        """
        log.info("📊 通过 Skill /readdata/detail 获取阅读统计...")

        now = _dt.datetime.now(BEIJING_TZ)
        today_bj_00 = int(
            now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        )

        overall_data = self._call_readdata("overall", timeout)
        time.sleep(0.7)
        weekly_data = self._call_readdata("weekly", timeout)
        time.sleep(0.7)
        monthly_data = self._call_readdata("monthly", timeout)

        overall_buckets = self._sum_read_times(overall_data)
        weekly_buckets = self._sum_read_times(weekly_data)
        monthly_buckets = self._sum_read_times(monthly_data)

        log.info(
            "Skill 原始数据：overall=%d (桶数=%d) weekly=%d (桶数=%d) monthly=%d (桶数=%d)",
            overall_buckets["total"], overall_buckets["count"],
            weekly_buckets["total"], weekly_buckets["count"],
            monthly_buckets["total"], monthly_buckets["count"],
        )

        today_sec = self._extract_today(weekly_data, today_bj_00)
        if today_sec is None:
            today_sec = self._extract_today(monthly_data, today_bj_00)

        week_sec = weekly_buckets["total"] if weekly_buckets["count"] > 0 else None
        month_sec = monthly_buckets["total"] if monthly_buckets["count"] > 0 else None
        total_sec = overall_buckets["total"] if overall_buckets["count"] > 0 else None

        week_sec = self._validate_range(week_sec, 0, 604800)      # 7d 上限
        month_sec = self._validate_range(month_sec, 0, 2678400)   # 31d 上限

        result = {
            "today_seconds": today_sec,
            "week_seconds": week_sec,
            "month_seconds": month_sec,
            "total_seconds": total_sec,
            "fetched_at": int(time.time()),
        }
        log.info(
            "📊 阅读统计：today=%s week=%s month=%s total=%s",
            self._format_hm(today_sec), self._format_hm(week_sec),
            self._format_hm(month_sec), self._format_hm(total_sec),
        )
        return result

    # ---------- 验证 ----------
    def verify_api_key(self, *, timeout: int = 10) -> tuple[bool, str]:
        """验证 API Key 是否有效。

        Returns:
            (有效, 描述信息)
        """
        api_key = str(self._cfg.get("skill.api_key") or "").strip()
        if not api_key:
            return False, "API Key 未配置"

        try:
            data = self._call("/shelf/sync", {}, timeout=timeout)
            if data is None:
                # _call 已记录具体失败原因（401/403/499/异常）
                return False, "❌ 鉴权失败或连接失败"
            books = data.get("books") or data.get("data") or []
            if isinstance(books, list) and books:
                return True, f"✅ 有效（书架 {len(books)} 本）"
            return False, "❌ 响应无 books 字段"
        except Exception as exc:  # noqa: BLE001
            return False, f"❌ 连接失败：{exc}"

    # ---------- 工具方法 ----------
    @staticmethod
    def _sum_read_times(data: dict) -> dict:
        if not isinstance(data, dict):
            return {"total": 0, "count": 0}
        read_times = data.get("readTimes")
        if not isinstance(read_times, dict):
            return {"total": 0, "count": 0}
        total = 0
        count = 0
        for v in read_times.values():
            iv = int(v) if isinstance(v, (int, float)) else 0
            total += iv
            count += 1
        return {"total": total, "count": count}

    @staticmethod
    def _extract_today(data: dict, today_bj_00: int) -> int | None:
        if not isinstance(data, dict):
            return None
        read_times = data.get("readTimes")
        if not isinstance(read_times, dict):
            return None
        for key in (str(today_bj_00), today_bj_00):
            if key in read_times:
                v = read_times[key]
                if isinstance(v, (int, float)):
                    return int(v)
        return None

    @staticmethod
    def _validate_range(value: int | None, lo: int, hi: int) -> int | None:
        if value is None:
            return None
        if value < lo or value > hi:
            log.warning("Skill 数据合理性校验失败：value=%s 不在 [%s, %s]", value, lo, hi)
            return None
        return value

    @staticmethod
    def _format_hm(seconds: int | None) -> str:
        if seconds is None:
            return "-"
        if seconds < 0:
            return "-"
        h = seconds // 3600
        m = (seconds % 3600) // 60
        if h > 0:
            return f"{h}h{m:02d}m"
        return f"{m}m"
