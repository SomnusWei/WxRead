"""主界面（v2 上下布局）。

布局：
  ┌────────────────────────┬─────────────────────────────────┐
  │   上左：状态+控制       │   上右：统计+操作               │
  │   日期/状态/登录态      │   阅读统计卡                   │
  │   今日目标/已完成       │   扫码登录/查看书架/获取数据    │
  │   成功失败/下次请求     │   设置                         │
  │   当前阅读书籍         │                                 │
  │   控制按钮（开始/暂停） │                                 │
  └────────────────────────┴─────────────────────────────────┘
  ┌──────────────────────────────────────────────────────────────┐
  │  📋 工作日志                              [清空] [暂停滚动] │
  └──────────────────────────────────────────────────────────────┘

特性：
  - 接收 scheduler 的多个 Qt 信号实时更新 UI
  - 工作日志 100ms 批量合并追加
  - 进度数字缓动动画
  - CDP 登录对话框弹出
"""
from __future__ import annotations

import time
from typing import Any

from PySide6.QtCore import QTimer, Qt, Signal, QThread, QObject
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.config import ConfigStore
from app.core.local_db import LocalDB
from app.core.notifier import WxPusherNotifier
from app.core.scheduler import Scheduler
from app.core.skill_api import SkillAPI
from app.core.weread_api import WeReadApi
from app.utils.logger import get_logger
from app.utils.logger import log_bus

log = get_logger(__name__)


class _FetchThread(QThread):
    """后台获取书架/章节池/进度（线程安全，用信号通知主线程）。"""

    log_msg = Signal(str)
    stats_updated = Signal(dict)
    done = Signal()

    def __init__(self, skill: SkillAPI, db: "LocalDB", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._skill = skill
        self._db = db

    def run(self) -> None:  # noqa: D401
        try:
            books = self._skill.fetch_shelf()
            self.log_msg.emit(f"🔄 fetch_shelf 返回 {len(books)} 本")
            if books:
                self._db.update_shelf(books)
                self.log_msg.emit(f"📚 书架更新完成：{len(books)} 本")
            else:
                self.log_msg.emit("⚠️ fetch_shelf 返回空，请检查 Skill API Key 或响应格式")
            # 获取未读完书的章节池和进度
            unread = self._db.get_unread_books()
            self.log_msg.emit(f"📖 未读完书：{len(unread)} 本，开始拉章节池/进度（前 10 本）")
            for book in unread[:10]:  # 限制前 10 本避免太久
                bid = str(book.get("bookId"))
                title = str(book.get("title", "未知"))
                if not self._db.has_chapters(bid):
                    chapters = self._skill.fetch_chapters(bid)
                    if chapters:
                        self._db.update_chapters(bid, title, chapters)
                        self.log_msg.emit(f"📖 章节池：{title} → {len(chapters)} 章")
                progress_data = self._skill.fetch_progress(bid)
                if progress_data:
                    percent = progress_data.get("percent")
                    if percent is not None:
                        try:
                            self._db.update_book_progress(bid, int(float(percent)))
                        except (TypeError, ValueError):
                            pass
            # 刷新统计
            stats = self._skill.fetch_reading_stats()
            if stats:
                local_stats = {
                    "today_seconds": stats.get("today_seconds") or 0,
                    "weekly_seconds": stats.get("week_seconds") or 0,
                    "monthly_seconds": stats.get("month_seconds") or 0,
                    "total_seconds": stats.get("total_seconds") or 0,
                }
                self._db.update_reading_stats(local_stats)
                self.stats_updated.emit(local_stats)
                self.log_msg.emit(f"📊 统计已更新：今日 {local_stats['today_seconds']}s")
        except Exception as exc:  # noqa: BLE001
            log.exception("获取数据失败")
            self.log_msg.emit(f"❌ 获取数据失败：{exc}")
        finally:
            self.done.emit()


class _SkillRefreshThread(QThread):
    """后台刷新 Skill 单项数据（统计 or 当前进度），线程安全。"""

    log_msg = Signal(str)
    stats_updated = Signal(dict)
    book_progress_updated = Signal(str, int)  # book_id, percent
    done = Signal()

    def __init__(
        self,
        skill: SkillAPI,
        db: "LocalDB",
        mode: str,  # "stats" | "progress"
        book_id: str | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._skill = skill
        self._db = db
        self._mode = mode
        self._book_id = book_id

    def run(self) -> None:  # noqa: D401
        try:
            if self._mode == "stats":
                self._refresh_stats()
            elif self._mode == "progress":
                self._refresh_progress()
        except Exception as exc:  # noqa: BLE001
            log.exception("Skill 刷新失败（mode=%s）", self._mode)
            self.log_msg.emit(f"❌ Skill 刷新失败：{exc}")
        finally:
            self.done.emit()

    def _refresh_stats(self) -> None:
        self.log_msg.emit("📊 正在获取 Skill 阅读统计...")
        stats = self._skill.fetch_reading_stats()
        if not stats:
            self.log_msg.emit("❌ 获取阅读统计失败（返回空）")
            return
        local_stats = {
            "today_seconds": stats.get("today_seconds") or 0,
            "weekly_seconds": stats.get("week_seconds") or 0,
            "monthly_seconds": stats.get("month_seconds") or 0,
            "total_seconds": stats.get("total_seconds") or 0,
        }
        self._db.update_reading_stats(local_stats)
        self.stats_updated.emit(local_stats)
        self.log_msg.emit(
            f"✅ 阅读统计已刷新：今日 {local_stats['today_seconds'] // 60} 分钟"
        )

    def _refresh_progress(self) -> None:
        if not self._book_id:
            self.log_msg.emit("⚠️ 无当前书籍，无法刷新进度")
            return
        self.log_msg.emit(f"📖 正在获取 Skill 书籍进度：bookId={self._book_id}")
        progress_data = self._skill.fetch_progress(self._book_id)
        if not progress_data:
            self.log_msg.emit("❌ 获取书籍进度失败（返回空）")
            return
        percent = progress_data.get("percent")
        if percent is None:
            self.log_msg.emit("❌ 书籍进度返回无 percent 字段")
            return
        try:
            new_progress = int(float(percent))
        except (TypeError, ValueError):
            self.log_msg.emit(f"❌ percent 格式异常：{percent}")
            return
        self._db.update_book_progress(self._book_id, new_progress)
        self.book_progress_updated.emit(self._book_id, new_progress)
        self.log_msg.emit(f"✅ 书籍进度已刷新：{new_progress}%")


class MainPage(QWidget):
    """主界面（上下布局）。"""

    def __init__(
        self,
        config: ConfigStore,
        api: WeReadApi,
        skill: SkillAPI,
        db: LocalDB,
        notifier: WxPusherNotifier,
        scheduler: Scheduler,
        main_window: QMainWindow | None = None,
    ) -> None:
        super().__init__()
        self._cfg = config
        self._api = api
        self._skill = skill
        self._db = db
        self._notifier = notifier
        self._scheduler = scheduler
        self._main_window = main_window

        # 日志批量缓冲
        self._log_buffer: list[str] = []
        self._log_auto_scroll = True

        # 当前阅读书的 bookId（从 scheduler progress 信号捕获，供"刷新进度"按钮用）
        self._current_book_id: str | None = None
        # Skill 刷新线程引用（防止 GC）
        self._skill_refresh_thread: _SkillRefreshThread | None = None

        self._build_ui()
        self._connect_signals()
        self._init_log_flush_timer()
        self._refresh_login_status()
        self._refresh_reading_stats()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # ===== 上部：左右分栏 =====
        top_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        top_splitter.setChildrenCollapsible(False)
        top_splitter.setHandleWidth(3)

        # 上左：状态+控制
        left_panel = self._build_left_panel()
        top_splitter.addWidget(left_panel)

        # 上右：统计+操作
        right_panel = self._build_right_panel()
        top_splitter.addWidget(right_panel)

        top_splitter.setStretchFactor(0, 3)
        top_splitter.setStretchFactor(1, 2)
        top_splitter.setSizes([600, 400])
        root.addWidget(top_splitter, 3)

        # ===== 下部：工作日志 =====
        log_box = self._build_log_panel()
        root.addWidget(log_box, 1)

    def _build_left_panel(self) -> QWidget:
        panel = QFrame()
        panel.setStyleSheet(
            "QFrame{background:#ffffff;border:1px solid #e3e8f1;border-radius:8px;}"
        )
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(5)

        # 基础状态（2 列网格更紧凑）
        status_box = QGroupBox("状态")
        status_grid = QGridLayout(status_box)
        status_grid.setSpacing(4)
        status_grid.setHorizontalSpacing(12)

        self._lbl_date = QLabel("📅 日期：-")
        self._lbl_status = QLabel("🔄 状态：待机")
        self._lbl_login = QLabel("🔐 登录态：检查中...")
        self._lbl_next = QLabel("⏱️ 下次请求：-")
        self._lbl_target = QLabel("🎯 今日目标：- 分钟")
        self._lbl_done = QLabel("✅ 已完成：- 分钟 (-%)")
        self._lbl_success_fail = QLabel("📊 成功：0  失败：0")
        for lbl in (
            self._lbl_date, self._lbl_status, self._lbl_login,
            self._lbl_next, self._lbl_target, self._lbl_done,
            self._lbl_success_fail,
        ):
            lbl.setStyleSheet("color:#2f3b52;font-size:12px;")
        # 2 列布局：左列 4 项，右列 3 项
        status_grid.addWidget(self._lbl_date, 0, 0)
        status_grid.addWidget(self._lbl_status, 0, 1)
        status_grid.addWidget(self._lbl_login, 1, 0)
        status_grid.addWidget(self._lbl_next, 1, 1)
        status_grid.addWidget(self._lbl_target, 2, 0)
        status_grid.addWidget(self._lbl_done, 2, 1)
        status_grid.addWidget(self._lbl_success_fail, 3, 0, 1, 2)
        layout.addWidget(status_box)

        # 当前阅读书籍
        book_box = QGroupBox("📖 当前阅读书籍")
        book_layout = QVBoxLayout(book_box)
        book_layout.setSpacing(4)
        self._lbl_book_title = QLabel("书名：-")
        self._lbl_book_progress = QLabel("书籍进度：-")
        self._lbl_chapter = QLabel("当前章节：-")
        self._lbl_chapter_count = QLabel("本章已读：- 次")
        for lbl in (
            self._lbl_book_title, self._lbl_book_progress,
            self._lbl_chapter, self._lbl_chapter_count,
        ):
            lbl.setStyleSheet("color:#2f3b52;font-size:12px;")
            book_layout.addWidget(lbl)
        layout.addWidget(book_box)

        # 控制按钮
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self._btn_start = QPushButton("▶ 开始")
        self._btn_start.setStyleSheet(self._btn_primary_style("#2d9d3c"))
        self._btn_start.clicked.connect(self._on_start)
        self._btn_pause = QPushButton("⏸ 暂停")
        self._btn_pause.setStyleSheet(self._btn_primary_style("#c49100"))
        self._btn_pause.setEnabled(False)
        self._btn_pause.clicked.connect(self._on_pause)
        self._btn_stop = QPushButton("⏹ 停止")
        self._btn_stop.setStyleSheet(self._btn_primary_style("#d9534f"))
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._on_stop)
        btn_row.addWidget(self._btn_start)
        btn_row.addWidget(self._btn_pause)
        btn_row.addWidget(self._btn_stop)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        layout.addStretch(1)
        return panel

    def _build_right_panel(self) -> QWidget:
        panel = QFrame()
        panel.setStyleSheet(
            "QFrame{background:#ffffff;border:1px solid #e3e8f1;border-radius:8px;}"
        )
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(5)

        # 阅读统计（2×2 网格，紧凑方块）
        stats_box = QGroupBox("📊 阅读统计")
        stats_grid = QGridLayout(stats_box)
        stats_grid.setSpacing(6)
        stats_grid.setHorizontalSpacing(16)
        self._lbl_today = QLabel("今日：-")
        self._lbl_week = QLabel("本周：-")
        self._lbl_month = QLabel("本月：-")
        self._lbl_total = QLabel("总计：-")
        for lbl in (self._lbl_today, self._lbl_week, self._lbl_month, self._lbl_total):
            lbl.setStyleSheet("color:#2f3b52;font-size:13px;font-weight:600;")
        stats_grid.addWidget(self._lbl_today, 0, 0)
        stats_grid.addWidget(self._lbl_week, 0, 1)
        stats_grid.addWidget(self._lbl_month, 1, 0)
        stats_grid.addWidget(self._lbl_total, 1, 1)
        self._lbl_last_refresh = QLabel("最后刷新：-")
        self._lbl_last_refresh.setStyleSheet("color:#93a0b8;font-size:11px;")
        stats_grid.addWidget(self._lbl_last_refresh, 2, 0, 1, 2)
        layout.addWidget(stats_box)

        # 操作按钮（2 列网格，等宽紧凑）
        ops_box = QGroupBox("🔘 操作")
        ops_grid = QGridLayout(ops_box)
        ops_grid.setSpacing(5)
        ops_grid.setHorizontalSpacing(6)
        self._btn_login = QPushButton("🔐 扫码登录")
        self._btn_login.setStyleSheet(self._btn_secondary_style())
        self._btn_login.clicked.connect(self._on_cdp_login)
        self._btn_shelf = QPushButton("📚 查看书架")
        self._btn_shelf.setStyleSheet(self._btn_secondary_style())
        self._btn_shelf.clicked.connect(self._on_view_shelf)
        self._btn_fetch = QPushButton("🔄 获取数据")
        self._btn_fetch.setStyleSheet(self._btn_secondary_style())
        self._btn_fetch.clicked.connect(self._on_fetch_data)
        self._btn_refresh_stats = QPushButton("📊 刷新统计")
        self._btn_refresh_stats.setStyleSheet(self._btn_secondary_style())
        self._btn_refresh_stats.clicked.connect(self._on_refresh_stats)
        self._btn_refresh_progress = QPushButton("📖 刷新进度")
        self._btn_refresh_progress.setStyleSheet(self._btn_secondary_style())
        self._btn_refresh_progress.clicked.connect(self._on_refresh_progress)
        self._btn_settings = QPushButton("⚙️ 设置")
        self._btn_settings.setStyleSheet(self._btn_secondary_style())
        self._btn_settings.clicked.connect(self._on_open_settings)
        # 3 行 × 2 列
        ops_grid.addWidget(self._btn_login, 0, 0)
        ops_grid.addWidget(self._btn_shelf, 0, 1)
        ops_grid.addWidget(self._btn_fetch, 1, 0)
        ops_grid.addWidget(self._btn_refresh_stats, 1, 1)
        ops_grid.addWidget(self._btn_refresh_progress, 2, 0)
        ops_grid.addWidget(self._btn_settings, 2, 1)
        layout.addWidget(ops_box)

        layout.addStretch(1)
        return panel

    def _build_log_panel(self) -> QWidget:
        box = QFrame()
        box.setStyleSheet(
            "QFrame{background:#ffffff;border:1px solid #e3e8f1;border-radius:8px;}"
        )
        layout = QVBoxLayout(box)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        header = QHBoxLayout()
        title = QLabel("📋 工作日志")
        title.setStyleSheet("font-size:14px;font-weight:600;color:#2f3b52;")
        header.addWidget(title)
        header.addStretch(1)
        self._btn_pause_scroll = QPushButton("⏸ 暂停滚动")
        self._btn_pause_scroll.setCheckable(True)
        self._btn_pause_scroll.setStyleSheet(self._btn_secondary_style())
        self._btn_pause_scroll.clicked.connect(self._on_toggle_scroll)
        self._btn_clear_log = QPushButton("🗑️ 清空")
        self._btn_clear_log.setStyleSheet(self._btn_secondary_style())
        self._btn_clear_log.clicked.connect(self._on_clear_log)
        header.addWidget(self._btn_pause_scroll)
        header.addWidget(self._btn_clear_log)
        layout.addLayout(header)

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(500)  # 自动裁剪
        self._log_view.setStyleSheet(
            "QPlainTextEdit{background:#1e1e2e;color:#cdd6f4;font-family:'Consolas',monospace;"
            "font-size:12px;border:1px solid #313244;border-radius:6px;padding:8px;}"
        )
        layout.addWidget(self._log_view)
        return box

    @staticmethod
    def _btn_primary_style(color: str) -> str:
        return (
            f"QPushButton{{background:{color};color:#fff;border:none;border-radius:8px;"
            "padding:8px 8px;font-size:13px;font-weight:600;}"
            "QPushButton:hover{opacity:0.9;}"
            "QPushButton:disabled{background:#cdd6f4;color:#fff;}"
        )

    @staticmethod
    def _btn_secondary_style() -> str:
        return (
            "QPushButton{background:#eef2f8;color:#2f3b52;border:none;border-radius:8px;"
            "padding:8px 6px;font-size:12px;}"
            "QPushButton:hover{background:#e2e8f4;}"
            "QPushButton:disabled{color:#aab3c5;background:#f3f5fb;}"
        )

    # ------------------------------------------------------------------
    # 信号连接
    # ------------------------------------------------------------------
    def _connect_signals(self) -> None:
        # scheduler 信号
        self._scheduler.state_changed.connect(self._on_state_changed)
        self._scheduler.log_emitted.connect(self._on_log_emitted)
        self._scheduler.progress.connect(self._on_progress)
        self._scheduler.book_progress_updated.connect(self._on_book_progress)
        self._scheduler.current_book_changed.connect(self._on_book_changed)
        self._scheduler.current_chapter_changed.connect(self._on_chapter_changed)
        self._scheduler.reading_stats_updated.connect(self._on_stats_updated)
        self._scheduler.cookie_broken.connect(self._on_cookie_broken)
        self._scheduler.task_completed.connect(self._on_task_completed)
        self._scheduler.cookie_hard_invalid.connect(self._on_cookie_hard)

        # LogBus 信号（接管全局日志）
        log_bus.log_emitted.connect(self._on_log_bus)

        # 日期刷新定时器（每分钟）
        self._date_timer = QTimer(self)
        self._date_timer.timeout.connect(self._refresh_date)
        self._date_timer.start(60000)
        self._refresh_date()

        # 下次请求倒计时（每秒）
        self._countdown_timer = QTimer(self)
        self._countdown_timer.timeout.connect(self._refresh_next_request)
        self._countdown_timer.start(1000)

    def _init_log_flush_timer(self) -> None:
        """日志 100ms 批量合并刷新。"""
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(100)
        self._flush_timer.timeout.connect(self._flush_log_buffer)
        self._flush_timer.start()

    # ------------------------------------------------------------------
    # UI 更新槽
    # ------------------------------------------------------------------
    def _refresh_date(self) -> None:
        import datetime as _dt
        self._lbl_date.setText(
            f"📅 日期：{_dt.datetime.now().strftime('%Y-%m-%d')}"
        )

    def _refresh_next_request(self) -> None:
        next_at = getattr(self._scheduler, "_next_run_at", 0.0)
        if next_at <= 0:
            self._lbl_next.setText("⏱️ 下次请求：-")
            return
        remain = next_at - time.time()
        if remain <= 0:
            self._lbl_next.setText("⏱️ 下次请求：即将")
            return
        mins = int(remain) // 60
        secs = int(remain) % 60
        self._lbl_next.setText(f"⏱️ 下次请求：{mins:02d}:{secs:02d}")

    def _refresh_login_status(self) -> None:
        cookies = self._cfg.get_cookies_dict()
        wr_skey = cookies.get("wr_skey", "")
        if wr_skey and len(str(wr_skey)) >= 8:
            self._lbl_login.setText("🔐 登录态：✅ 有效")
            self._lbl_login.setStyleSheet("color:#2d9d3c;font-size:13px;font-weight:600;")
        else:
            self._lbl_login.setText("🔐 登录态：❌ 失效")
            self._lbl_login.setStyleSheet("color:#d9534f;font-size:13px;font-weight:600;")

    def _refresh_reading_stats(self) -> None:
        stats = self._db.get_reading_stats()
        self._lbl_today.setText(f"今日：{self._fmt_hm(stats.get('today_seconds', 0))}")
        self._lbl_week.setText(f"本周：{self._fmt_hm(stats.get('weekly_seconds', 0))}")
        self._lbl_month.setText(f"本月：{self._fmt_hm(stats.get('monthly_seconds', 0))}")
        self._lbl_total.setText(f"总计：{self._fmt_hm(stats.get('total_seconds', 0))}")

    @staticmethod
    def _fmt_hm(seconds: Any) -> str:
        try:
            sec = int(seconds)
        except (TypeError, ValueError):
            return "-"
        if sec <= 0:
            return "0m"
        h = sec // 3600
        m = (sec % 3600) // 60
        if h > 0:
            return f"{h}h{m:02d}m"
        return f"{m}m"

    # ------------------------------------------------------------------
    # scheduler 信号槽
    # ------------------------------------------------------------------
    def _on_state_changed(self, text: str) -> None:
        self._lbl_status.setText(f"🔄 状态：{text}")

    def _on_log_emitted(self, msg: str) -> None:
        self._log_buffer.append(msg)

    def _on_progress(self, info: dict) -> None:
        target = info.get("target_minutes", 0)
        today_sec = info.get("today_seconds", 0)
        today_min = int(today_sec) // 60
        success = info.get("success", 0)
        fail = info.get("fail", 0)
        pct = (today_min * 100 // target) if target > 0 else 0
        self._lbl_target.setText(f"🎯 今日目标：{target} 分钟")
        self._lbl_done.setText(f"✅ 已完成：{today_min} 分钟 ({pct}%)")
        self._lbl_success_fail.setText(f"📊 成功：{success}  失败：{fail}")
        # 捕获当前书 bookId，供"刷新进度"按钮用
        book = info.get("book")
        if isinstance(book, dict) and book.get("bookId"):
            self._current_book_id = str(book.get("bookId"))

    def _on_book_progress(
        self, title: str, progress: int, chapter_title: str, chapter_count: int
    ) -> None:
        self._lbl_book_title.setText(f"书名：{title}")
        self._lbl_book_progress.setText(f"书籍进度：{progress}%")
        self._lbl_chapter.setText(f"当前章节：{chapter_title}")
        self._lbl_chapter_count.setText(f"本章已读：{chapter_count} 次")

    def _on_book_changed(self, title: str, progress: int) -> None:
        self._lbl_book_title.setText(f"书名：{title}")
        self._lbl_book_progress.setText(f"书籍进度：{progress}%")

    def _on_chapter_changed(self, title: str, uid: int) -> None:
        self._lbl_chapter.setText(f"当前章节：{title}（uid={uid}）")
        self._lbl_chapter_count.setText("本章已读：0 次")

    def _on_stats_updated(self, stats: dict) -> None:
        self._lbl_today.setText(f"今日：{self._fmt_hm(stats.get('today_seconds', 0))}")
        self._lbl_week.setText(f"本周：{self._fmt_hm(stats.get('weekly_seconds', 0))}")
        self._lbl_month.setText(f"本月：{self._fmt_hm(stats.get('monthly_seconds', 0))}")
        self._lbl_total.setText(f"总计：{self._fmt_hm(stats.get('total_seconds', 0))}")
        import datetime as _dt
        self._lbl_last_refresh.setText(
            f"最后刷新：{_dt.datetime.now().strftime('%H:%M:%S')}"
        )

    def _on_cookie_broken(self) -> None:
        self._refresh_login_status()
        self._btn_start.setEnabled(True)
        self._btn_pause.setEnabled(False)
        self._btn_stop.setEnabled(False)

    def _on_cookie_hard(self, reason: str) -> None:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.critical(
            self,
            "登录态硬失效",
            f"登录态已硬失效，必须重新扫码登录。\n\n原因：{reason}",
        )

    def _on_task_completed(self, minutes: int) -> None:
        self._lbl_status.setText(f"🔄 状态：已完成（{minutes} 分钟）")
        self._btn_start.setEnabled(True)
        self._btn_pause.setEnabled(False)
        self._btn_stop.setEnabled(False)

    # ------------------------------------------------------------------
    # 日志相关
    # ------------------------------------------------------------------
    def _on_log_bus(self, level: str, msg: str) -> None:
        """接收全局 LogBus 信号，写入日志缓冲。"""
        self._log_buffer.append(f"[{level}] {msg}")

    def _flush_log_buffer(self) -> None:
        if not self._log_buffer:
            return
        # 批量合并追加
        text = "\n".join(self._log_buffer)
        self._log_buffer.clear()
        self._log_view.appendPlainText(text)
        if self._log_auto_scroll:
            sb = self._log_view.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _on_toggle_scroll(self) -> None:
        self._log_auto_scroll = not self._btn_pause_scroll.isChecked()
        self._btn_pause_scroll.setText(
            "▶ 恢复滚动" if self._btn_pause_scroll.isChecked() else "⏸ 暂停滚动"
        )

    def _on_clear_log(self) -> None:
        self._log_view.clear()
        self._log_buffer.clear()

    # ------------------------------------------------------------------
    # 按钮槽
    # ------------------------------------------------------------------
    def _on_start(self) -> None:
        # 前置检查
        cookies = self._cfg.get_cookies_dict()
        wr_skey = cookies.get("wr_skey", "")
        if not wr_skey or len(str(wr_skey)) < 8:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self, "未登录", "请先点击「扫码登录」获取 Cookie。"
            )
            return
        shelf = self._db.get_shelf()
        if not shelf:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self, "无书架数据", "请先点击「获取书架/章节池/进度」拉取数据。"
            )
            return
        if self._scheduler.isRunning():
            self._scheduler.resume()
        else:
            self._scheduler.start()
        self._btn_start.setEnabled(False)
        self._btn_pause.setEnabled(True)
        self._btn_stop.setEnabled(True)

    def _on_pause(self) -> None:
        self._scheduler.pause()
        self._btn_start.setEnabled(True)
        self._btn_pause.setEnabled(False)

    def _on_stop(self) -> None:
        self._scheduler.stop()
        self._btn_start.setEnabled(True)
        self._btn_pause.setEnabled(False)
        self._btn_stop.setEnabled(False)

    def _on_cdp_login(self) -> None:
        from app.ui.cdp_login_dialog import CDPLoginDialog
        dlg = CDPLoginDialog(self._cfg, self._api, parent=self)
        dlg.login_success.connect(self._on_login_success)
        dlg.login_failed.connect(self._on_login_failed)
        dlg.exec()

    def _on_login_success(self, info: dict) -> None:
        self._refresh_login_status()
        self._notifier.notify_login_success(
            info.get("cookie_count", 0),
            info.get("wr_skey_length", 0),
        )
        # 自动触发获取书架/章节池/进度
        QTimer.singleShot(500, self._on_fetch_data)

    def _on_login_failed(self, reason: str) -> None:
        self._refresh_login_status()

    def _on_view_shelf(self) -> None:
        try:
            from app.ui.shelf_dialog import ShelfDialog
            dlg = ShelfDialog(self._db, parent=self)
            dlg.exec()
        except ImportError:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self, "提示", "书架查看对话框（Phase 9c 实现）")

    def _on_fetch_data(self) -> None:
        """异步获取书架/章节池/进度（QThread 子类 + 信号，线程安全）。"""
        self._btn_fetch.setEnabled(False)
        self._btn_fetch.setText("⏳ 获取中...")
        self._fetch_thread = _FetchThread(
            self._skill, self._db, parent=self
        )
        self._fetch_thread.log_msg.connect(self._on_log_emitted)
        self._fetch_thread.stats_updated.connect(self._on_stats_updated)
        self._fetch_thread.done.connect(self._fetch_done)
        self._fetch_thread.start()

    def _fetch_done(self) -> None:
        self._btn_fetch.setEnabled(True)
        self._btn_fetch.setText("🔄 获取数据")
        self._refresh_reading_stats()
        if hasattr(self, "_fetch_thread") and self._fetch_thread.isRunning():
            self._fetch_thread.quit()
            self._fetch_thread.wait(3000)

    def _on_refresh_stats(self) -> None:
        """手动刷新 Skill 阅读统计（后台线程）。"""
        self._btn_refresh_stats.setEnabled(False)
        self._btn_refresh_stats.setText("⏳ 刷新中...")
        self._skill_refresh_thread = _SkillRefreshThread(
            self._skill, self._db, mode="stats", parent=self
        )
        self._skill_refresh_thread.log_msg.connect(self._on_log_emitted)
        self._skill_refresh_thread.stats_updated.connect(self._on_stats_updated)
        self._skill_refresh_thread.done.connect(self._on_skill_refresh_done)
        self._skill_refresh_thread.start()

    def _on_refresh_progress(self) -> None:
        """手动刷新当前书的 Skill 进度（后台线程）。"""
        if not self._current_book_id:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(
                self, "无当前书籍",
                "当前没有正在阅读的书籍。\n请先点击「开始」让调度器选书。",
            )
            return
        self._btn_refresh_progress.setEnabled(False)
        self._btn_refresh_progress.setText("⏳ 刷新中...")
        self._skill_refresh_thread = _SkillRefreshThread(
            self._skill, self._db, mode="progress",
            book_id=self._current_book_id, parent=self,
        )
        self._skill_refresh_thread.log_msg.connect(self._on_log_emitted)
        self._skill_refresh_thread.book_progress_updated.connect(
            self._on_skill_book_progress
        )
        self._skill_refresh_thread.done.connect(self._on_skill_refresh_done)
        self._skill_refresh_thread.start()

    def _on_skill_book_progress(self, book_id: str, percent: int) -> None:
        """Skill 手动刷新进度回调 → 更新 UI。"""
        self._lbl_book_progress.setText(f"书籍进度：{percent}%（Skill 覆盖）")

    def _on_skill_refresh_done(self) -> None:
        """Skill 刷新线程完成 → 恢复按钮。"""
        self._btn_refresh_stats.setEnabled(True)
        self._btn_refresh_stats.setText("📊 刷新统计")
        self._btn_refresh_progress.setEnabled(True)
        self._btn_refresh_progress.setText("📖 刷新进度")
        if self._skill_refresh_thread and self._skill_refresh_thread.isRunning():
            self._skill_refresh_thread.quit()
            self._skill_refresh_thread.wait(3000)

    def _on_open_settings(self) -> None:
        if self._main_window:
            self._main_window.show_settings_page()
