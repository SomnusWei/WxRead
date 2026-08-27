"""阅读调度器（v2 进度驱动版）。

v2 改造：
- 进度驱动选书：过滤 progress<100%，按进度降序，选第一本
- 顺序选章：从 current_chapter_uid 开始，4~5 次后换章
- 本地实时进度 + Skill 定期覆盖（每 30 分钟）
- 实时通过 Qt 信号通知 UI 更新当前书/进度/章节/已读次数
- 完成度判断：Skill 今日阅读时长 >= 目标分钟数
- 防风控：启动延迟 30~90s + 间隔 30~45s±15% + 每 20 次长休息 60~180s + 失败冷却 30~60s
"""
from __future__ import annotations

import datetime as dt
import random
import threading
import time
from typing import Any

from PySide6.QtCore import QThread, Signal

from app.core.config import ConfigStore
from app.core.local_db import LocalDB
from app.core.notifier import WxPusherNotifier
from app.core.skill_api import SkillAPI
from app.core.weread_api import WeReadApi
from app.utils.logger import get_logger

log = get_logger(__name__)

# 单次阅读成功后本地累加的阅读秒数（默认每次 ≈ 45 秒）
READ_ONCE_SECONDS = 45


class Scheduler(QThread):
    """阅读调度器（进度驱动，实时通知 UI）。

    信号（实时通知 UI 更新）：
      state_changed(str)                  → 运行状态文字
      log_emitted(str)                     → 工作日志（UI 批量追加）
      progress(dict)                       → 任务概览（含 next_run_at/计数/书快照）
      book_progress_updated(str, int, str, int) → (书名, 进度%, 章节标题, 本章已读次数)
      current_book_changed(str, int)       → (书名, 进度%)
      current_chapter_changed(str, int)    → (章节标题, chapter_uid)
      reading_stats_updated(dict)          → 阅读统计字典
      cookie_broken()                      → 登录态彻底失效
      task_completed(int)                  → 今日完成分钟数
      cookie_hard_invalid(str)             → 硬失效原因（UI 弹窗）
    """

    state_changed = Signal(str)
    log_emitted = Signal(str)
    progress = Signal(dict)
    book_progress_updated = Signal(str, int, str, int)
    current_book_changed = Signal(str, int)
    current_chapter_changed = Signal(str, int)
    reading_stats_updated = Signal(dict)
    cookie_broken = Signal()
    task_completed = Signal(int)
    cookie_hard_invalid = Signal(str)

    def __init__(
        self,
        api: WeReadApi,
        skill_api: SkillAPI,
        local_db: LocalDB,
        config: ConfigStore | None = None,
        notifier: WxPusherNotifier | None = None,
    ) -> None:
        super().__init__()
        self._api = api
        self._skill = skill_api
        self._db = local_db
        self._cfg = config or ConfigStore()
        self._notifier = notifier or WxPusherNotifier(self._cfg)

        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # 默认放行

        # 当前阅读状态（线程安全）
        self._state_lock = threading.RLock()
        self._current_book: dict | None = None
        self._current_chapter: dict | None = None
        self._chapter_read_count = 0
        self._success_count = 0
        self._fail_count = 0
        self._current_state = "待机中"
        self._next_run_at = 0.0

        # 健康巡检
        self._health_next_ts = 0.0
        self._health_interval_sec = int(
            self._cfg.get("reading.health_check_min", 12)
        ) * 60
        self._health_fail_count = 0

        # Skill 数据定期刷新（间隔动态从配置读取，不在 __init__ 缓存）
        self._last_skill_refresh_ts = 0.0

        # 接收 WeReadApi 的硬/软失效信号
        self._api.cookie_invalid.connect(self._on_cookie_invalid)

    # ---------------- 控制 ----------------
    def stop(self) -> None:
        self._stop_event.set()
        self._pause_event.set()
        self.wait(3000)

    def pause(self) -> None:
        self._pause_event.clear()
        self._set_state("已暂停")

    def resume(self) -> None:
        self._pause_event.set()
        self._set_state("运行中")

    def _set_state(self, text: str) -> None:
        with self._state_lock:
            self._current_state = text
        self.state_changed.emit(text)

    def _emit_log(self, msg: str) -> None:
        log.info(msg)
        self.log_emitted.emit(msg)

    # ---------------- 主循环 ----------------
    def run(self) -> None:  # noqa: D401
        # --- 同实例复用的「再启动」防御：
        # UI 层 stop() 后直接在同一 Scheduler 对象上再次 start()，
        # QThread.finished 后允许重入 run()，但 threading.Event 的
        # set/clear 状态跨 run() 保留，必须手动复位到"放行/未停止"
        # 的初始约定，否则倒计时 / 主循环守卫会立即 return。
        self._stop_event.clear()
        if not self._pause_event.is_set():
            self._pause_event.set()   # 默认放行（运行态）
        self._last_skill_refresh_ts = 0.0
        self._success_count = 0
        self._fail_count = 0
        self._chapter_read_count = 0
        self._next_run_at = 0.0

        log.info("调度器已启动")
        self._set_state("初始化中")

        # 健康巡检：启动后 3 分钟做第一次
        with self._state_lock:
            self._health_next_ts = time.time() + max(
                60.0, float(self._cfg.get("reading.health_check_first_min", 3)) * 60
            )
            self._current_book = None
            self._current_chapter = None

        # 启动即检查登录态
        if not self._api.check_session():
            self._emit_log("启动检测：当前登录态无效，正在尝试刷新...")
            if not self._api.ensure_session():
                self._set_state("登录态失效，等待扫码")
                self._notify_cookie_fail("HARD_INVALID", "启动时登录态无效")
                self.cookie_broken.emit()
                return

        # ===== 启动随机延迟（反机器指纹）· 每秒刷倒计时 =====
        reading_cfg = self._cfg.get("reading", {}) or {}
        try:
            s_min = float(reading_cfg.get("startup_delay_min_sec", 30))
            s_max = float(reading_cfg.get("startup_delay_max_sec", 90))
            if s_max < s_min:
                s_max = s_min
            startup_delay = random.uniform(s_min, s_max)
        except (ValueError, TypeError):
            startup_delay = random.uniform(30, 90)
        self._emit_log(
            f"⏱️ 启动随机延迟 {startup_delay:.0f}s（{s_min:.0f}~{s_max:.0f}s），"
            "模拟真人进入阅读前的准备"
        )
        # 暂停语义（全文件统一）：
        #   _pause_event.set()   → 放行 / 运行中
        #   _pause_event.clear() → 阻塞 / 暂停中
        # 倒计时阶段：set() 时推进倒计时；clear() 时挂起冻结，等待 resume() 恢复
        remaining = float(startup_delay)
        last_emitted_tick = -1
        while remaining > 0:
            if self._stop_event.is_set():
                return
            if self._pause_event.is_set():
                # ← 运行态：正常扣减倒计时
                tick = int(remaining + 0.5)
                if tick != last_emitted_tick:
                    self._set_state(f"启动准备中（{tick}s）")
                    last_emitted_tick = tick
                chunk = min(0.25, remaining)
                if chunk <= 0:
                    break
                if not self._interruptible_sleep(chunk):
                    return
                remaining -= chunk
            else:
                # ← 暂停态：不扣倒计时，等待用户恢复
                self._set_state("已暂停（启动倒计时冻结）")
                self._pause_event.wait(timeout=0.5)
                if self._stop_event.is_set():
                    return

        # 主循环
        while not self._stop_event.is_set():
            # ⚠️ 暂停语义：本项目 _pause_event 与"常见约定"相反——
            #    set()   = 内部标记"放行 / 运行中"
            #    clear() = 内部标记"阻塞 / 暂停中"
            # 这样写的历史原因：pause()/resume()/倒计时/_interruptible_sleep
            # 四处都按同一反语义工作，主循环这里必须保持一致。
            if not self._pause_event.is_set():
                self._set_state("已暂停")
                # wait() 会阻塞到被外部 set()（即 resume()）
                self._pause_event.wait()
                if self._stop_event.is_set():
                    return
                self._set_state("运行中")

            # ① 健康检查（每 12 分钟）
            self._maybe_run_health_check()
            if self._stop_event.is_set():
                return

            # ② 完成度检查（Skill 今日时长 >= 目标）
            target_min = self._ensure_daily_plan()
            today_sec = self._db.get_today_seconds()
            today_min = today_sec // 60
            if today_min >= target_min:
                self._set_state("今日任务已完成")
                self._emit_log(
                    f"🎯 今日任务已完成！已读 {today_min}/{target_min} 分钟"
                )
                self._notifier.notify_daily_done(today_min, target_min)
                self.task_completed.emit(today_min)
                # 等待下一天或用户停止
                if not self._sleep_till_next_minute():
                    return
                continue

            # ③ 检查书架是否全部 100%
            if self._is_shelf_all_done():
                self._set_state("书架已全部读完")
                self._emit_log("📚 书架中所有书籍已读完（progress=100%）")
                self.task_completed.emit(today_min)
                if not self._sleep_till_next_minute():
                    return
                continue

            # ④ 定期刷新 Skill 数据（每 30 分钟）
            self._maybe_refresh_skill_data()
            if self._stop_event.is_set():
                return

            # ⑤ 选书选章
            if not self._select_book_and_chapter():
                self._set_state("无可读书籍")
                self._emit_log("⚠️ 无可读书籍（书架为空或无章节池）")
                if not self._sleep_till_next_minute():
                    return
                continue

            # ⑥ 执行单次阅读上报
            self._set_state("阅读中")
            ok = self._do_one_reading_step()

            # ⑦ 章节切换检查（4~5 次后换章）
            if ok:
                self._check_chapter_switch()
                # 100% 换书
                self._check_book_switch()

            # ⑧ 计算下次间隔
            cur_interval = self._calc_next_interval(ok)
            self._next_run_at = time.time() + cur_interval
            self._emit_progress(cur_interval, ok)
            if not self._interruptible_sleep(cur_interval):
                return

        self._set_state("已停止")
        log.info("调度器已停止")

    # ---------------- 选书选章 ----------------
    # 封面/版权页等非正文标题黑名单
    _SKIP_TITLES = {"封面", "版权信息", "编辑说明", "序", "序言", "前言",
                    "目录", "书名页", "扉页", "版权页", "内容简介", "作者简介"}

    @classmethod
    def _is_skip_chapter(cls, chapter: dict) -> bool:
        """判断是否为非正文章节（封面/版权页等），上报这些会被服务器拒绝。"""
        if not isinstance(chapter, dict):
            return False
        title = str(chapter.get("title", "")).strip()
        if title in cls._SKIP_TITLES:
            return True
        # 字数过少（< 50）大概率是扉页/版权页
        try:
            wc = int(chapter.get("wordCount", 0) or 0)
        except (TypeError, ValueError):
            wc = 0
        if 0 < wc < 50:
            return True
        return False

    def _first_content_chapter(self, book_id: str) -> dict | None:
        """返回第一个正文章节（跳过封面/版权页）。"""
        chapters = self._db.get_chapters(book_id)
        for ch in chapters:
            if not self._is_skip_chapter(ch):
                return ch
        # 全部都是非正文 → 返回第一章兜底
        return chapters[0] if chapters else None

    def _select_book_and_chapter(self) -> bool:
        """进度驱动选书 + 顺序选章。"""
        with self._state_lock:
            # 已有当前书且进度 < 100% → 继续读
            if (
                self._current_book
                and self._safe_progress(self._current_book) < 100
                and self._current_chapter
            ):
                return True

            # 选下一本：进度最高的未读完的书
            book = self._db.select_next_book()
            if not book:
                self._current_book = None
                self._current_chapter = None
                return False

            self._current_book = book
            book_id = str(book.get("bookId"))
            title = str(book.get("title", "未知"))
            progress = self._safe_progress(book)
            self._chapter_read_count = 0
            self._emit_log(f"📖 切换书籍：{title}（进度 {progress}%）")
            self.current_book_changed.emit(title, progress)

            # 选到新书后实时拉一次 Skill 进度覆盖本地（避免旧进度不准）
            new_progress = self._refresh_book_progress(book_id)
            if new_progress is not None:
                progress = new_progress
                self.current_book_changed.emit(title, progress)

            # 选起始章节：从 current_chapter_uid 开始
            current_uid = book.get("current_chapter_uid")
            chapter = None
            if current_uid is not None:
                chapter = self._db.get_chapter_by_uid(book_id, current_uid)
            if not chapter:
                # 回退到第一个正文章节（跳过封面/版权页）
                chapter = self._first_content_chapter(book_id)
            if not chapter:
                # 无章节池 → 尝试拉取
                self._emit_log(f"⚠️ {title} 无章节池，尝试用 Skill 拉取...")
                chapters = self._skill.fetch_chapters(book_id)
                if chapters:
                    self._db.update_chapters(book_id, title, chapters)
                    chapter = self._first_content_chapter(book_id)
            if not chapter:
                self._emit_log(f"❌ {title} 无可用章节，跳过此书")
                # 标记为已读避免循环
                self._db.update_book_progress(book_id, 100)
                self._current_book = None
                return False

            self._current_chapter = chapter
            ch_title = str(chapter.get("title", "未知"))
            ch_uid = chapter.get("chapterUid", 0)
            self._emit_log(f"📖 起始章节：{ch_title}（uid={ch_uid}）")
            self.current_chapter_changed.emit(ch_title, ch_uid)
            return True

    def _check_chapter_switch(self) -> None:
        """检查是否需要换章（4~5 次后）。"""
        reading_cfg = self._cfg.get("reading", {}) or {}
        try:
            ch_max = int(reading_cfg.get("chapter_read_max", 5))
        except (ValueError, TypeError):
            ch_max = 5
        with self._state_lock:
            if self._chapter_read_count < ch_max:
                return
            if not self._current_book or not self._current_chapter:
                return
            book = self._current_book
            cur_chapter = self._current_chapter

        book_id = str(book.get("bookId"))
        cur_uid = cur_chapter.get("chapterUid")
        next_chapter = self._db.get_next_chapter(book_id, cur_uid)
        # 跳过封面/版权页等非正文章节
        while next_chapter and self._is_skip_chapter(next_chapter):
            next_chapter = self._db.get_next_chapter(book_id, next_chapter.get("chapterUid"))
        if not next_chapter:
            # 已到最后一章 → 标记进度 100% 触发换书
            self._emit_log(
                f"📖 {book.get('title', '?')} 已读完最后一章，标记 100%"
            )
            self._db.update_book_progress(book_id, 100)
            return

        with self._state_lock:
            self._current_chapter = next_chapter
            self._chapter_read_count = 0
        ch_title = str(next_chapter.get("title", "未知"))
        ch_uid = next_chapter.get("chapterUid", 0)
        self._emit_log(f"📖 切换章节：{ch_title}（uid={ch_uid}）")
        self.current_chapter_changed.emit(ch_title, ch_uid)
        # 更新 LocalDB 当前章节
        self._db.update_book_progress(
            book_id,
            self._safe_progress(book),
            chapter_uid=ch_uid,
            chapter_title=ch_title,
        )

    def _check_book_switch(self) -> None:
        """检查是否需要换书（进度 100%）。"""
        with self._state_lock:
            if not self._current_book:
                return
            book = self._current_book
        book_id = str(book.get("bookId"))
        progress = self._safe_progress(book)
        if progress < 100:
            return
        self._emit_log(
            f"📖 {book.get('title', '?')} 进度已达 100%，准备换书"
        )
        # 清空当前书，下一轮 _select_book_and_chapter 会选下一本
        with self._state_lock:
            self._current_book = None
            self._current_chapter = None
            self._chapter_read_count = 0

    # ---------------- 单次阅读上报 ----------------
    def _do_one_reading_step(self) -> bool:
        """执行一次 read_once 上报。"""
        with self._state_lock:
            book = self._current_book
            chapter = self._current_chapter
        if not book or not chapter:
            self._emit_log("⚠️ 无当前书/章节，跳过本次阅读")
            return False

        book_id = str(book.get("bookId"))
        chapter_uid = str(chapter.get("chapterUid", ""))
        title = str(book.get("title", "未知"))
        ch_title = str(chapter.get("title", "未知"))

        ok = self._api.read_once(book_id, chapter_uid)
        with self._state_lock:
            if ok:
                self._success_count += 1
                self._chapter_read_count += 1
                # 本地累加进度（按章节比例微调）
                self._increment_local_progress(book_id, title, ch_title)
                # 本地累加今日阅读秒数
                today_sec = self._db.add_today_seconds(READ_ONCE_SECONDS)
                self._emit_log(
                    f"✅ 阅读成功：{title} {ch_title}（第 {self._chapter_read_count} 次，"
                    f"今日 {today_sec // 60} 分钟）"
                )
            else:
                self._fail_count += 1
                self._emit_log(f"❌ 阅读失败：{title} {ch_title}")
        return ok

    def _increment_local_progress(
        self, book_id: str, title: str, ch_title: str
    ) -> None:
        """本地累加进度（按章节比例微调）+ 实时通知 UI。"""
        chapters = self._db.get_chapters(book_id)
        chapter_count = max(1, len(chapters))
        # 每章在全书中的进度比例
        delta = 100.0 / chapter_count
        new_progress = self._db.increment_book_progress(book_id, delta)
        with self._state_lock:
            if self._current_book:
                self._current_book["progress"] = new_progress
        # 实时发射 UI 更新信号
        self.book_progress_updated.emit(
            title, new_progress, ch_title, self._chapter_read_count
        )

    # ---------------- 健康检查 ----------------
    def _maybe_run_health_check(self) -> None:
        now = time.time()
        if self._health_next_ts <= 0.0:
            self._health_next_ts = now + max(
                180.0,
                float(self._cfg.get("reading.health_check_first_min", 3)) * 60,
            )
        if now < self._health_next_ts:
            return
        self._health_next_ts = now + max(180.0, float(self._health_interval_sec))

        self._emit_log("🔍 登录态巡检中...")
        ok = self._api.check_session()
        if ok:
            with self._state_lock:
                self._health_fail_count = 0
            self._emit_log("✅ 登录态巡检通过")
            return
        self._emit_log("⚠️ 巡检发现登录态异常，正在刷新 Cookie...")
        refreshed = self._api.ensure_session()
        with self._state_lock:
            if refreshed:
                self._health_fail_count = 0
                self._emit_log("✅ 巡检刷新 Cookie 成功")
                return
            self._health_fail_count += 1
            self._health_next_ts = now + 5 * 60
            fails = self._health_fail_count
        self._emit_log(f"❌ 巡检刷新失败（第 {fails} 次）")
        if fails >= 2:
            self._set_state("登录态失效，等待扫码")
            self._notify_cookie_fail("SOFT_FAIL", "连续 2 次巡检刷新失败")
            self.cookie_broken.emit()

    # ---------------- Skill 数据定期刷新 ----------------
    def _refresh_book_progress(self, book_id: str) -> int | None:
        """实时拉一次 Skill 进度覆盖本地，返回新进度或 None。"""
        try:
            progress_data = self._skill.fetch_progress(book_id)
            if not progress_data:
                return None
            percent = progress_data.get("percent")
            if percent is None:
                return None
            try:
                new_progress = int(float(percent))
            except (TypeError, ValueError):
                return None
            self._db.update_book_progress(book_id, new_progress)
            with self._state_lock:
                if self._current_book:
                    self._current_book["progress"] = new_progress
            self._emit_log(f"📊 Skill 进度刷新：{new_progress}%")
            return new_progress
        except Exception as exc:  # noqa: BLE001
            self._emit_log(f"⚠️ Skill 进度刷新失败：{exc}")
            return None

    def _maybe_refresh_skill_data(self) -> None:
        now = time.time()
        # 动态读取配置，设置页改了立即生效（无需重启调度器）
        try:
            interval_sec = int(self._cfg.get("skill.refresh_interval_min", 30)) * 60
        except (TypeError, ValueError):
            interval_sec = 30 * 60
        if now - self._last_skill_refresh_ts < interval_sec:
            return
        self._last_skill_refresh_ts = now
        self._emit_log("📊 刷新 Skill 数据（统计 + 进度）...")
        try:
            stats = self._skill.fetch_reading_stats()
            if stats:
                # 适配 LocalDB 字段名
                local_stats = {
                    "today_seconds": stats.get("today_seconds") or 0,
                    "weekly_seconds": stats.get("week_seconds") or 0,
                    "monthly_seconds": stats.get("month_seconds") or 0,
                    "total_seconds": stats.get("total_seconds") or 0,
                }
                self._db.update_reading_stats(local_stats)
                self.reading_stats_updated.emit(local_stats)
                self._emit_log(
                    f"📊 Skill 统计刷新：今日 {local_stats['today_seconds'] // 60} 分钟"
                )
        except Exception as exc:  # noqa: BLE001
            self._emit_log(f"⚠️ Skill 统计刷新失败：{exc}")

        # 刷新当前书进度（复用 _refresh_book_progress）
        with self._state_lock:
            book = self._current_book
        if book:
            book_id = str(book.get("bookId"))
            self._refresh_book_progress(book_id)

    # ---------------- 完成度 / 书架检查 ----------------
    def _ensure_daily_plan(self) -> int:
        """获取或生成今日目标分钟数。"""
        from datetime import date

        today = date.today().isoformat()
        daily_plan = self._cfg.get("daily_plan", {}) or {}
        persisted_date = str(daily_plan.get("date") or "")
        persisted_target = int(daily_plan.get("target_minutes") or 0)
        if persisted_date == today and persisted_target > 0:
            return persisted_target

        reading_cfg = self._cfg.get("reading", {}) or {}
        try:
            mn_h = float(reading_cfg.get("min_hours", 1.5))
            mx_h = float(reading_cfg.get("max_hours", 3.0))
        except (ValueError, TypeError):
            mn_h, mx_h = 1.5, 3.0
        mn_min = int(mn_h * 60)
        mx_min = int(mx_h * 60)
        if mx_min < mn_min:
            mx_min = mn_min
        target = random.randint(mn_min, mx_min)
        self._cfg.set("daily_plan", {"date": today, "target_minutes": target})
        self._emit_log(f"🎯 今日目标：{target} 分钟（{mn_h}~{mx_h} 小时随机）")
        self._notifier.notify_daily_start(target)
        return target

    def _is_shelf_all_done(self) -> bool:
        unread = self._db.get_unread_books()
        return not unread

    # ---------------- 间隔计算 ----------------
    def _calc_next_interval(self, ok: bool) -> float:
        reading_cfg = self._cfg.get("reading", {}) or {}
        try:
            mn = max(5, int(reading_cfg.get("min_interval_sec", 30)))
            mx = max(mn, int(reading_cfg.get("max_interval_sec", 45)))
        except (ValueError, TypeError):
            mn, mx = 30, 45
        base = random.uniform(mn, mx)
        jitter = random.uniform(0.85, 1.15)
        cur = base * jitter
        # 每 N 次长休息（频率从配置动态读取）
        with self._state_lock:
            success = self._success_count
        try:
            rest_every = int(reading_cfg.get("long_rest_every", 20))
        except (ValueError, TypeError):
            rest_every = 20
        if rest_every > 0 and success > 0 and success % rest_every == 0:
            try:
                rest_min = int(reading_cfg.get("long_rest_min_sec", 60))
                rest_max = int(reading_cfg.get("long_rest_max_sec", 180))
            except (ValueError, TypeError):
                rest_min, rest_max = 60, 180
            extra = random.uniform(rest_min, rest_max)
            cur += extra
            self._emit_log(f"☕ 每 {rest_every} 次长休息：+{extra:.0f}s")
        if not ok:
            try:
                cd_min = int(reading_cfg.get("fail_cooldown_min_sec", 30))
                cd_max = int(reading_cfg.get("fail_cooldown_max_sec", 60))
            except (ValueError, TypeError):
                cd_min, cd_max = 30, 60
            extra = random.uniform(cd_min, cd_max)
            cur += extra
            self._emit_log(f"⚠️ 失败冷却：+{extra:.0f}s")
        return cur

    def _emit_progress(self, cur_interval: float, ok: bool) -> None:
        with self._state_lock:
            book = dict(self._current_book) if self._current_book else None
            chapter = (
                dict(self._current_chapter) if self._current_chapter else None
            )
            success = self._success_count
            fail = self._fail_count
            state = self._current_state
        self.progress.emit({
            "target_minutes": self._cfg.get("daily_plan.target_minutes", 0),
            "today_seconds": self._db.get_today_seconds(),
            "success": success,
            "fail": fail,
            "current_interval": round(cur_interval, 1),
            "next_run_at": round(self._next_run_at, 3),
            "state": state,
            "book": book,
            "chapter": chapter,
            "chapter_read_count": self._chapter_read_count,
            "ok": ok,
        })

    # ---------------- 工具 ----------------
    @staticmethod
    def _safe_progress(book: dict) -> int:
        try:
            return int(book.get("progress", 0))
        except (TypeError, ValueError):
            return 0

    def _interruptible_sleep(self, seconds: float) -> bool:
        """可被 stop/pause 打断的 sleep。返回 False 表示被 stop 打断。

        暂停语义（全文件统一）：
          _pause_event.set()   → 放行 / 运行中 → 正常 sleep
          _pause_event.clear() → 阻塞 / 暂停中 → 挂起 wait 等待恢复
        """
        end = time.time() + seconds
        while time.time() < end:
            if self._stop_event.is_set():
                return False
            if not self._pause_event.is_set():
                # ← 暂停态（clear）：阻塞等待外部 resume() 调用 set() 恢复
                self._pause_event.wait(timeout=0.5)
                if self._stop_event.is_set():
                    return False
                continue
            chunk = min(0.2, end - time.time())
            if chunk <= 0:
                break
            time.sleep(chunk)
        return not self._stop_event.is_set()

    def _sleep_till_next_minute(self) -> bool:
        """睡到下一分钟边界（用于完成/书架全 100% 后等待）。"""
        now = dt.datetime.now()
        next_min = (now + dt.timedelta(minutes=1)).replace(
            second=0, microsecond=0
        )
        wait_sec = (next_min - now).total_seconds() + 1
        return self._interruptible_sleep(wait_sec)

    # ---------------- 信号处理 ----------------
    def _on_cookie_invalid(self, severity: str, reason: str) -> None:
        log.warning("收到 Cookie 失效信号：severity=%s reason=%s", severity, reason)
        self._notify_cookie_fail(severity, reason)
        if severity == "HARD_INVALID":
            self._set_state("登录态硬失效，等待扫码")
            self.cookie_hard_invalid.emit(reason)
            self.cookie_broken.emit()
            # 设置 stop 让主循环退出
            self._stop_event.set()

    def _notify_cookie_fail(self, severity: str, reason: str) -> None:
        self._notifier.notify_cookie_fail(severity, reason)
