"""状态 / 日志页：今日进度卡 + 官方阅读时长卡 + 控制按钮 + 实时日志。"""
from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Any

from PySide6.QtCore import QTimer, Qt, Signal, QUrl, QElapsedTimer
from PySide6.QtGui import QTextCharFormat, QColor, QFont, QTextCursor, QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.config import ConfigStore
from app.core.scheduler import ReadingScheduler
from app.core.weread_api import WeReadApi
from app.utils.logger import log_bus


def _card_style() -> str:
    return (
        "background:#fff;border:1px solid #e5eaf2;border-radius:14px;"
    )


class StatusPage(QWidget):
    _verify_result = Signal(bool)
    _verify_finished = Signal()
    _reading_summary_ready = Signal(dict)  # 后台线程拿到官方统计 → UI 线程更新卡
    _health_check_done = Signal(bool, str)  # ok, message
    # 跨页：让 MainWindow 把"在登录浏览器打开某本书"路由给 LoginPage
    request_navigate_reader = Signal(str)  # URL
    # 跨页：通知 LoginPage / MainWindow「用户希望登录页浏览器回到当前会话登录态」
    request_restore_browser_session = Signal()

    def __init__(
        self,
        scheduler: ReadingScheduler,
        api: WeReadApi,
        config: ConfigStore | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._scheduler = scheduler
        self._api = api
        self._cfg = config or ConfigStore()
        # —— 倒计时：_next_run_at=时间戳；_pending_interval=原始秒，UI 文字兜底
        self._next_run_at: float | None = None
        self._pending_interval: float | None = None
        # —— 官方阅读时长缓存：3 分钟内不重复拉（避免把接口打爆）
        self._summary_cache_ts: float = 0.0
        self._summary_cache: dict | None = None
        self._summary_worker_running = False
        # —— 健康检查互斥：UI 发起 + 定时触发都不能并发
        self._health_worker_running = False
        # —— Skill 请求"主线程本地看门狗"：不依赖 scheduler 线程，22s 内一定强制解锁按钮。
        #    （scheduler 内部线程、信号链、ThreadPoolExecutor 全部失效时，这里是最后一道防线）
        self._skill_local_watchdog: QTimer | None = None

        self._verify_result.connect(self._update_cookie_status)
        self._verify_finished.connect(self._finish_verify)
        self._reading_summary_ready.connect(self._apply_reading_summary)
        self._health_check_done.connect(self._apply_health_check)

        # —— 日志窗口批处理（丝滑流畅关键 v3）：
        #    历史上每次 _append_log 都做 cursor.movePosition(End) + insertText + sb.setValue，
        #    Skill 批量发 / 调度器每秒进度时会把 GUI 线程打成筛子（信号风暴）。
        #    改造：消息只入队，固定 50ms 节拍一次 flush 批量追加 + 滚底，
        #    + 连续相同的 message 合并为 "×N"，日志窗口从"每秒刷 20 次"降到"每 50ms 刷 1 次"。
        self._log_pending: list[tuple[str, str]] = []  # [(level_upper, text)]
        self._log_last_key: tuple[str, str] | None = None
        self._log_last_count: int = 0
        self._log_flush_timer = QTimer(self)
        self._log_flush_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._log_flush_timer.timeout.connect(self._flush_log_buffer)
        self._log_flush_timer.start(50)
        self._log_level_formats: dict[str, QTextCharFormat] = {}
        self._log_monospace_font = QFont()
        self._log_monospace_font.setStyleHint(QFont.StyleHint.Monospace)
        self._log_monospace_font.setFamilies(["Consolas", "Microsoft YaHei UI"])

        self._build_ui()
        self._hook_signals()
        self._reset_progress()

    # ---------------- UI ----------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        header = QLabel("📊 阅读状态")
        header.setStyleSheet("font-size:18px;font-weight:600;color:#2f3b52;")
        root.addWidget(header)

        # ----- 进度卡片 -----
        card = QFrame()
        card.setStyleSheet(_card_style())
        cv = QVBoxLayout(card)
        cv.setContentsMargins(20, 18, 20, 18)
        cv.setSpacing(12)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self._lbl_date = QLabel("——")
        self._lbl_date.setStyleSheet("color:#5f6c85;font-size:13px;")
        self._lbl_state = QLabel("待机中")
        self._lbl_state.setStyleSheet(
            "color:#fff;background:#b4c0d4;border-radius:999px;"
            "padding:3px 12px;font-size:12px;font-weight:600;"
        )
        # 状态徽章：最小宽度 + 不可压缩（它是圆角胶囊，被压窄会变形）
        self._lbl_state.setMinimumWidth(64)
        self._lbl_state.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._lbl_cookie = QLabel("登录态：未检测")
        self._lbl_cookie.setStyleSheet("color:#7a879f;font-size:12px;")
        # 登录态标签：允许 Elide（窗口极小时显示"登录态：✅…"），不换行
        self._lbl_cookie.setWordWrap(False)
        try:
            self._lbl_cookie.setTextElideMode(Qt.TextElideMode.ElideRight)
        except Exception:  # noqa: BLE001
            pass
        self._lbl_cookie.setMinimumWidth(120)
        row1.addWidget(self._lbl_date)
        row1.addWidget(self._lbl_state)
        row1.addStretch(1)
        row1.addWidget(self._lbl_cookie, 1)  # stretch=1 → 让它优先压缩（带 Elide）
        cv.addLayout(row1)

        self._lbl_target = QLabel("今日目标：-  /  已完成：-")
        self._lbl_target.setStyleSheet("font-size:15px;font-weight:600;color:#2f3b52;")
        cv.addWidget(self._lbl_target)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFormat("%p% (%v / %m 分钟)")
        cv.addWidget(self._progress)

        self._lbl_detail = QLabel("成功：-  失败：-  下次请求：待机中")
        self._lbl_detail.setStyleSheet("color:#7a879f;font-size:12px;")
        cv.addWidget(self._lbl_detail)

        # 控制按钮
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self._btn_start = QPushButton("▶ 开始阅读")
        self._btn_pause = QPushButton("⏸ 暂停")
        self._btn_stop = QPushButton("⏹ 停止")
        self._btn_verify = QPushButton("🔍 检测登录态")
        self._btn_start.setStyleSheet(self._primary_btn())
        for b in (self._btn_pause, self._btn_stop, self._btn_verify):
            b.setStyleSheet(self._secondary_btn())
        self._btn_pause.setEnabled(False)
        self._btn_stop.setEnabled(False)
        self._btn_start.clicked.connect(self._on_start)
        self._btn_pause.clicked.connect(self._on_pause)
        self._btn_stop.clicked.connect(self._on_stop)
        self._btn_verify.clicked.connect(self._on_verify)

        btn_row.addWidget(self._btn_start)
        btn_row.addWidget(self._btn_pause)
        btn_row.addWidget(self._btn_stop)
        btn_row.addStretch(1)
        btn_row.addWidget(self._btn_verify)
        cv.addLayout(btn_row)

        root.addWidget(card)

        # ----- 当前阅读本卡片（书名 / URL / 进度 / 手动设置 / 去浏览器打开 / 复制链接）-----
        card_book = QFrame()
        card_book.setStyleSheet(_card_style())
        cbv = QVBoxLayout(card_book)
        cbv.setContentsMargins(20, 16, 20, 16)
        cbv.setSpacing(10)

        head_b = QHBoxLayout()
        head_b.setSpacing(8)  # 明确控件间距，避免小窗口时被压成负值
        title_b = QLabel("📖 正在读的书")
        title_b.setStyleSheet("font-size:14px;font-weight:600;color:#2f3b52;")
        # title 不需要伸缩：它文本短，作为锚点
        title_b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self._lbl_book_source = QLabel("尚未选择书籍")
        self._lbl_book_source.setStyleSheet("color:#93a0b8;font-size:12px;")
        # —— 关键修复（小窗口挤压 v2）——
        #   1) 最小宽度降低到 100（不是 180），让它能先收缩；
        #   2) stretch >0，把"多余空间先让给标签 + 不足时先压缩标签"；
        #   3) 文字省略号（ElideRight）：窄于文本宽度时显示"扫码浏…"不换行不挤控件；
        #   4) 最大宽度上限：不要吃掉按钮的空间（按钮是必须完整显示的）。
        self._lbl_book_source.setMinimumWidth(100)
        self._lbl_book_source.setMaximumWidth(360)
        self._lbl_book_source.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._lbl_book_source.setWordWrap(False)
        try:
            self._lbl_book_source.setTextElideMode(Qt.TextElideMode.ElideRight)
        except Exception:  # noqa: BLE001
            pass  # 老 PySide 可能没这个 API，但 setWordWrap(False) 已经能避免换行

        # ====== 策略一：锁状态标签（上锁/未锁一眼可见）======
        self._lbl_book_lock = QLabel("🔓 未锁定（自动同步可覆盖）")
        self._lbl_book_lock.setStyleSheet(
            "color:#93a0b8;font-size:12px;background:#f1f5fb;"
            "padding:3px 10px;border-radius:10px;"
        )
        # —— 同 source 标签：最小宽度降低 + 允许收缩（最大 240）
        self._lbl_book_lock.setMinimumWidth(150)
        self._lbl_book_lock.setMaximumWidth(240)
        self._lbl_book_lock.setWordWrap(False)
        try:
            self._lbl_book_lock.setTextElideMode(Qt.TextElideMode.ElideRight)
        except Exception:  # noqa: BLE001
            pass

        btn_pick_b = QPushButton("从书架挑选")
        btn_open_reader = QPushButton("扫码浏览器打开")  # 精简文字，避免按钮本身过宽
        btn_copy_url = QPushButton("复制链接")
        btn_clear_b = QPushButton("清除")
        btn_pick_b.setStyleSheet(self._secondary_btn())
        btn_open_reader.setStyleSheet(self._secondary_btn())
        btn_copy_url.setStyleSheet(self._secondary_btn())
        btn_clear_b.setStyleSheet(self._secondary_btn())
        for b in (btn_pick_b, btn_open_reader, btn_copy_url, btn_clear_b):
            # —— 按钮最小高度 30，固定不可压缩（按钮是"必须完整点击"的控件）
            b.setMinimumHeight(30)
            b.setMaximumHeight(30)
            b.setMinimumWidth(80)
            # —— 关键：按钮 SizePolicy 改成 Fixed，小窗口不会被压成"窄条"
            b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        btn_open_reader.setMinimumWidth(130)  # 精简文字后按钮可以更窄
        btn_pick_b.clicked.connect(self._on_pick_book_from_shelf)
        btn_open_reader.clicked.connect(self._on_open_book_in_browser)
        btn_copy_url.clicked.connect(self._on_copy_book_url)
        btn_clear_b.clicked.connect(self._on_clear_current_book)

        head_b.addWidget(title_b)
        # source 标签 + 锁标签：给 stretch=1（优先伸缩它们，保护按钮不被压）
        head_b.addWidget(self._lbl_book_source, 1)
        head_b.addWidget(self._lbl_book_lock, 0)
        head_b.addStretch(1)
        head_b.addWidget(btn_pick_b)
        head_b.addWidget(btn_open_reader)
        head_b.addWidget(btn_copy_url)
        head_b.addWidget(btn_clear_b)
        cbv.addLayout(head_b)

        # 书名 + 作者
        self._lbl_book_title = QLabel("未选择书籍：先登录 → 从扫码登录页打开一本书 → 系统会自动同步；\n或在下方粘贴微信读书「书本阅读页」URL，再点「更新到阅读状态」。")
        self._lbl_book_title.setStyleSheet(
            "font-size:15px;font-weight:600;color:#2f3b52;background:#f8fafc;"
            "border-radius:10px;padding:12px 14px;"
        )
        self._lbl_book_title.setWordWrap(True)
        cbv.addWidget(self._lbl_book_title)

        # 书的 URL + 提交按钮
        row_url = QHBoxLayout()
        row_url.setSpacing(10)
        self._edit_book_url = QLineEdit()
        self._edit_book_url.setPlaceholderText(
            "粘贴书本阅读页链接，例如：https://weread.qq.com/web/reader/wb36d322f07186022636daa5e?kecc32f3013eccbc87e4b62e  "
            "（在扫码登录浏览器里打开一本书 → 复制地址栏到这里）"
        )
        self._edit_book_url.setMinimumHeight(34)
        self._edit_book_url.setStyleSheet(self._lineedit_qss())
        btn_apply_url = QPushButton("更新到阅读状态")
        btn_apply_url.setStyleSheet(self._primary_btn())
        btn_apply_url.setMinimumWidth(130)
        btn_apply_url.setMinimumHeight(34)
        btn_apply_url.clicked.connect(self._on_apply_book_url)
        row_url.addWidget(self._edit_book_url, 1)
        row_url.addWidget(btn_apply_url)
        cbv.addLayout(row_url)

        # 进度条（0 ~ 100%，对应官方书架 readingProgress/10000）
        prog_row = QHBoxLayout()
        prog_lbl = QLabel("书籍阅读进度：")
        prog_lbl.setStyleSheet("color:#5f6c85;font-size:12px;")
        self._book_progress = QProgressBar()
        self._book_progress.setRange(0, 10000)
        self._book_progress.setValue(0)
        self._book_progress.setFormat("—")
        self._book_progress.setTextVisible(True)
        self._book_progress.setMinimumHeight(26)
        self._book_progress.setMinimumWidth(240)
        prog_row.addWidget(prog_lbl)
        prog_row.addWidget(self._book_progress, 1)
        cbv.addLayout(prog_row)

        # 最近一次上报时间
        self._lbl_last_report = QLabel("最近一次上报：尚未开始")
        self._lbl_last_report.setStyleSheet("color:#93a0b8;font-size:12px;")
        cbv.addWidget(self._lbl_last_report)

        self._current_book_cache: dict | None = None
        root.addWidget(card_book)

        # ----- 官方今日阅读时长卡（新增：定期返回微信读书官方信息）-----
        card2 = QFrame()
        card2.setStyleSheet(_card_style())
        c2v = QVBoxLayout(card2)
        c2v.setContentsMargins(20, 16, 20, 16)
        c2v.setSpacing(10)

        head2 = QHBoxLayout()
        head2.setSpacing(8)
        title2 = QLabel("⏱️ 微信读书 · 官方阅读数据")
        title2.setStyleSheet("font-size:14px;font-weight:600;color:#2f3b52;")
        title2.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)  # 锚点不伸缩
        self._lbl_summary_refresh = QLabel("尚未更新")
        self._lbl_summary_refresh.setStyleSheet("color:#93a0b8;font-size:12px;")
        self._lbl_summary_refresh.setWordWrap(False)
        self._lbl_summary_refresh.setMinimumWidth(80)
        try:
            self._lbl_summary_refresh.setTextElideMode(Qt.TextElideMode.ElideRight)
        except Exception:  # noqa: BLE001
            pass
        self._lbl_skill_status = QLabel("")
        self._lbl_skill_status.setStyleSheet("color:#2d6cdf;font-size:12px;")
        # ===== v2 关键：Skill 状态标签取消 WordWrap，改成 Elide + 最大宽度 =====
        #   之前 setWordWrap(True) 在小窗口会把头部撑高成 3-4 行，
        #   把下方 4 个时长方块直接挤出可视区（用户截图小窗口能看到的变形根源之一）。
        self._lbl_skill_status.setWordWrap(False)
        self._lbl_skill_status.setMaximumWidth(480)
        self._lbl_skill_status.setMinimumWidth(120)
        try:
            self._lbl_skill_status.setTextElideMode(Qt.TextElideMode.ElideRight)
        except Exception:  # noqa: BLE001
            pass
        self._btn_refresh_summary = QPushButton("🔄 立即刷新（Skill 优先）")
        self._btn_refresh_summary.setStyleSheet(self._secondary_btn())
        self._btn_refresh_summary.clicked.connect(lambda: self._refresh_reading_summary(force=True))
        self._btn_refresh_summary.setMinimumHeight(30)
        self._btn_refresh_summary.setMaximumHeight(30)
        self._btn_refresh_summary.setMinimumWidth(170)
        self._btn_refresh_summary.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        head2.addWidget(title2)
        head2.addWidget(self._lbl_summary_refresh, 0)
        head2.addWidget(self._lbl_skill_status, 1)  # stretch=1 → 多余空间给它，不足时它先 Elide
        head2.addSpacing(6)
        head2.addWidget(self._btn_refresh_summary)
        c2v.addLayout(head2)

        # 4 个方块：今日 / 本周 / 本月 / 累计
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        # ===== v2：4 列均分 + 每列最小宽度 120 =====
        #   不设 stretch 的话，Qt 在小窗口会按"内容自然宽度"分配（"累计"字少→被压得很窄），
        #   导致 4 格宽度参差不齐；setColumnStretch(全部 =1) 让它们严格 1:1:1:1。
        for col_i in range(4):
            grid.setColumnStretch(col_i, 1)
            grid.setColumnMinimumWidth(col_i, 110)
        self._sum_today_val = QLabel("—")
        self._sum_today_key = QLabel("今日已读")
        self._sum_week_val = QLabel("—")
        self._sum_week_key = QLabel("本周")
        self._sum_month_val = QLabel("—")
        self._sum_month_key = QLabel("本月")
        self._sum_total_val = QLabel("—")
        self._sum_total_key = QLabel("累计")
        for val in (
            self._sum_today_val, self._sum_week_val,
            self._sum_month_val, self._sum_total_val,
        ):
            val.setStyleSheet(
                "font-size:22px;font-weight:700;color:#2d6cdf;"
                "background:#f3f7ff;border-radius:10px;padding:10px 6px;"
            )
            val.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for key in (
            self._sum_today_key, self._sum_week_key,
            self._sum_month_key, self._sum_total_key,
        ):
            key.setStyleSheet("color:#7a879f;font-size:12px;")
            key.setAlignment(Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(self._sum_today_val, 0, 0)
        grid.addWidget(self._sum_week_val, 0, 1)
        grid.addWidget(self._sum_month_val, 0, 2)
        grid.addWidget(self._sum_total_val, 0, 3)
        grid.addWidget(self._sum_today_key, 1, 0)
        grid.addWidget(self._sum_week_key, 1, 1)
        grid.addWidget(self._sum_month_key, 1, 2)
        grid.addWidget(self._sum_total_key, 1, 3)
        c2v.addLayout(grid)

        self._lbl_summary_tip = QLabel("登录后自动同步；每 3 分钟刷新一次。点击「立即刷新」可强制获取。")
        self._lbl_summary_tip.setStyleSheet("color:#93a0b8;font-size:12px;")
        self._lbl_summary_tip.setWordWrap(True)
        c2v.addWidget(self._lbl_summary_tip)

        root.addWidget(card2)

        # ----- 日志 -----
        log_header = QLabel("📝 运行日志")
        log_header.setStyleSheet("font-size:15px;font-weight:600;color:#2f3b52;margin-top:6px;")
        root.addWidget(log_header)

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(2000)
        # —— 丝滑化：关掉编辑期间的"自动语法/重绘"信号（readonly 本就不需要）
        #    并固定文档宽度/Undo 深度，避免 GUI 线程每次 append 都走全文 rewrap
        self._log_view.setUndoRedoEnabled(False)
        self._log_view.setWordWrapMode(self._log_view.wordWrapMode())  # 保持默认（不 wrap 更快）
        self._log_view.document().setDocumentMargin(6)
        self._log_view.setMinimumHeight(180)
        self._log_view.setStyleSheet(
            "QPlainTextEdit{background:#ffffff;"
            "border:1px solid #e5eaf2;border-radius:10px;"
            "padding:6px 10px;color:#2f3b52;font-size:12.5px;}"
        )
        root.addWidget(self._log_view, 1)

        # 启动时刷新时间
        self._tick_date()

    @staticmethod
    def _primary_btn() -> str:
        return (
            "QPushButton{background:#2d6cdf;color:#fff;border:none;border-radius:8px;"
            "padding:8px 20px;font-size:13px;font-weight:600;}"
            "QPushButton:hover{background:#265bc0;}"
            "QPushButton:disabled{background:#b9c9eb;color:#fff;}"
        )

    @staticmethod
    def _secondary_btn() -> str:
        return (
            "QPushButton{background:#eef2f8;color:#2f3b52;border:none;border-radius:8px;"
            "padding:8px 16px;font-size:13px;}"
            "QPushButton:hover{background:#e2e8f4;}"
            "QPushButton:disabled{color:#aab3c5;background:#f3f5fb;}"
        )

    @staticmethod
    def _lineedit_qss() -> str:
        return (
            "QLineEdit{background:#ffffff;color:#2f3b52;border:1px solid #d5dbe6;"
            "border-radius:8px;padding:6px 10px;font-size:13px;}"
            "QLineEdit:focus{border:1px solid #2d6cdf;}"
            "QLineEdit:disabled{background:#f3f5fb;color:#aab3c5;}"
        )

    # ---------------- Signals hook ----------------
    def _hook_signals(self) -> None:
        self._scheduler.state_changed.connect(self._on_state_changed)
        self._scheduler.progress.connect(self._on_progress)
        self._scheduler.log.connect(lambda msg: self._append_log(msg, "INFO"))
        self._scheduler.cookie_broken.connect(self._on_cookie_broken)
        self._scheduler.task_completed.connect(self._on_task_completed)
        self._scheduler.cookie_fail_reported.connect(self._on_cookie_broken)
        # —— Skill 同步结果（每 N 次成功读 或 手动刷新 Skill）
        self._scheduler.skill_sync_completed.connect(self._apply_skill_sync)

        self._api.message.connect(lambda m: self._append_log(m, "INFO"))
        self._api.warning.connect(lambda m: self._append_log(m, "WARN"))
        self._api.error.connect(lambda m: self._append_log(m, "ERROR"))
        self._api.reading_summary.connect(self._reading_summary_ready.emit)  # 多线程安全：转一次 UI 信号
        # 当前书籍（API 线程 -> Signal -> UI 线程）
        self._api.current_book_changed.connect(self._apply_current_book)

        # 全局日志信号
        log_bus.log_emitted.connect(self._on_global_log)

        # 日期定时刷新
        self._date_timer = QTimer(self)
        self._date_timer.timeout.connect(self._tick_date)
        self._date_timer.start(30_000)

        # —— 倒计时 tick（1s 刷新"下次请求剩余 X 秒"显示）
        self._countdown_timer = QTimer(self)
        self._countdown_timer.timeout.connect(self._tick_countdown)
        self._countdown_timer.start(1000)

        # —— 官方阅读时长：每 3 分钟尝试一次；启动后 12 秒先跑 1 次
        self._summary_timer = QTimer(self)
        self._summary_timer.timeout.connect(lambda: self._refresh_reading_summary(force=False))
        self._summary_timer.start(3 * 60 * 1000)
        QTimer.singleShot(12 * 1000, lambda: self._refresh_reading_summary(force=False))

    # ---------------- handlers ----------------
    def _tick_date(self) -> None:
        now = dt.datetime.now()
        self._lbl_date.setText(f"📅 {now.date().isoformat()}  {now.strftime('%H:%M')}")

    def _fmt_remain(self, secs: float) -> str:
        secs = max(0, int(round(secs)))
        if secs >= 60:
            m, s = divmod(secs, 60)
            return f"{m:>2}分{s:0>2}秒"
        return f"{secs:>2}秒"

    def _tick_countdown(self) -> None:
        """每秒跑一次：把"下次请求：还剩 XX 秒"带倒计时文字打在 _lbl_detail。

        为避免"任务态/完成态"时把倒计时抹掉：当调度器不是阅读中时，也保留最后一次的倒计时展示
        （但状态文字会在 _on_state_changed / _reset_progress 里覆盖）。
        """
        # 先把官方阅读卡的"更新于 xx 分钟前"刷新一下
        if self._summary_cache_ts > 0:
            ago = int(time.time() - self._summary_cache_ts)
            if ago < 60:
                self._lbl_summary_refresh.setText(f"更新于 {ago}s 前")
            else:
                self._lbl_summary_refresh.setText(f"更新于 {ago // 60} 分钟前")

        detail = getattr(self, "_detail_snapshot", None)
        if detail is None:
            return

        # 计算剩余秒数
        remain: float | None = None
        if isinstance(self._next_run_at, (int, float)):
            remain = float(self._next_run_at) - time.time()
            if remain < 0:
                remain = 0.0

        state = str(detail.get("state") or "")
        success = detail.get("success", 0)
        fail = detail.get("fail", 0)
        interval = detail.get("current_interval", "-")

        if "已暂停" in state:
            next_text = "下次请求：⏸ 已暂停（恢复后计时）"
        elif "已停止" in state or "待机中" in state or "空闲中" in state or "已完成" in state:
            next_text = "下次请求：待机中"
        elif "登录态失效" in state:
            next_text = "下次请求：⏸ 登录态失效，等待扫码"
        elif remain is not None:
            # 倒计时：mm:ss
            rem_s = max(0, int(round(remain)))
            if rem_s >= 60:
                mm, ss = divmod(rem_s, 60)
                mm_text = f"{mm:>2}:{ss:0>2d}"
            else:
                mm_text = f"   {rem_s:>2}s"
            dots = "..." if (rem_s % 2 == 0) else " . "
            next_text = f"下次请求：还剩 {mm_text} （本轮计划 {interval}s）{dots}"
        else:
            next_text = f"下次请求：约 {interval}s"

        self._lbl_detail.setText(
            f"成功：{success}  失败：{fail}  {next_text}"
        )

    def _reset_progress(self) -> None:
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._lbl_target.setText("今日目标：-  /  已完成：-")
        self._lbl_detail.setText("成功：-  失败：-  下次请求：待机中")
        self._detail_snapshot = {
            "success": 0, "fail": 0, "current_interval": "-", "state": "待机中",
        }
        self._next_run_at = None
        self._pending_interval = None

    def _on_state_changed(self, state: str) -> None:
        self._lbl_state.setText(state)
        if "阅读中" in state or "初始化中" in state:
            self._lbl_state.setStyleSheet(
                "color:#fff;background:#2d6cdf;border-radius:999px;"
                "padding:3px 12px;font-size:12px;font-weight:600;"
            )
            self._btn_start.setEnabled(False)
            self._btn_pause.setEnabled(True)
            self._btn_stop.setEnabled(True)
        elif "已暂停" in state:
            self._lbl_state.setStyleSheet(
                "color:#fff;background:#f0a020;border-radius:999px;"
                "padding:3px 12px;font-size:12px;font-weight:600;"
            )
            self._btn_pause.setEnabled(False)
            self._btn_start.setEnabled(True)
        elif "已停止" in state or "空闲中" in state or "已完成" in state:
            if "已完成" in state:
                self._lbl_state.setStyleSheet(
                    "color:#fff;background:#2eae5d;border-radius:999px;"
                    "padding:3px 12px;font-size:12px;font-weight:600;"
                )
            elif "已停止" in state:
                self._lbl_state.setStyleSheet(
                    "color:#fff;background:#9098aa;border-radius:999px;"
                    "padding:3px 12px;font-size:12px;font-weight:600;"
                )
            else:
                self._lbl_state.setStyleSheet(
                    "color:#fff;background:#b4c0d4;border-radius:999px;"
                    "padding:3px 12px;font-size:12px;font-weight:600;"
                )
            self._btn_start.setEnabled(True)
            self._btn_pause.setEnabled(False)
            self._btn_stop.setEnabled(False)
        elif "登录态失效" in state:
            self._lbl_state.setStyleSheet(
                "color:#fff;background:#e25454;border-radius:999px;"
                "padding:3px 12px;font-size:12px;font-weight:600;"
            )
            self._btn_start.setEnabled(False)
            self._btn_pause.setEnabled(False)
            self._btn_stop.setEnabled(False)

    def _on_progress(self, info: dict) -> None:
        target = int(info.get("target_minutes", 0) or 0)
        done = int(info.get("started_minutes", 0) or 0)
        if target <= 0:
            self._progress.setRange(0, 100)
            self._progress.setValue(0)
        else:
            self._progress.setRange(0, target)
            self._progress.setValue(min(done, target))
        self._lbl_target.setText(
            f"今日目标：{target // 60}h{target % 60:02d}m  "
            f"/  已完成：{done // 60}h{done % 60:02d}m"
        )
        interval = info.get("current_interval", "-")
        # 更新倒计时基准（时间戳）
        next_ts = info.get("next_run_at")
        if isinstance(next_ts, (int, float)) and float(next_ts) > 0:
            self._next_run_at = float(next_ts)
        elif isinstance(interval, (int, float)):
            # 兼容老版：没有时间戳就按"收到 progress 的时刻 + interval"估算
            self._next_run_at = time.time() + float(interval)
        if isinstance(interval, (int, float)):
            self._pending_interval = float(interval)

        state_text = info.get("state") if isinstance(info.get("state"), str) else self._lbl_state.text()
        self._detail_snapshot = {
            "success": int(info.get("success", 0) or 0),
            "fail": int(info.get("fail", 0) or 0),
            "current_interval": interval,
            "state": state_text,
        }

        # —— 从调度器 progress 里拿 book 快照 + 最近一次上报时间戳 立即灌到当前书卡
        book = info.get("book")
        if isinstance(book, dict):
            self._apply_current_book(book)
        last_ts = info.get("last_report_ts") or 0
        try:
            ts = int(last_ts) if last_ts else 0
        except (TypeError, ValueError):
            ts = 0
        if ts > 0:
            delta = max(0, int(time.time() - ts))
            if delta < 60:
                self._lbl_last_report.setText(f"最近一次上报：{delta}s 前（成功次数累计 {info.get('success',0)}）")
            else:
                self._lbl_last_report.setText(
                    f"最近一次上报：{delta//60} 分钟前（成功次数累计 {info.get('success',0)}）"
                )

        # 立即刷一次（否则等下一秒才变）
        self._tick_countdown()
        self._tick_date()

    # ---------------- 当前阅读本（书名 / URL / 进度） ----------------
    def _refresh_book_lock_label(self) -> None:
        """根据 API 的 _manual_book_locked 刷新「🔓/🔒」锁状态标签。"""
        try:
            locked = bool(self._api.is_book_locked())
        except Exception:  # noqa: BLE001
            locked = False
        if locked:
            self._lbl_book_lock.setText("🔒 已锁定（自动同步不会覆盖你选的书）")
            self._lbl_book_lock.setStyleSheet(
                "color:#c77400;font-size:12px;background:#fff6df;border:1px solid #ffe2a7;"
                "padding:3px 10px;border-radius:10px;font-weight:600;"
            )
        else:
            self._lbl_book_lock.setText("🔓 未锁定（自动同步可覆盖）")
            self._lbl_book_lock.setStyleSheet(
                "color:#93a0b8;font-size:12px;background:#f1f5fb;"
                "padding:3px 10px;border-radius:10px;"
            )

    def _apply_current_book(self, book: dict | None) -> None:
        # 先刷锁状态（无论 book 是否为空，锁状态都要实时）
        self._refresh_book_lock_label()
        if not isinstance(book, dict):
            return
        self._current_book_cache = dict(book)
        title = str(book.get("title") or "未命名书籍").strip()
        author = str(book.get("author") or "").strip()
        url = str(book.get("url") or "").strip()
        source = str(book.get("source") or "manual").strip()
        progress_raw = book.get("progress")
        progress_pct: float | None = None
        if isinstance(progress_raw, (int, float)) and not isinstance(progress_raw, bool):
            progress_pct = max(0.0, min(1.0, float(progress_raw)))
        book_id = str(book.get("book_id") or "").strip()
        reader_id = str(book.get("book_reader_id") or "").strip()
        chapter_id = str(book.get("chapter_id") or "").strip()

        # ====== 关键修复：截图里的"未选择书籍 + 作者" ======
        #   标题 fallback 优先级：
        #     1) 传入 title（非空且不是"未选择/未命名/默认占位"）
        #     2) reader 页面 / web storage 里能拿到 chapter 或 book name（稍后在 login_page 的 JS 兜底会补）
        #     3) reader_id：如果是 wb<hex> 用 bookId 前 8 位做《未命名书籍（ab12…）》
        #     4) 最后才默认"未选择书籍"
        #   作者：如果 title 还是占位但 author 有，就把 author 单独放 tips；不渲染"《未选择书籍》· 邓启耀"
        PLACEHOLDER_TITLES = {"未选择书籍", "未命名书籍", "", "未命名"}
        title_is_placeholder = (
            (not title)
            or title in PLACEHOLDER_TITLES
            or title.startswith("未命名书籍（")
            or title.startswith("未选择书籍")
        )
        # 如果有 reader_id 但 title 还是占位，拼一个"不那么空"的标题
        if title_is_placeholder and reader_id:
            short = reader_id[:10]
            title = f"正在阅读（ID：{short}…）"
            title_is_placeholder = (
                False  # 有个真实提示就不要再显示"未选择书籍"了
            )
        if title_is_placeholder and not author:
            title_label = "未选择书籍：先登录 → 从扫码登录页打开一本书 → 系统会自动同步；\n或在下方粘贴微信读书「书本阅读页」URL，再点「更新到阅读状态」。"
        elif title_is_placeholder and author:
            # title 空但 author 有（典型：旧 shelf 源 booklist 没 title 但 author 能拿到）
            title_label = f"已同步作者：{author}\n（书名仍未解析：建议在扫码登录浏览器打开这本书，或粘贴阅读页 URL → 点「更新到阅读状态」）"
        else:
            title_label = f"《{title}》"
            if author:
                title_label += f" · {author}"
        self._lbl_book_title.setText(title_label)
        # 若 URL 有值，填入输入框（方便用户再编辑 / 复制）
        if url and (self._edit_book_url.text().strip() != url):
            self._edit_book_url.setText(url)
        # 来源标签：先 compact 去重 → 再按 "段" 翻译中文 → 最后 setText（UI 侧再保险一层）
        from app.core.weread_api import WeReadApi
        source_compact = WeReadApi.compact_source(source) or source
        source_map = {
            "shelf_booklist": "自动·书架",
            "reader_url": "浏览器URL",
            "manual": "手动粘贴链接",
            "login_browser_nav": "扫码浏览器导航",
            "login_page_storage": "扫码页storage",
            "shelf_pick_manual": "书架手动挑选",
            "skill_book_info": "Skill同步",
            "skill_sync": "Skill同步",
            "cookie_shelf_progress": "Cookie书架进度",
            "clear": "手动清除",
        }
        # 逐段翻译（compact 后仍可能是 "a+b" 格式，按 "+" 切分逐段 map）
        segs = [s for s in source_compact.split("+") if s]
        translated: list[str] = []
        for seg in segs:
            translated.append(source_map.get(seg, seg))
        source_display = "+".join(translated) or "未知来源"
        # UI 硬上限：超过 60 字符就尾部截断（配合 Elide 显示省略号）
        if len(source_display) > 60:
            source_display = source_display[:57] + "…"
        self._lbl_book_source.setText(source_display)

        # 书籍进度条：0~10000 映射 0~100.00%
        if progress_pct is None:
            self._book_progress.setValue(0)
            self._book_progress.setFormat("—")
        else:
            v = int(round(progress_pct * 10000))
            v = max(0, min(10000, v))
            self._book_progress.setValue(v)
            self._book_progress.setFormat(f"{progress_pct * 100:.2f}%")
        # 提示：还没有 url 就告诉用户去"扫码登录浏览器"里打开再同步
        # —— v2：不直接拼到 setText 里（那会让 source 标签再次变长挤爆布局），
        #    改设 ToolTip：鼠标悬停时显示详细说明；并用标签右下角的小图标提示用户。
        if not url:
            current = self._lbl_book_source.text() or ""
            self._lbl_book_source.setToolTip(
                "当前书还没有可打开的 URL：请先去「🔐 扫码登录」页的内置浏览器打开一本书的阅读页，\n"
                "或在下方输入框粘贴微信读书 reader URL 后点击「更新到阅读状态」。"
            )
            # 只用后缀小标识，不写长文字（避免标签宽度膨胀）
            suffix = "·⚠️无URL"
            # 保证总字符 ≤ 60（防止小窗口叠加超长）
            if len(current) + len(suffix) <= 62:
                self._lbl_book_source.setText(current + " " + suffix)
            else:
                self._lbl_book_source.setText(current[: 62 - len(suffix) - 1] + "… " + suffix)
        # bookId / chapterId / readerId 写入日志（仅一次变更可见）
        prog_txt = f"{progress_pct:.2%}" if progress_pct is not None else "—"
        self._append_log(
            f"📚 当前书籍同步：source={source} title={title[:30]} bid={book_id[:12]}… "
            f"reader={reader_id[:14]}… chapter={chapter_id[:12]}… progress={prog_txt}",
            "INFO",
        )

    def _on_apply_book_url(self) -> None:
        url = self._edit_book_url.text().strip()
        if not url:
            QMessageBox.information(self, "提示", "请先粘贴微信读书「书本阅读页」URL。")
            return
        if "weread.qq.com" not in url:
            ok = QMessageBox.question(
                self, "非官方链接",
                f"URL 里不包含 weread.qq.com，仍要把它作为当前书链接保存？\n{url[:120]}"
            )
            if ok != QMessageBox.StandardButton.Yes:
                return
        book = self._api.set_current_book({"url": url}, source="manual")
        if book is None:
            QMessageBox.warning(self, "解析失败", "URL 无法解析为有效的书，请确认链接格式。")
            return
        # 解析后把"规范化后的 URL"写回输入框
        if book.get("url") and book.get("url") != url:
            self._edit_book_url.setText(str(book["url"]))
        # —— 如果解析出来 book_id 为空（reader_id 不是 wb/wr 前缀，无法直接解码），
        #    就异步调一次 resolve_book_id_for_reader：从书架 BOOKLIST 用 reader_id/title
        #    反向定位到真 bookId + 章节，并在后台自动补上（否则接下来"开始读书"会拿不到章节
        #    → payload chapter 不是这本书的 → read 返回空 {}）。
        bid = str(book.get("book_id") or "").strip()
        rid = str(book.get("book_reader_id") or "").strip()
        title = str(book.get("title") or "").strip()
        if not bid and (rid or title):
            self._append_log("ⓘ URL 已保存，但需要从书架反向解析真 bookId 与章节（用于阅读上报），后台解析中...", "INFO")

            def _t_resolve():
                try:
                    res2 = self._api.resolve_book_id_for_reader(
                        reader_id=rid or None,
                        title=title or None,
                        timeout=12,
                    )
                    if isinstance(res2, dict):
                        b2 = str(res2.get("book_id") or "").strip()
                        t2 = str(res2.get("title") or "").strip() or "(未命名)"
                        p2 = str(res2.get("progress_text") or "—")
                        if b2:
                            self._append_log(f"✅ 已反向解析：bookId={b2} 《{t2[:20]}》进度={p2}", "OK")
                            return
                    self._append_log("⚠️ 书架里没找到这本书对应 bookId（可能不在书架？）；建议先加入书架再开始读书。", "WARN")
                except Exception as _e:  # noqa: BLE001
                    self._append_log(f"⚠️ 反向解析异常：{_e}", "WARN")

            threading.Thread(target=_t_resolve, daemon=True).start()
        self._append_log("手动设置当前书成功（策略一·锁已开启：自动同步不会再覆盖你选的书）", "OK")
        self._refresh_book_lock_label()

    def _on_pick_book_from_shelf(self) -> None:
        """后台刷新（shelf/booklist → 进度最高未读完优先）
        注意：用户主动点击 → 使用 source='shelf_pick_manual'，策略一会把它当作用户操作。"""
        if self._health_worker_running:
            return
        self._append_log(
            "正在从微信读书书架挑选最近在读的书（这是你的主动操作：会解锁并用书架最新选择覆盖当前书）...",
            "INFO",
        )

        def _t() -> None:
            # source=shelf_pick_manual → USER_INITIATED_SOURCES → 解锁+覆盖
            res = self._api.refresh_current_book_from_shelf(source="shelf_pick_manual")
            if res:
                self._append_log(
                    f"挑选完成：《{(res.get('title') or '未命名')[:20]}》 "
                    f"进度={res.get('progress_text') or '—'}（来源：{res.get('source')}）",
                    "OK",
                )
            else:
                self._append_log("未能从书架选出书籍（未登录 / 书架空？）", "WARN")

        threading.Thread(target=_t, daemon=True).start()

    def _on_open_book_in_browser(self) -> None:
        """把当前书 URL 交给「扫码登录」Tab 的内置浏览器打开。"""
        cb = self._current_book_cache if isinstance(self._current_book_cache, dict) else None
        url = str((cb or {}).get("url") or "").strip() or self._edit_book_url.text().strip()
        if not url:
            QMessageBox.information(self, "尚未选择书籍", "先登录，并在上方输入或点「从书架挑选」选出一本书。")
            return
        self._append_log(f"将在扫码登录浏览器打开：{url[:120]}", "INFO")
        self.request_navigate_reader.emit(url)

    def _on_copy_book_url(self) -> None:
        cb = self._current_book_cache if isinstance(self._current_book_cache, dict) else None
        url = str((cb or {}).get("url") or "").strip() or self._edit_book_url.text().strip()
        if not url:
            QMessageBox.information(self, "尚未选择书籍", "没有可复制的链接。")
            return
        from PySide6.QtWidgets import QApplication
        cb2 = QApplication.clipboard()
        if cb2 is None:
            self._append_log("系统剪贴板不可用", "WARN")
            return
        cb2.setText(url)
        self._append_log(f"📋 已复制链接到剪贴板：{url[:120]}", "OK")

    def _on_clear_current_book(self) -> None:
        ok = QMessageBox.question(
            self, "确认",
            "清除当前正在读的书并取消锁定？\n（清除后会回到默认书籍池随机上报，后续自动同步又可以覆盖）",
        )
        if ok != QMessageBox.StandardButton.Yes:
            return
        # 策略一：用户点"清除" → source=clear → 解锁并清空
        self._api.set_current_book(None, source="clear")
        self._current_book_cache = None
        self._lbl_book_title.setText("已清除；你可以粘贴 URL 或在扫码登录浏览器打开一本书同步。")
        self._edit_book_url.clear()
        self._lbl_book_source.setText("尚未选择书籍")
        self._book_progress.setValue(0)
        self._book_progress.setFormat("—")
        self._lbl_last_report.setText("最近一次上报：尚未开始")
        self._refresh_book_lock_label()
        self._append_log("已清除当前阅读本（锁已解除）", "INFO")

    def _on_task_completed(self, minutes: int) -> None:
        self._append_log(f"🎉 今日任务完成，共约 {minutes // 60} 小时 {minutes % 60:02d} 分钟", "OK")

    def _on_cookie_broken(self) -> None:
        self._lbl_cookie.setText("登录态：❌ 已失效")
        self._lbl_cookie.setStyleSheet("color:#e25454;font-size:12px;font-weight:600;")

    def _update_cookie_status(self, ok: bool) -> None:  # noqa: FBT001
        if ok:
            self._lbl_cookie.setText("登录态：✅ 有效")
            self._lbl_cookie.setStyleSheet("color:#2eae5d;font-size:12px;font-weight:600;")
            self._append_log("登录态健康检查通过 ✅", "OK")
            # 如果之前在"登录态失效"状态，恢复按钮
            state_text = self._lbl_state.text()
            if "登录态失效" in state_text:
                self._lbl_state.setText("待机中")
                self._btn_start.setEnabled(True)
        else:
            self._lbl_cookie.setText("登录态：❌ 失效，请前往「扫码登录」")
            self._lbl_cookie.setStyleSheet("color:#e25454;font-size:12px;font-weight:600;")
            self._append_log("登录态健康检查未通过 ❌", "ERROR")

    # ---------------- Buttons ----------------
    def _on_start(self) -> None:
        if not self._api.check_session():
            # 先尝试恢复一次
            self._append_log("启动前检查登录态，发现无效，正在尝试自动刷新 Cookie...", "WARN")
            if not self._api.ensure_session():
                self._update_cookie_status(False)
                return
        self._update_cookie_status(True)
        if not self._scheduler.isRunning():
            self._scheduler.start()
            self._append_log("阅读任务已启动", "OK")
        else:
            self._scheduler.resume()
            self._append_log("阅读任务已恢复", "OK")
        # 启动即立刻尝试一次"官方阅读时长"（强制用缓存也行）
        from PySide6.QtCore import QTimer
        QTimer.singleShot(2500, lambda: self._refresh_reading_summary(force=False))
        # 启动也先刷新一次"Skill 启用状态"徽标（UI 立刻知道用户有没有配 Key）
        self._tick_skill_status()
        self._on_state_changed(self._lbl_state.text() or "阅读中")

    def _on_pause(self) -> None:
        self._scheduler.pause()
        self._append_log("阅读任务已暂停", "WARN")

    def _on_stop(self) -> None:
        self._scheduler.stop()
        self._append_log("阅读任务已停止", "WARN")
        self._on_state_changed("已停止")
        self._next_run_at = None

    def _on_verify(self) -> None:
        self._btn_verify.setEnabled(False)
        self._btn_verify.setText("检测中...")
        self._append_log("正在检测登录态...", "INFO")

        def _task() -> None:
            try:
                ok = bool(self._api.check_session())
                if not ok:
                    ok = bool(self._api.ensure_session())
                # 顺便取一次最新的官方阅读数据（轻量）
                self._refresh_reading_summary(force=False)
                msg = "OK" if ok else "FAIL"
            except Exception as exc:  # noqa: BLE001
                ok = False
                msg = f"exception: {exc}"
            self._verify_result.emit(ok)
            self._verify_finished.emit()
            self._health_check_done.emit(ok, msg)
        threading.Thread(target=_task, daemon=True).start()

    def _finish_verify(self) -> None:
        self._btn_verify.setEnabled(True)
        self._btn_verify.setText("🔍 检测登录态")

    # ---------------- 官方阅读时长 ----------------
    def _tick_skill_status(self) -> None:
        """顶栏"Skill 状态"徽标：告诉用户是否已配置 + 刷新阈值。"""
        if not hasattr(self, "_lbl_skill_status"):
            return
        skills_cfg = self._cfg.get("weread_skills", {}) or {}
        key = str(skills_cfg.get("api_key") or "").strip()
        n = max(1, min(500, int(skills_cfg.get("refresh_every_n_reads") or 10)))
        if not key:
            self._lbl_skill_status.setText(
                "未配置 Skill Key：前往「⚙️ 设置」→「微信读书官方 Skill」粘贴 wrk-* 后，"
                "书名/进度/时长才会优先用官方 Skill 返回。"
            )
            self._lbl_skill_status.setStyleSheet("color:#93a0b8;font-size:12px;")
        else:
            self._lbl_skill_status.setText(
                f"✅ Skill 已启用：每成功阅读 {n} 次同步一次官方阅读数据与当前书籍（约 {n * 30 // 60}~{n * 45 // 60} 分钟）"
            )
            self._lbl_skill_status.setStyleSheet("color:#2eae5d;font-size:12px;")

    def _refresh_reading_summary(self, *, force: bool) -> None:  # noqa: FBT001
        """触发一次阅读时长抓取；优先 weread-skills，没配 Key 时降级原 web 接口。

        缓存策略：
          * Skill 启用：缓存 2 分钟；force=True 或缓存超时都走 scheduler.trigger_skills_sync_nowait()
          * Skill 未启用：保留原来的 fetch_daily_reading_summary + 3 分钟缓存
        """
        # 每次进来都刷新一下"Skill 徽标"（用户刚在设置页保存的变化立刻可见）
        self._tick_skill_status()

        skills_cfg = self._cfg.get("weread_skills", {}) or {}
        key = str(skills_cfg.get("api_key") or "").strip()
        now = time.time()
        skill_enabled = bool(key)

        if skill_enabled:
            ttl = 2 * 60  # Skill 结果缓存 2 分钟（避免把官方网关打爆）
            cached_ok = (
                self._summary_cache is not None
                and (now - self._summary_cache_ts) < ttl
                and (self._summary_cache.get("source") or "").startswith("skill_")
            )
            if cached_ok and not force:
                self._apply_reading_summary(self._summary_cache)
                return
            # 有 Skill → 直接跑一次官方 Skill 网关：/readdata/detail overall + /shelf/sync + /book/info
            # ⚠️ 三重超时保证（从里到外）：
            #    ① weread_skills.call 单次 requests 分离 (connect=5s, read=≤15s)
            #    ② weread_skills.fetch_all 外层 ThreadPoolExecutor 总 20s 硬中断
            #    ③ status_page 本地主线程 QTimer(22s) 看门狗：绝对保证按钮解锁（不依赖子线程/信号链）
            if not getattr(self._scheduler, "_skills_worker_running", False):
                self._btn_refresh_summary.setEnabled(False)
            self._lbl_summary_tip.setText(
                "正在请求微信读书官方 Skill（无需安装 npx skills add；内部直连 POST i.weread.qq.com/api/agent/gateway）"
                "，请稍候...22 秒内未返回将自动解锁按钮，详情请查看下方日志窗口。"
            )
            # —— 启动本地看门狗（必开，不管 scheduler 内部 timer 是否 tick）——
            try:
                if self._skill_local_watchdog is None:
                    self._skill_local_watchdog = QTimer(self)
                    self._skill_local_watchdog.setSingleShot(True)
                    self._skill_local_watchdog.timeout.connect(self._on_skill_local_watchdog_timeout)
                self._skill_local_watchdog.stop()
                self._skill_local_watchdog.setInterval(22 * 1000)
                self._skill_local_watchdog.start()
            except Exception as _wd_exc:  # noqa: BLE001
                log.warning("启动 Skill 本地看门狗失败：%s", _wd_exc)
            self._scheduler.trigger_skills_sync_nowait()
            return

        # —— Skill 未启用：保留旧的 9 路接口兜底 ——
        ttl = 3 * 60
        cached_ok = (
            self._summary_cache is not None
            and (now - self._summary_cache_ts) < ttl
        )
        if cached_ok and not force:
            # 已有缓存 & 没过期 & 不是强制刷新 → 只用缓存更新 UI，不打接口
            self._apply_reading_summary(self._summary_cache)
            return
        if getattr(self, "_summary_worker_running", False):
            if force:
                self._lbl_summary_tip.setText("正在请求微信读书官方数据，请稍候...")
            return
        self._summary_worker_running = True
        self._btn_refresh_summary.setEnabled(False)
        self._lbl_summary_tip.setText("正在向微信读书官方请求今日阅读数据，请稍候...")

        def _worker() -> None:
            try:
                res = self._api.fetch_daily_reading_summary()
                err = res.get("error") if isinstance(res, dict) else None
                if isinstance(res, dict) and err != "no_cookies":
                    self._summary_cache_ts = time.time()
                    self._summary_cache = res
            except Exception as exc:  # noqa: BLE001
                res = {"error": f"exception: {exc}"}
            # —— 无论成功失败，都确保按钮可再次点击（Signal 可能没订阅/走了异常路径也不怕）——
            try:
                from PySide6.QtCore import QMetaObject, Qt as _Qt
                QMetaObject.invokeMethod(
                    self, "_unlock_summary_controls",
                    _Qt.ConnectionType.QueuedConnection,
                )
            except Exception:  # noqa: BLE001
                pass
            self._reading_summary_ready.emit(res if isinstance(res, dict) else {"error": "empty"})
        threading.Thread(target=_worker, daemon=True, name="ReadingSummary").start()

    def _on_skill_local_watchdog_timeout(self) -> None:
        """status_page 本地主线程 22s 看门狗到期：100% 解锁按钮 + 更新 tip。

        注意：理论上 scheduler 的 18s watchdog + fetch_all 20s ThreadPoolExecutor
        应该先触发，但万一线程/信号链出问题（比如信号没连上、QThread 事件循环异常），
        这里是 UI 侧最后一道防线。到期后直接认为 Skill 同步"超时失败"，构造
        兼容 payload 走一遍 apply 流程，确保 UI 状态一致。
        """
        self._append_log("⏱️  Skill 看门狗（本地 22s）触发：请求未在预期时间返回，已强制解锁按钮。", "WARN")
        try:
            self._lbl_summary_tip.setText(
                "⚠️ weread-skills 网关 22 秒内未返回（可能是网络到 i.weread.qq.com 不通或 Key 失效）。"
                "按钮已自动解锁，请：① 在设置页点击「验证 Key 有效性」再确认一次；"
                "② 检查本机是否能访问 i.weread.qq.com（通常需要国内网络）。"
            )
        except Exception:  # noqa: BLE001
            pass
        self._unlock_summary_controls()

    def _unlock_summary_controls(self) -> None:
        """所有"结束出口"最后统一调一次：保证按钮一定能再点（看门狗兜底的最后一道防线）。"""
        self._summary_worker_running = False
        # 把 scheduler 的 Skill 跑位标志也清一下（极端场景看门狗 fire 了但 worker 还没 finally 执行的双保险）
        try:
            setattr(self._scheduler, "_skills_worker_running", False)
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._btn_refresh_summary is not None:
                self._btn_refresh_summary.setEnabled(True)
        except Exception:  # noqa: BLE001
            pass

    def _apply_skill_sync(self, payload: dict) -> None:
        """Skill 同步结果：同时更新 官方阅读时长卡 + （如有）当前书籍卡。"""
        # Skill 同步信号到了 → 先把本地看门狗停掉（否则 22s 到期会错判成超时）
        try:
            if getattr(self, "_skill_local_watchdog", None) is not None:
                self._skill_local_watchdog.stop()
        except Exception:  # noqa: BLE001
            pass
        if not isinstance(payload, dict):
            return
        # 写入缓存 & 更新官方阅读时长卡
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        # 把 Skill 结果包装成"reading_summary payload 兼容结构"再复用 apply 逻辑
        # fetch_all 的 source 前缀 skill_*，apply_reading_summary 里根据 source 做文案分支
        merged: dict[str, Any] = dict(summary)
        merged.setdefault("source", "skill_readdata_detail_overall")
        err = payload.get("error")
        if err:
            merged.setdefault("error", "all_failed")
        merged.setdefault("fetched_at", int(payload.get("fetched_at") or time.time()))
        # 原始键名给调试
        merged.setdefault(
            "raw_keys",
            (
                list((payload.get("readdata") or {}).get("raw_keys") or [])
                + [str((payload.get("readdata") or {}).get("source") or "")]
            ),
        )
        self._summary_cache_ts = time.time()
        self._summary_cache = merged
        self._apply_reading_summary(merged)
        # Skill 同步返回的 current_book：如果有 title/progress 直接用它（优先级最高，
        # 因为它来自官方 /book/info 或 /shelf/sync，字段完整度远高于之前 shelf/booklist）
        cb = payload.get("current_book")
        if isinstance(cb, dict) and (cb.get("title") or cb.get("progress") is not None or cb.get("book_id")):
            # 如果当前页面没在展示"手动保存的书"，就把 Skill 拉出来的同步上去
            cur_cache = self._current_book_cache if isinstance(self._current_book_cache, dict) else {}
            old_progress = cur_cache.get("progress")
            new_progress = cb.get("progress")
            new_title = str(cb.get("title") or "").strip()
            cur_title = str(cur_cache.get("title") or "").strip()
            # 只要 Skill 解析出了进度或 title 有值就刷新（用户截图就是进度+标题缺）
            update_cond = (
                (new_title and new_title not in ("未命名书籍", "未选择书籍")
                 and ("未选择" in cur_title or "未命名" in cur_title or not cur_title))
                or (new_progress is not None and old_progress is None)
            )
            if update_cond:
                # 直接调用统一渲染入口（它会写缓存 + 更新输入框 + 进度条 + 日志）
                self._apply_current_book(cb)
        # —— Skill 同步结束，必须解锁按钮（看门狗 + 正常完成 + 异常出口全都最后走一次）——
        self._unlock_summary_controls()

    def _apply_reading_summary(self, payload: dict) -> None:
        """UI 线程：把 reading_summary 数据灌进"官方阅读时长卡"。"""
        self._summary_worker_running = False
        self._btn_refresh_summary.setEnabled(True)

        err = payload.get("error") if isinstance(payload, dict) else None
        source = payload.get("source") if isinstance(payload, dict) else None
        today_hm = payload.get("today_hm") if isinstance(payload, dict) else None
        week_hm = payload.get("week_hm") if isinstance(payload, dict) else None
        month_hm = payload.get("month_hm") if isinstance(payload, dict) else None
        total_hm = payload.get("total_hm") if isinstance(payload, dict) else None
        today_sec = payload.get("today_seconds") if isinstance(payload, dict) else None

        if isinstance(today_hm, str):
            self._sum_today_val.setText(today_hm)
        else:
            self._sum_today_val.setText("—")
        if isinstance(week_hm, str):
            self._sum_week_val.setText(week_hm)
        if isinstance(month_hm, str):
            self._sum_month_val.setText(month_hm)
        if isinstance(total_hm, str):
            self._sum_total_val.setText(total_hm)

        # 小提示：告诉用户结果来源 / 失败原因
        is_skill_source = isinstance(source, str) and (
            source.startswith("skill_") or source == "skill_disabled"
        )
        if err == "no_cookies":
            self._lbl_summary_tip.setText(
                "⚠️ 尚未检测到登录态；请先前往「扫码登录」完成登录后再刷新。"
            )
            if self._summary_cache_ts <= 0:
                self._lbl_summary_refresh.setText("暂无数据")
        elif err == "disabled: no wrk- API Key":
            self._lbl_summary_tip.setText(
                "ℹ️ 未配置 Skill Key，已自动切换为微信读书 web 端接口兜底。"
                " 前往「⚙️ 设置」→「微信读书官方 Skill」填入 wrk-* Key 可获得更稳定的统计数据。"
            )
        elif err == "all_failed" and is_skill_source:
            self._lbl_summary_tip.setText(
                "⚠️ weread-skills 网关暂时都没返回字段（返回结构/字段名可能变更）。"
                " 已按「⚙️ 设置」的阈值自动重试，也可点击「立即刷新（Skill 优先）」重试。"
            )
        elif err == "all_failed":
            self._lbl_summary_tip.setText(
                "⚠️ 所有官方接口暂时都没返回今日时长（可能需要等今天有第一次阅读上报）。"
                " 会自动重试，或点击「立即刷新（Skill 优先）」重试。"
            )
        elif isinstance(source, str) and isinstance(today_sec, int) and is_skill_source:
            # 10 次约 5 分钟；显示用户自己配的阈值
            skills_cfg = self._cfg.get("weread_skills", {}) or {}
            n = max(1, min(500, int(skills_cfg.get("refresh_every_n_reads") or 10)))
            self._lbl_summary_tip.setText(
                f"✅ 数据来源：微信读书官方 Skill（{source}）。每成功阅读 {n} 次同步一次；"
                f"数值与微信读书 App「我 → 统计」保持一致。"
            )
        elif isinstance(source, str) and isinstance(today_sec, int):
            self._lbl_summary_tip.setText(
                f"数据来源：微信读书官方接口（{source}）。每 3 分钟自动刷新；"
                f"数值与微信读书 App「我 → 统计」保持一致。"
            )
        elif isinstance(source, str):
            self._lbl_summary_tip.setText(
                f"数据来源：微信读书官方接口（{source}）。每 3 分钟自动刷新。"
            )
        else:
            self._lbl_summary_tip.setText(
                "登录后自动同步；每 3 分钟刷新一次。点击「立即刷新（Skill 优先）」可强制获取。"
            )
        # 更新"更新于 xxx 前"
        if isinstance(self._summary_cache_ts, (int, float)) and self._summary_cache_ts > 0:
            ago = int(time.time() - self._summary_cache_ts)
            self._lbl_summary_refresh.setText(
                f"更新于 {ago}s 前" if ago < 60 else f"更新于 {ago // 60} 分钟前"
            )

    # ---------------- 健康检查结果 ----------------
    def _apply_health_check(self, ok: bool, msg: str) -> None:  # noqa: FBT001
        if ok:
            self._append_log("主动健康检查：登录态 ✅ 有效", "OK")
        else:
            self._append_log(f"主动健康检查：❌ 结果={msg}", "ERROR")

    # ---------------- logs (批处理版，每 50ms flush 1 次) ----------------
    def _on_global_log(self, level: str, msg: str) -> None:
        self._append_log(msg, level)

    def _log_format_for(self, level_upper: str) -> QTextCharFormat:
        """按 level 缓存 QTextCharFormat：避免每条日志都 new QColor/QFont 造成 GUI 线程分配压力。"""
        cached = self._log_level_formats.get(level_upper)
        if cached is not None:
            return cached
        color_map = {
            "INFO": "#3c4a63",
            "WARN": "#d08a00",
            "WARNING": "#d08a00",
            "ERROR": "#e25454",
            "CRITICAL": "#e25454",
            "OK": "#2eae5d",
            "SUCCESS": "#2eae5d",
        }
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color_map.get(level_upper, "#3c4a63")))
        fmt.setFontFamilies(["Consolas", "Microsoft YaHei UI"])
        fmt.setFont(self._log_monospace_font)
        self._log_level_formats[level_upper] = fmt
        return fmt

    def _append_log(self, msg: str, level: str) -> None:
        """新路径：不直接操作 QPlainTextEdit（那会立即触发全文重绘 + 游标移动）。
        只把 (level, 原始 msg) 入队；50ms 节拍的 _flush_log_buffer 统一一次性刷，
        并把「连续同 level + 同 msg」折叠为 "msg ×N"，减少日志窗口重复噪声。"""
        lvl = str(level).upper()
        text = str(msg)
        # 去重：连续同一条日志折叠计数
        key = (lvl, text)
        if self._log_last_key == key:
            self._log_last_count += 1
        else:
            # 先把上一条 flush-ready 的记录入队（带可能已累积的 ×N 计数）
            if self._log_last_key is not None:
                self._log_pending.append((
                    self._log_last_key[0],
                    self._format_with_count(self._log_last_key[1], self._log_last_count),
                ))
            self._log_last_key = key
            self._log_last_count = 1

    @staticmethod
    def _format_with_count(text: str, count: int) -> str:
        if count <= 1:
            return text
        return f"{text}  ×{count}"

    def _flush_log_buffer(self) -> None:
        """每 50ms 跑一次：把 pending 队列 + _log_last_key 一次性 append 到 log view。
        避免 GUI 线程在调度器 / Skill / 全局 log 信号风暴下 每秒刷几十次游标。"""
        # Step 1: 把 _log_last_key 作为"最后一段 active"入队（带当前累积计数），重置去重缓冲
        if self._log_last_key is not None:
            self._log_pending.append((
                self._log_last_key[0],
                self._format_with_count(self._log_last_key[1], self._log_last_count),
            ))
            self._log_last_key = None
            self._log_last_count = 0

        if not self._log_pending:
            return

        # Step 2: 批量生成渲染片段（timestamp + fmt + text + \n）
        #   —— 关键：全程 setUpdatesEnabled(False)，避免每 insertText 触发 paint，
        #      最后才统一滚底（而不是每条后滚底）。
        now_hms = dt.datetime.now().strftime("%H:%M:%S")
        try:
            self._log_view.setUpdatesEnabled(False)
            cursor = QTextCursor(self._log_view.document())
            cursor.movePosition(QTextCursor.MoveOperation.End)
            for lvl, text in self._log_pending:
                fmt = self._log_format_for(lvl)
                prefix = f"[{now_hms}] [{lvl}]  "
                cursor.insertText(prefix + text + "\n", fmt)
        finally:
            self._log_pending.clear()
            self._log_view.setUpdatesEnabled(True)

        # 统一一次滚底：只在"用户本来就在底部附近"时才自动滚
        # —— 如果用户正在手动翻到上面看旧日志，就不强行拉下来（避免交互卡顿）
        sb = self._log_view.verticalScrollBar()
        if sb is not None:
            near_bottom = (sb.value() + 32) >= sb.maximum()
            if near_bottom:
                sb.setValue(sb.maximum())
