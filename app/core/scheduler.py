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
from .weread_skills import WeReadSkills
from .notifier import WxPusherNotifier
from app.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class DailyPlan:
    date: str                   # YYYY-MM-DD
    target_minutes: int         # 今日目标总分钟数
    started_minutes: int = 0    # 已完成分钟数（每成功 1 次约计 0.5 分钟）
    success_count: int = 0
    fail_count: int = 0

    @property
    def remaining_minutes(self) -> int:
        return max(0, self.target_minutes - self.started_minutes)


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
    # —— Skill 同步：每 N 次成功阅读触发一次，返回阅读统计 + 书架 + 书籍详情
    skill_sync_completed = Signal(dict)

    def __init__(
        self,
        api: WeReadApi,
        config: ConfigStore | None = None,
        notifier: WxPusherNotifier | None = None,
        skills: WeReadSkills | None = None,
    ) -> None:
        super().__init__()
        self._api = api
        self._cfg = config or ConfigStore()
        self._notifier = notifier or WxPusherNotifier(self._cfg)
        self._skills = skills or WeReadSkills(self._cfg)
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
        # —— Skill 触发：每成功 N 次 read_once 触发一次 weread-skills 同步
        self._skills_lock = threading.Lock()
        self._skills_next_trigger_at: int = self._skills.refresh_every_n_reads()
        # 允许外部（StatusPage "立即刷新"按钮）手动触发一次；避免同一时间重复跑
        self._skills_worker_running: bool = False
        self._skills_started_at: float = 0.0
        # —— 看门狗（UI 最担心的"按钮一直禁用"）：超过 SKILL_MAX_SECONDS 还没完成就算超时，直接放锁+发结果
        self._skill_max_seconds: int = 18  # 单条网关 POST 默认 15s；三条串行最多给 18s 留 3s 冗余
        self._skills_watchdog_timer: QTimer | None = None  # UI 线程初始化后再 new（懒加载，跨线程安全）

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
            if plan.remaining_minutes <= 0:
                self._set_state("今日任务已完成，空闲中")
                self._notify_daily_done(plan.started_minutes)
                if not self._sleep_till_next_minute(plan.date):
                    return
                continue
            self._set_state("阅读中")
            # 执行一次
            ok = self._do_one_reading_step(plan)
            # 进度上报
            cfg = self._cfg.get("reading", {}) or {}
            cur_interval = random.uniform(
                cfg.get("min_interval_sec", 25), cfg.get("max_interval_sec", 45)
            )
            # 每完成 ~20 次加一段较长休息（模拟疲劳停顿）
            if plan.success_count > 0 and plan.success_count % 20 == 0:
                cur_interval += random.uniform(60, 180)
            # ±15% 抖动
            cur_interval = cur_interval * random.uniform(0.85, 1.15)
            if not ok:
                # 失败时给更长冷却，避免把接口打爆
                cur_interval = max(cur_interval, 60.0)

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
                # 目标：以分钟为粒度随机；避免恰好整点（9小时 → 9小时47分钟这种感觉）
                target_min = mn * 60
                target_max = mx * 60
                target = random.randint(target_min, target_max)
                self._daily = DailyPlan(date=today, target_minutes=target)
                self._cookie_fail_notified = False
                self.log.emit(
                    f"【{today}】今日阅读任务：{target // 60} 小时 {target % 60} 分钟"
                )
                self._notifier.send_async(
                    f"【微信读书助手】今日任务：{target // 60}h{target % 60:02d}m，开始执行。",
                    dedup_key=f"plan_{today}",
                    dedup_window_sec=3600 * 23,
                )
            return self._daily

    def _do_one_reading_step(self, plan: DailyPlan) -> bool:
        last_time = int(time.time()) - 30
        ok = self._api.read_once(last_time=last_time)
        with self._daily_lock:
            if ok:
                plan.success_count += 1
                plan.started_minutes += 1  # 一次 30s 左右 ≈ 0.5 分钟，累计 +1 偏保守
            else:
                plan.fail_count += 1
        if ok:
            self.log.emit(
                f"阅读进度：{plan.started_minutes}/{plan.target_minutes} 分钟 "
                f"（{plan.started_minutes * 100 // max(1, plan.target_minutes)}%）"
            )
            # —— Skill 阈值触发：每 N 次成功阅读 → 拉一次 weread-skills 统计/书架/当前书进度
            self._maybe_run_skills_sync(plan.success_count, force=False)
        else:
            # 若连续失败且登录态真的失效 → 通知
            if not self._api.check_session():
                self.log.emit("检测到登录态已失效，准备推送提醒")
                self._notify_cookie_fail()
        return ok

    # ---------------- weread-skills 同步（每 N 次成功读 / 手动触发） ----------------
    def _ensure_skill_watchdog(self) -> None:
        """在 UI 线程里懒加载看门狗 QTimer（QTimer 只能在拥有 event loop 的线程构造）。"""
        if getattr(self, "_skills_watchdog_timer", None) is None:
            # 因为 ReadingScheduler 继承自 QObject，我们在构造时就在同一个 thread（主 UI）
            # 所以这里直接 new 并 moveToThread(self.thread()) 是安全的。
            from PySide6.QtCore import QTimer as _QTimer
            self._skills_watchdog_timer = _QTimer(self)
            self._skills_watchdog_timer.setSingleShot(False)
            self._skills_watchdog_timer.setInterval(1000)
            self._skills_watchdog_timer.timeout.connect(self._on_skill_watchdog_tick)

    def _on_skill_watchdog_tick(self) -> None:
        import time as _time
        if not self._skills_worker_running:
            return
        started = float(self._skills_started_at or 0.0)
        if started <= 0:
            return
        elapsed = _time.time() - started
        if elapsed <= float(self._skill_max_seconds or 18):
            return
        # 判定超时：构造一个失败结果并放锁，UI 侧按钮立刻可以再点
        self._skills_worker_running = False
        if self._skills_watchdog_timer is not None:
            try:
                self._skills_watchdog_timer.stop()
            except Exception:  # noqa: BLE001
                pass
        self.log.emit(
            f"⚠️ weread-skills 同步超过 {int(self._skill_max_seconds or 18)} 秒未返回，"
            f"已自动释放按钮。请检查：① Skill Key 是否仍有效（设置页点「验证 Key 有效性」）；"
            f"② 本机网络到 i.weread.qq.com 是否可达。"
        )
        self.skill_sync_completed.emit({
            "ok": False,
            "fetched_at": int(_time.time()),
            "error": "skill_timeout",
            "readdata": {"ok": False, "http": 0, "error": "timeout", "raw_keys": []},
            "shelf": {"ok": False, "http": 0, "error": "timeout"},
            "book_info": None,
            "summary": {
                "today_seconds": None, "today_hm": "—",
                "week_seconds": None, "week_hm": "—",
                "month_seconds": None, "month_hm": "—",
                "total_seconds": None, "total_hm": "—",
                "source": "skill_timeout",
            },
            "current_book": None,
        })

    def trigger_skills_sync_nowait(self) -> None:
        """StatusPage 手动点「立即刷新」时调用：后台立即跑一次 Skill 同步。
        不再在入口处提前获取锁——所有 guard/flag/watchdog 统一由单层线程体内的 _do_skills_sync 完成，
        避免以前"入口先设 flag → 调 _maybe_run 被行 379 短路 return → fetch_all 根本不跑"的死锁。
        """
        import threading as _t
        self._ensure_skill_watchdog()
        # 快速去重（不强制，只节省一次线程创建；内层 _do_skills_sync 还会再做 CAS 级判定）
        if getattr(self, "_skills_worker_running", False):
            return
        big_count = int(getattr(self, "_skills_next_trigger_at", 0) or 0) + 999_999
        _t.Thread(
            target=self._do_skills_sync,
            kwargs={"success_count_for_progress": big_count, "force": True},
            daemon=True,
            name="SkillSyncManual",
        ).start()

    def _do_skills_sync(self, *, success_count_for_progress: int, force: bool) -> None:
        """**同步**执行一次 weread-skills 同步：阈值/启用判断 → 拉取 → 发信号 → 收尾。
        调用方（手动 trigger_skills_sync_nowait / 自动 scheduler 内触发）都必须：
          (a) 已经 self._ensure_skill_watchdog() 过（保证 QTimer 在有事件循环的线程）；
          (b) 把本方法包在 threading.Thread 里启动（避免阻塞所在线程）。
        单层线程设计：一次线程创建 → 整段跑到底 → finally 解锁。不再双层嵌套。
        """
        import time as _time
        # 1) 启用检查：手动 force=True 允许越过 enabled；自动触发需要显式开启
        if not force and not self._skills.enabled:
            return
        threshold = self._skills.refresh_every_n_reads()
        # 2) 阈值（仅自动）& 下一次触发点（哪怕 force=True 也更新一下，避免连续手动之后回不来）
        with self._skills_lock:
            if not force:
                if success_count_for_progress < self._skills_next_trigger_at:
                    return
            # 下一次触发点：按 success_count 最接近的 threshold 整数倍
            if success_count_for_progress >= 0 and threshold > 0:
                self._skills_next_trigger_at = ((success_count_for_progress // threshold) + 1) * threshold
        # 3) CAS 级获取 running 标志（唯一真入口）
        if self._skills_worker_running:
            return
        self._ensure_skill_watchdog()
        self._skills_worker_running = True
        self._skills_started_at = _time.time()
        try:
            if self._skills_watchdog_timer is not None:
                self._skills_watchdog_timer.start()
        except Exception:  # noqa: BLE001
            pass
        self.log.emit(
            "📚 weread-skills 同步中：{} 成功次数={} 阈值=每 {} 次".format(
                ("强制触发" if force else "阈值触发"), success_count_for_progress, threshold,
            )
        )

        try:
            cur = self._api.current_book() or {}
            res = self._skills.fetch_all(
                current_book_id=str(cur.get("book_id") or "").strip() or None,
                current_reader_id=str(cur.get("book_reader_id") or "").strip() or None,
            )
            # —— 终极兜底：Skill 端未取到的 today/week/month/total 桶，用 Cookie 会话（weread_api 9 端点）补足 ——
            summary = res.get("summary") if isinstance(res.get("summary"), dict) else {}
            need_fill = any(
                summary.get(k) is None
                for k in ("today_seconds", "week_seconds", "month_seconds", "total_seconds")
            )
            if need_fill:
                try:
                    cookie_res = self._api.fetch_daily_reading_summary(timeout=10)
                    if isinstance(cookie_res, dict):
                        new_summary = dict(summary)
                        merged_source_parts = [str(new_summary.get("source") or "readdata_detail_multi")]
                        any_filled = False
                        for sec_k, hm_k in (
                            ("today_seconds", "today_hm"),
                            ("week_seconds", "week_hm"),
                            ("month_seconds", "month_hm"),
                            ("total_seconds", "total_hm"),
                        ):
                            if new_summary.get(sec_k) is None:
                                cv = cookie_res.get(sec_k)
                                if isinstance(cv, int) and cv > 0:
                                    new_summary[sec_k] = cv
                                    hm_v = cookie_res.get(hm_k)
                                    if isinstance(hm_v, str) and hm_v and hm_v != "-":
                                        new_summary[hm_k] = hm_v
                                    any_filled = True
                        if any_filled:
                            csrc = cookie_res.get("source") or "cookie_api"
                            merged_source_parts.append(f"cookie_fill:{csrc}")
                            new_summary["source"] = "+".join(merged_source_parts)
                            self.log.emit(
                                "📚 Cookie 会话兜底补全时长：today={} week={} month={} total={}（source={}）".format(
                                    new_summary.get("today_hm"), new_summary.get("week_hm"),
                                    new_summary.get("month_hm"), new_summary.get("total_hm"),
                                    new_summary["source"],
                                )
                            )
                            res["summary"] = new_summary
                except Exception as e:  # noqa: BLE001
                    log.warning("Skill 后 Cookie 兜底失败：%s", e)
            # 同步 current_book（Skill 给了真实书名/进度 → 覆盖之前"未选择书籍"的占位）
            # —— 关键：Skill 的 shelf_best / book_info 派生出的 reader_id 可能与"用户显式设置的 URL(reader_id)"
            #    完全不是同一本书（典型：用户手动贴了《金蚕往事》的 URL，shelf 最佳还是《祈祷落幕时》）。
            #    如果检测到两边 reader_id 真不一致：不要把 shelf 的 book_id/title 强行覆盖用户的选择 →  chimera 错误。
            cb = res.get("current_book")
            prev_book = self._api.current_book() or {}
            apply_skill_cb = True
            if isinstance(cb, dict) and isinstance(prev_book, dict):
                rid_skill = str(cb.get("book_reader_id") or "").strip().lower()
                rid_prev = str(prev_book.get("book_reader_id") or "").strip().lower()
                title_skill = str(cb.get("title") or "").strip()
                title_prev = str(prev_book.get("title") or "").strip()
                bid_skill = str(cb.get("book_id") or "").strip()
                bid_prev = str(prev_book.get("book_id") or "").strip()
                prev_meaningful = bool((title_prev and title_prev not in ("未选择书籍", "未命名书籍")) or bid_prev)
                skill_meaningful = bool(title_skill or bid_skill)
                # —— 两种情况拒绝 Skill 覆盖：
                # (a) reader_id 明确不同，且旧书已有"有意义的 title/book_id"（=用户之前已经 set 过一次）
                # (b) title 明确不同，两边都有 title
                if prev_meaningful and skill_meaningful:
                    def _rid_core(r: str) -> str:
                        # 去掉常见 wb/wr 前缀，再比核心串（有些派生不一致但核心 hex 相同）
                        if r.startswith("wb") or r.startswith("wr"):
                            return r[2:] if len(r) > 2 else r
                        return r
                    if rid_skill and rid_prev and _rid_core(rid_skill) != _rid_core(rid_prev):
                        # 两边都有 reader_id 且核心不匹配 → 绝对不能混
                        apply_skill_cb = False
                        self.log.emit(
                            "📚 Skill 结果与当前书 reader_id 不匹配（Skill={} vs 当前={}）：保留用户手动设置"
                            .format(
                                (title_skill or bid_skill or "—"),
                                (title_prev or bid_prev or "—"),
                            )
                        )
                    elif title_skill and title_prev and title_skill != title_prev:
                        # 书名不一致：如果 Skill 没有提供 reader_id 或 bid_prev 空，倾向保留旧（更贴近用户当前导航）
                        pass
            if apply_skill_cb and isinstance(cb, dict) and (cb.get("title") or cb.get("book_id") or cb.get("book_reader_id")):
                final_payload = dict(cb)
                # —— 额外保护：若 Skill 产生了"reader_id（原用户）≠ book_id（shelf 最佳）"的错配对象，
                #    而之前的 payload 是对的（reader_id=用户设置的），那我们只取 Skill 的字段做"补字段"而不是完整覆写。
                if isinstance(prev_book, dict):
                    prev_reader = str(prev_book.get("book_reader_id") or "").strip()
                    sk_reader = str(final_payload.get("book_reader_id") or "").strip()
                    # 旧 URL 的 reader_id 比 skill 的更"最新"→ 保留旧 reader_id/url/chapter
                    if prev_reader and (not sk_reader or sk_reader.lower() != prev_reader.lower()):
                        final_payload["book_reader_id"] = prev_reader
                        prev_url = str(prev_book.get("url") or "").strip()
                        if prev_url:
                            final_payload["url"] = prev_url
                        prev_ch = str(prev_book.get("chapter_id") or "").strip()
                        if prev_ch:
                            final_payload["chapter_id"] = prev_ch
                self._api.set_current_book(final_payload, source=cb.get("source") or "skill_sync")
            # —— 进度从 Cookie 版 shelf_sync 兜底（Skill shelf 只有 10 字段，官方 web/shelf/sync 才有真 readingProgress）
            if isinstance(self._api.current_book(), dict):
                try:
                    pb = self._api.inject_progress_from_cookie_shelf(timeout=8)
                    if isinstance(pb, dict):
                        self.log.emit(
                            "📚 Cookie shelf 补充进度：title={} progress={}".format(
                                str(pb.get("title") or "—"),
                                str(pb.get("progress_text") or "(无)"),
                            )
                        )
                except Exception as _e_pb:  # noqa: BLE001
                    log.warning("inject_progress_from_cookie_shelf 异常：%s", _e_pb)
            ok = bool(res.get("ok"))
            summary = res.get("summary") or {}
            t_hm = summary.get("today_hm") if isinstance(summary, dict) else None
            self.log.emit(
                "📚 weread-skills 同步完成：{} 今日={} book={}".format(
                    "✅" if ok else "⚠️",
                    (t_hm or "—"),
                    (("《" + str(cb.get("title"))[:16] + "》") if isinstance(cb, dict) and cb.get("title") else "(未解析)"),
                )
            )
            self.skill_sync_completed.emit(res)
        except Exception as exc:  # noqa: BLE001
            log.warning("weread-skills 同步异常：%s", exc, exc_info=True)
            self.log.emit(f"⚠️ weread-skills 同步异常：{exc}")
            self.skill_sync_completed.emit({
                "ok": False, "fetched_at": int(_time.time()),
                "error": f"exception: {exc}",
                "readdata": {"ok": False, "http": 0, "error": str(exc), "raw_keys": []},
                "shelf": {"ok": False, "http": 0, "error": str(exc)},
                "book_info": None,
                "summary": {
                    "today_seconds": None, "today_hm": "—",
                    "week_seconds": None, "week_hm": "—",
                    "month_seconds": None, "month_hm": "—",
                    "total_seconds": None, "total_hm": "—",
                    "source": f"skill_exception:{type(exc).__name__}",
                },
                "current_book": None,
            })
        finally:
            self._skills_worker_running = False
            try:
                if self._skills_watchdog_timer is not None:
                    self._skills_watchdog_timer.stop()
            except Exception:  # noqa: BLE001
                pass

    def _maybe_run_skills_sync(self, success_count: int, *, force: bool) -> None:  # noqa: FBT001
        """**向后兼容**：老的自动触发入口（scheduler 主循环里每 N 次成功读调用）。
        只做一件事：创建单层线程，跑上面新的 _do_skills_sync。不再有双层嵌套 finally 提前清锁。
        """
        # 提前快检（省一次线程创建）：enabled / running
        if not force and not self._skills.enabled:
            return
        if getattr(self, "_skills_worker_running", False):
            return
        self._ensure_skill_watchdog()
        import threading as _t
        _t.Thread(
            target=self._do_skills_sync,
            kwargs={"success_count_for_progress": success_count, "force": force},
            daemon=True,
            name="SkillSyncAuto",
        ).start()

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
