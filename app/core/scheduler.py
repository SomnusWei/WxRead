"""阅读调度器：每天按随机时长随机间隔完成阅读计划。

设计要点：
1. 启动时确定今天目标：在 [min_hours, max_hours] 之间随机，分钟粒度（例如 9h47m）。
2. 每次请求间隔在 [min_interval_sec, max_interval_sec] 之间随机（含 ±15% 抖动）。
3. 每 ~20 次请求穿插一次稍长的"休息"（60-180 秒），模拟真实阅读疲劳。
4. 每 ~10 分钟切换一次"书籍/章节"由 weread_api 内部随机，这里只控制节奏。
5. 使用 QThread + 事件信号驱动 UI 状态同步。
"""
from __future__ import annotations

import datetime as dt
import random
import threading
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QThread, Signal

from .config import ConfigStore
from .weread_api import WeReadApi
from .notifier import WxPusherNotifier
from app.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class DailyPlan:
    date: str                   # YYYY-MM-DD
    target_minutes: int         # 今日目标总分钟数
    started_minutes: int = 0    # 已完成分钟数（本地估算值，Skill 为权威）
    skill_baseline_sec: int = 0 # Skill 网关返回的今日阅读秒数基线
    session_accum_sec: int = 0  # 本次启动后本地累加的阅读秒数
    success_count: int = 0
    fail_count: int = 0

    @property
    def completed_sec(self) -> int:
        """已完成秒数 = max(Skill 基线 + 本地累加, 本地累计)。"""
        skill_est = self.skill_baseline_sec + self.session_accum_sec
        return max(skill_est, self.started_minutes * 60)

    @property
    def completed_minutes(self) -> int:
        """已完成分钟数（向上取整）。"""
        return (self.completed_sec + 59) // 60

    @property
    def remaining_minutes(self) -> int:
        return max(0, self.target_minutes - self.completed_minutes)


class ReadingScheduler(QThread):
    """后台阅读调度线程。

    信号：
      state_changed(str)          -> 文案状态
      progress(dict)              -> 今日任务概览
      log(str)                    -> 普通日志
      cookie_broken()             -> 登录态彻底失效
      task_completed(int)         -> 今日完成的分钟数
    """

    state_changed = Signal(str)
    # progress 里新增：
    #   next_run_at(float)   = time.time() 级别的单例时间戳（UI 用它做 1s 倒计时）
    #   current_interval     = 本次计划间隔秒数（用于标签 & 兜底）
    #   state                = 运行状态文字，让 UI 不需要来回比对
    progress = Signal(dict)
    log = Signal(str)
    cookie_broken = Signal()
    task_completed = Signal(int)  # 实际完成分钟数
    cookie_fail_reported = Signal()  # 登录态失效已推送（UI 同步红色"已失效"徽标）

    def __init__(
        self,
        api: WeReadApi,
        config: ConfigStore | None = None,
        notifier: WxPusherNotifier | None = None,
    ) -> None:
        super().__init__()
        self._api = api
        self._cfg = config or ConfigStore()
        self._notifier = notifier or WxPusherNotifier(self._cfg)
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # 默认"放行"
        self._cookie_fail_notified = False
        self._daily_lock = threading.Lock()
        self._daily: DailyPlan | None = None
        # —— 登录态健康巡检（阅读过程中定期检测，不再只靠失败时被动发现）
        self._health_lock = threading.Lock()
        self._health_next_ts = 0.0  # 下次要跑 check_session 的时间戳
        self._health_interval_sec = int((self._cfg.get("reading.health_check_min") or 12)) * 60  # 默认 12 分钟
        self._health_fail_count = 0
        # —— 倒计时共享状态（UI 拿到 next_run_at 做 1s tick）
        self._state_lock = threading.Lock()
        self._current_state = "待机中"
        # —— 连续 2 次 HTTP 200 空 body 检测：key=book_id，记录连续次数；≥2 时自动清缓存+重拉章节
        self._consec_empty_body: dict[str, int] = {}
        # —— Skill 网关基线相关：混合方案完成度检测
        self._skill_refresh_interval_sec = 300  # 每 5 分钟刷新一次 Skill 基线
        self._last_skill_refresh_ts = 0.0
        self._skill_success_refresh_count = 10  # 每 10 次成功阅读也会尝试刷新
        self._skill_fallback_used = False  # Skill 失败时是否已降级为本地估算

    # ---------------- controls ----------------
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

    # ---------------- main loop ----------------
    def run(self) -> None:  # noqa: D401
        log.info("调度器已启动")
        self._set_state("初始化中")
        # 初始化健康巡检：启动后 3 分钟做第一次（避免启动高峰就跑一遍）
        with self._health_lock:
            self._health_next_ts = time.time() + max(60.0, float(self._cfg.get("reading.health_check_first_min") or 3) * 60)
        # —— 启动即尝试：从书架拿一本"进度最高未读完"的书做默认当前书（状态页立刻显示书名/URL/进度）
        try:
            self._api.refresh_current_book_from_shelf()
        except Exception as exc:  # noqa: BLE001
            log.debug("启动时刷新当前书失败（正常：无登录态/书架空就跳过）：%s", exc)
        # 启动先快速验证一次会话是否可用
        if not self._api.check_session():
            self.log.emit("启动检测：当前登录态无效，正在尝试刷新...")
            if not self._api.ensure_session():
                self._set_state("登录态失效，等待扫码")
                self._notify_cookie_fail()
        # 主循环：跨日期自动滚动到下一天计划
        while not self._stop_event.is_set():
            # —— 每次回到循环入口都尝试一次"登录态健康巡检"（到期才会真的跑）
            self._maybe_run_health_check()
            plan = self._ensure_daily_plan()
            # —— 检查完成度：使用 Skill 混合方案 ——
            if plan.completed_minutes >= plan.target_minutes:
                self._set_state("今日任务已完成，空闲中")
                self.log.emit(
                    f"🎯 今日任务已完成！已读 {plan.completed_minutes}/{plan.target_minutes} 分钟"
                    f"（Skill 基线 {plan.skill_baseline_sec}s + 本次累加 {plan.session_accum_sec}s）"
                )
                self._notify_daily_done(plan.completed_minutes)
                if not self._sleep_till_next_minute(plan.date):
                    return
                continue
            # —— 周期性刷新 Skill 基线 ——
            self._maybe_refresh_skill_baseline(plan)
            self._set_state("阅读中")
            # 执行一次
            ok = self._do_one_reading_step(plan)
            # 进度上报
            cfg = self._cfg.get("reading", {}) or {}
            mn = max(5, int(cfg.get("min_interval_sec", 25)))
            mx = max(mn, int(cfg.get("max_interval_sec", 45)))
            cur_interval = random.uniform(mn, mx)
            # 每完成 ~20 次加一段较长休息（模拟疲劳停顿）
            if plan.success_count > 0 and plan.success_count % 20 == 0:
                cur_interval += random.uniform(60, 180)
            # ±15% 抖动
            cur_interval = cur_interval * random.uniform(0.85, 1.15)
            if not ok:
                # 失败时"加 30~60s 额外冷却"，而不是硬下限 60s
                #   否则失败后永远是 60s，用户看起来就是"固定 60 秒一次"
                extra = random.uniform(30, 60)
                cur_interval += extra
                self.log.emit(
                    "⚠️ 本次阅读失败，额外冷却 {}s（下一轮 ≈ {}s 后）".format(
                        round(extra, 0), round(cur_interval, 1),
                    )
                )

            # 用绝对时间戳驱动 UI 倒计时（UI 只需要 tick 1s + max(0,next_run_at-now)）
            next_run_at = time.time() + float(cur_interval)
            with self._state_lock:
                state_text = self._current_state
            # —— 带"当前书"快照，让状态页不必跨线程访问 API._book_lock
            try:
                book_snap = self._api.current_book()
            except Exception:  # noqa: BLE001
                book_snap = None
            last_report_ts = int(getattr(self._api, "_last_read_ts", 0) or 0)

            self.progress.emit(
                {
                    "date": plan.date,
                    "target_minutes": plan.target_minutes,
                    "started_minutes": plan.started_minutes,
                    # —— Skill 混合方案新增字段 ——
                    "completed_minutes": plan.completed_minutes,
                    "completed_sec": plan.completed_sec,
                    "skill_baseline_sec": plan.skill_baseline_sec,
                    "session_accum_sec": plan.session_accum_sec,
                    "skill_available": not self._skill_fallback_used,
                    "success": plan.success_count,
                    "fail": plan.fail_count,
                    "current_interval": round(cur_interval, 1),
                    "next_run_at": round(next_run_at, 3),
                    "state": state_text,
                    "book": book_snap,
                    "last_report_ts": last_report_ts,
                }
            )

            # 等待下一次
            if not self._interruptible_sleep(cur_interval):
                return

        self._set_state("已停止")
        log.info("调度器已停止")

    # ---------------- 登录态健康巡检（阅读过程中） ----------------
    def _maybe_run_health_check(self) -> None:
        """每次循环入口尝试；只有到达下一次健康检查时间戳才会真的发起 check_session。"""
        now = time.time()
        with self._health_lock:
            if self._health_next_ts <= 0.0:
                self._health_next_ts = now + max(180.0, float(self._cfg.get("reading.health_check_first_min") or 3) * 60)
            if now < self._health_next_ts:
                return
            # 占领这次窗口，其它分支（比如 pause/stop 过程中重入）不会重复跑
            self._health_next_ts = now + max(180.0, float(self._health_interval_sec))
        self.log.emit("🔍 登录态巡检中：自动检查 chapter_sync / shelf_sync / wr_skey 刷新")
        ok = self._api.check_session()
        if ok:
            with self._health_lock:
                self._health_fail_count = 0
            self.log.emit("✅ 登录态巡检通过，下一轮 ≈ {} 分钟后".format(round(max(180, self._health_interval_sec) / 60, 1)))
            return
        # 失败：尝试自动续期
        self.log.emit("⚠️ 巡检发现登录态异常，正在自动刷新 Cookie ...")
        refreshed = self._api.ensure_session()
        with self._health_lock:
            if refreshed:
                self._health_fail_count = 0
                self.log.emit("✅ 巡检刷新 Cookie 成功，已恢复")
                return
            self._health_fail_count += 1
            self._health_next_ts = now + 5 * 60  # 失败后缩短到 5 分钟后再试
        self.log.emit(
            "❌ 巡检刷新失败（本次失败计数 {}），已切换为主动告警模式".format(self._health_fail_count)
        )
        # 连续 2 次都救不回 → 推送 WxPusher（配置 notify_cookie_fail=true 才推）
        if self._health_fail_count >= 2:
            self._set_state("登录态失效，等待扫码")
            self._notify_cookie_fail()
            self.cookie_fail_reported.emit()
        return

    # ---------------- helpers ----------------
    def _ensure_daily_plan(self) -> DailyPlan:
        today = dt.date.today().isoformat()
        with self._daily_lock:
            if self._daily is None or self._daily.date != today:
                cfg = self._cfg.get("reading", {}) or {}
                mn = max(0, int(cfg.get("min_hours", 8)))
                mx = max(mn, int(cfg.get("max_hours", 10)))
                target_min = mn * 60
                target_max = mx * 60
                # —— 需求3：当天持久化，从 config 读今日目标 ——
                persisted = cfg.get("daily_plan") or {}
                persisted_date = str(persisted.get("date") or "").strip()
                persisted_target = int(persisted.get("target_minutes") or 0)
                is_new_day = (persisted_date != today) or (persisted_target <= 0)
                if is_new_day:
                    # 首次启动（或次日）→ 随机取新值并写回 config
                    target = random.randint(target_min, target_max)
                    self._cfg.set("reading.daily_plan", {
                        "date": today,
                        "target_minutes": target,
                    })
                    self._daily = DailyPlan(date=today, target_minutes=target)
                    self._cookie_fail_notified = False
                    self.log.emit(
                        f"【{today}】今日阅读任务：{target // 60} 小时 {target % 60} 分钟"
                    )
                    # —— 首次创建计划时，立即尝试获取 Skill 基线 ——
                    self._fetch_skill_baseline(self._daily)
                    # WxPusher 推送只在首次取值时发送（避免重启重复推送）
                    self._notifier.send_async(
                        f"【微信读书助手】今日任务：{target // 60}h{target % 60:02d}m，开始执行。"
                        f"{'（Skill 基线 ' + str(self._daily.skill_baseline_sec) + 's）' if self._daily.skill_baseline_sec else ''}",
                        dedup_key=f"plan_{today}",
                        dedup_window_sec=3600 * 23,
                    )
                else:
                    # 当天重启 → 复用已持久化的目标值
                    target = persisted_target
                    self._daily = DailyPlan(date=today, target_minutes=target)
                    self._cookie_fail_notified = False
                    self.log.emit(
                        f"【{today}】今日阅读任务（恢复）：{target // 60} 小时 {target % 60} 分钟"
                    )
                    # 恢复时也获取 Skill 基线
                    self._fetch_skill_baseline(self._daily)
            return self._daily

    def _do_one_reading_step(self, plan: DailyPlan) -> bool:
        last_time = int(time.time()) - 30
        ok = self._api.read_once(last_time=last_time)
        # ===== 连续 2 次 HTTP 200 空 body → 自动清缓存 + 重拉章节池 =====
        try:
            cb = self._api.current_book() or {}
            bid = str(cb.get("book_id") or "").strip() or "__unknown__"
        except Exception:  # noqa: BLE001
            bid = "__unknown__"
        is_empty = bool(getattr(self._api, "_last_read_empty_body", False))
        if is_empty and not ok:
            # 命中空 body → 计数器 +1
            self._consec_empty_body[bid] = int(self._consec_empty_body.get(bid, 0)) + 1
            log.info(
                "scheduler：书籍 %s 命中空 body（本次连续第 %d 次）",
                bid, self._consec_empty_body[bid],
            )
            if self._consec_empty_body[bid] >= 2:
                self.log.emit(
                    f"⚠️ 书籍 {bid[:20]} 连续 {self._consec_empty_body[bid]} 次返回空 body，"
                    "自动丢弃旧章节池并重拉..."
                )
                log.warning(
                    "书籍 %s 连续 %d 次空 body，自动重拉章节池",
                    bid, self._consec_empty_body[bid],
                )
                try:
                    # 后台不阻塞：先清 scoped，再 refresh_chapters_for_book（内部走 web 签名化 chapterInfos + 立即写盘）
                    self._api.scoped_chapters.pop(bid, None)
                    refreshed = self._api.refresh_chapters_for_book(bid)
                    log.info("书籍 %s 自动重拉章节池结果：%s", bid, refreshed)
                except Exception as _e:  # noqa: BLE001
                    log.warning("书籍 %s 自动重拉章节池异常：%s", bid, _e)
                finally:
                    # 无论成功失败都重置计数器，避免下一轮一上来就又重刷
                    self._consec_empty_body[bid] = 0
        else:
            # 成功 / 其它失败 → 计数器清零
            self._consec_empty_body[bid] = 0

        with self._daily_lock:
            if ok:
                plan.success_count += 1
                # —— 混合方案：同时累加本地估算分钟数 + 秒数（用于 Skill 估算）——
                plan.started_minutes += 1  # 一次 30s 左右 ≈ 0.5 分钟，累计 +1 偏保守
                plan.session_accum_sec += 30  # 每次成功阅读约 30 秒
            else:
                plan.fail_count += 1
        if ok:
            completed = plan.completed_minutes
            self.log.emit(
                f"阅读进度：{completed}/{plan.target_minutes} 分钟 "
                f"（{completed * 100 // max(1, plan.target_minutes)}%）"
                f"{'（Skill 基线 +' if plan.skill_baseline_sec else ''}估算"
            )
        else:
            # 若连续失败且登录态真的失效 → 通知
            if not self._api.check_session():
                self.log.emit("检测到登录态已失效，准备推送提醒")
                self._notify_cookie_fail()
        return ok

    def _notify_cookie_fail(self) -> None:
        if self._cookie_fail_notified:
            return
        if not (self._cfg.get("push.notify_cookie_fail", True)):
            return
        self._cookie_fail_notified = True
        self.cookie_broken.emit()
        self._notifier.send_async(
            "【微信读书助手】Cookie 已失效，请打开软件重新扫码登录，否则今日任务将无法完成。",
            max_attempts=3,
            dedup_key="cookie_fail",
            dedup_window_sec=3600 * 4,
        )

    # ---------------- Skill 基线（混合方案完成度检测） ----------------
    def _fetch_skill_baseline(self, plan: DailyPlan) -> None:
        """获取 Skill 网关的今日阅读时长基线。

        如果 Skill 不可用，降级为本地估算并记录警告。
        """
        try:
            summary = self._api.fetch_daily_reading_summary()
            today_sec = summary.get("today_seconds")
            if today_sec is not None and isinstance(today_sec, (int, float)) and today_sec >= 0:
                plan.skill_baseline_sec = int(today_sec)
                self._last_skill_refresh_ts = time.time()
                if self._skill_fallback_used:
                    self.log.emit(f"✅ Skill 基线已恢复：今日 {plan.skill_baseline_sec}s")
                    self._skill_fallback_used = False
                else:
                    self.log.emit(f"📊 Skill 基线：今日已读 {plan.skill_baseline_sec}s")
            else:
                self._log_skill_fallback("Skill 返回今日时长为 None")
        except Exception as exc:  # noqa: BLE001
            self._log_skill_fallback(f"Skill 基线获取异常：{exc}")

    def _maybe_refresh_skill_baseline(self, plan: DailyPlan) -> None:
        """周期性刷新 Skill 基线（每 5 分钟或每 N 次成功阅读）。"""
        now = time.time()
        time_elapsed = (now - self._last_skill_refresh_ts) >= self._skill_refresh_interval_sec
        count_elapsed = (plan.success_count > 0 and plan.success_count % self._skill_success_refresh_count == 0)
        if not (time_elapsed or count_elapsed):
            return
        try:
            summary = self._api.fetch_daily_reading_summary()
            today_sec = summary.get("today_seconds")
            if today_sec is not None and isinstance(today_sec, (int, float)) and today_sec >= 0:
                plan.skill_baseline_sec = int(today_sec)
                plan.session_accum_sec = 0  # 刷新后归零，从新基线开始累加
                self._last_skill_refresh_ts = now
                self.log.emit(
                    f"📊 Skill 基线刷新：今日 {plan.skill_baseline_sec}s"
                    f"（{plan.completed_minutes}/{plan.target_minutes} 分钟）"
                )
                self._skill_fallback_used = False
            else:
                self._log_skill_fallback("Skill 刷新返回 None")
        except Exception as exc:  # noqa: BLE001
            self._log_skill_fallback(f"Skill 刷新异常：{exc}")

    def _log_skill_fallback(self, reason: str) -> None:
        """Skill 不可用时记录降级日志（只记录一次）。"""
        if not self._skill_fallback_used:
            self._skill_fallback_used = True
            self.log.emit(
                f"⚠️ Skill 网关不可用（{reason}），"
                f"完成度检测降级为本地估算模式"
            )

    def _notify_daily_done(self, minutes: int) -> None:
        if not (self._cfg.get("push.notify_daily_done", True)):
            return
        today = dt.date.today().isoformat()
        self._notifier.send_async(
            f"【微信读书助手】{today} 任务已完成，共约 {minutes // 60} 小时 {minutes % 60:02d} 分钟。",
            dedup_key=f"daily_{today}",
            dedup_window_sec=3600 * 23,
        )
        self.task_completed.emit(minutes)

    def _sleep_till_next_minute(self, plan_date: str) -> bool:
        """在今天任务结束后，小睡 60 秒等待日期滚动。"""
        total = 0
        while total < 60 and not self._stop_event.is_set():
            if dt.date.today().isoformat() != plan_date:
                return True
            self._pause_event.wait(timeout=2)
            self._check_pause()
            total += 2
        return not self._stop_event.is_set()

    def _interruptible_sleep(self, seconds: float) -> bool:
        """可被 stop/pause 打断的 sleep，返回 False 表示收到 stop。"""
        end = time.time() + seconds
        while time.time() < end:
            if self._stop_event.is_set():
                return False
            self._pause_event.wait(timeout=0.5)
            self._check_pause()
        return True

    def _check_pause(self) -> None:
        # 如果被 pause 了就一直等恢复
        while not self._pause_event.is_set() and not self._stop_event.is_set():
            self._pause_event.wait(timeout=0.5)
