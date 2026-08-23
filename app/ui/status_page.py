"""状态 / 日志页：今日进度卡 + 官方阅读时长卡 + 控制按钮 + 实时日志。"""
from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Any

from PySide6.QtCore import QTimer, Qt, Signal, QUrl, QElapsedTimer, Slot, QMetaObject, Q_ARG
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

        # ====== 需求1：上下布局 → 上方左右布局 ======
        top_hbox = QHBoxLayout()
        top_hbox.setSpacing(12)

        # ----- 左 70%：阅读状态卡 + 阅读统计卡 -----
        left_vbox = QVBoxLayout()
        left_vbox.setSpacing(12)

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

        left_vbox.addWidget(card)

        # ====== "正在读的书"卡 — 需求1：已删除可视部分，保留隐藏占位控件避免 AttributeError ======
        self._lbl_book_source = QLabel("（已移除）")
        self._lbl_book_source.hide()
        self._lbl_book_lock = QLabel("（已移除）")
        self._lbl_book_lock.hide()
        self._lbl_book_title = QLabel("（已移除）")
        self._lbl_book_title.hide()
        self._edit_book_url = QLineEdit()
        self._edit_book_url.hide()
        self._book_progress = QProgressBar()
        self._book_progress.setRange(0, 10000)
        self._book_progress.hide()
        self._lbl_last_report = QLabel("（已移除）")
        self._lbl_last_report.hide()
        self._current_book_cache: dict | None = None

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
        self._btn_refresh_summary = QPushButton("🔄 立即刷新")
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

        # 4 个方块：今日 / 本周 / 本月 / 累计 —— 需求：改为 2×2 布局
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        # ===== 2×2：每行 2 列，stretch=1 均分 =====
        for col_i in range(2):
            grid.setColumnStretch(col_i, 1)
            grid.setColumnMinimumWidth(col_i, 140)
        self._sum_today_val = QLabel("—")
        self._sum_today_key = QLabel("今日已读")
        self._sum_week_val = QLabel("—")
        self._sum_week_key = QLabel("本周累计")
        self._sum_month_val = QLabel("—")
        self._sum_month_key = QLabel("本月累计")
        self._sum_total_val = QLabel("—")
        self._sum_total_key = QLabel("总累计")
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
        grid.addWidget(self._sum_today_key, 1, 0)
        grid.addWidget(self._sum_week_val, 0, 1)
        grid.addWidget(self._sum_week_key, 1, 1)
        grid.addWidget(self._sum_month_val, 2, 0)
        grid.addWidget(self._sum_month_key, 3, 0)
        grid.addWidget(self._sum_total_val, 2, 1)
        grid.addWidget(self._sum_total_key, 3, 1)
        c2v.addLayout(grid)

        self._lbl_summary_tip = QLabel("登录后自动同步；每 3 分钟刷新一次。点击「立即刷新」可强制获取。")
        self._lbl_summary_tip.setStyleSheet("color:#93a0b8;font-size:12px;")
        self._lbl_summary_tip.setWordWrap(True)
        c2v.addWidget(self._lbl_summary_tip)

        left_vbox.addWidget(card2)

        top_hbox.addLayout(left_vbox, 11)  # 左 55% (11/20)

        # ====== 右 45%：内置浏览器容器（main_window 会把 LoginPage 塞进来）======
        self._right_container = QFrame()
        self._right_container.setStyleSheet(
            "QFrame{background:#f4f7fc;border:1px solid #e3e8f1;border-radius:10px;}"
        )
        self._right_layout = QVBoxLayout(self._right_container)
        self._right_layout.setContentsMargins(8, 8, 8, 8)
        self._right_layout.setSpacing(6)
        # 浏览器占位标签（需求：删掉使用说明，只保留简短"浏览器加载中"）
        self._lbl_browser_placeholder = QLabel("🔐 内置浏览器加载中…")
        self._lbl_browser_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_browser_placeholder.setStyleSheet(
            "color:#7a879f;font-size:13px;background:#ffffff;"
            "border-radius:8px;padding:20px;"
        )
        self._right_layout.addWidget(self._lbl_browser_placeholder, 1)
        top_hbox.addWidget(self._right_container, 9)  # 右 45% (9/20)

        root.addLayout(top_hbox, 7)  # 上方占主体

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
        self._log_view.setMinimumHeight(120)
        self._log_view.setStyleSheet(
            "QPlainTextEdit{background:#ffffff;"
            "border:1px solid #e5eaf2;border-radius:10px;"
            "padding:6px 10px;color:#2f3b52;font-size:12.5px;}"
        )
        root.addWidget(self._log_view, 3)  # 下方日志占 30%

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
            # 🔧 阅读结束（完成/停止/空闲）：恢复 JS 拦截器
            if hasattr(self, "_login_page") and self._login_page:
                self._login_page.set_reader_active(False)
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
        # —— 优先使用 Skill 混合方案的完成度，兼容旧字段 ——
        done = int(info.get("completed_minutes") or info.get("started_minutes", 0) or 0)
        target = int(info.get("target_minutes", 0) or 0)
        skill_baseline = int(info.get("skill_baseline_sec", 0) or 0)
        skill_ok = bool(info.get("skill_available", False))
        if target <= 0:
            self._progress.setRange(0, 100)
            self._progress.setValue(0)
        else:
            self._progress.setRange(0, target)
            self._progress.setValue(min(done, target))
        # 今日目标显示：Skill 基线信息加入
        skill_tag = ""
        if skill_ok and skill_baseline > 0:
            skill_tag = f"  (Skill 已读 {skill_baseline}s)"
        elif not skill_ok:
            skill_tag = "  (本地估算)"
        self._lbl_target.setText(
            f"今日目标：{target // 60}h{target % 60:02d}m  "
            f"/  已完成：{done // 60}h{done % 60:02d}m{skill_tag}"
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
            "auto_sync_book": "官方同步",
            "auto_sync_summary": "官方同步",
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
        """▶ 开始阅读：启动自动化抓取工作流（状态机版本）。

        工作流阶段：
        1. CHECKING_SESSION - 异步自检登录态
        2. LOADING_BOOK_LOGIN - 未登录则跳三体书 + 触发"我已登录完成"逻辑
        3. CAPTURING - 登录态有效则注入 JS + 轮询 + 跳目标章节
        4. VALIDATING - 校验抓取数据，满足则启动 scheduler
        5. NEED_LOGIN - 失败则弹窗 + 禁用按钮 + 等用户扫码
        """
        # 防重入：如果工作流正在跑，提示并返回
        state = getattr(self, "_workflow_state", "IDLE")
        if state not in ("IDLE", "READING", ""):
            self._append_log(f"工作流进行中（{state}），请稍候...", "WARN")
            return
        # 重置状态机
        self._workflow_state = "CHECKING_SESSION"
        self._capture_retry_count = 0
        # 清理可能的旧定时器
        for attr in ("_satisfaction_timer", "_capture_timeout_timer"):
            t = getattr(self, attr, None)
            if t is not None:
                try:
                    t.stop()
                except Exception:  # noqa: BLE001
                    pass
                setattr(self, attr, None)
        # 启动 scheduler 已运行 → 直接 resume
        if self._scheduler.isRunning():
            self._scheduler.resume()
            self._append_log("阅读任务已恢复", "OK")
            self._workflow_state = "READING"
            return
        self._append_log("🚀 启动自动化抓取工作流", "INFO")
        self._workflow_step1_check_session()

    # ===== 工作流状态机 =====
    def _workflow_step1_check_session(self) -> None:
        """阶段1: 异步自检登录态"""
        self._workflow_state = "CHECKING_SESSION"
        self._append_log("阶段1: 自检登录态...", "INFO")

        def _task():
            try:
                ok = bool(self._api.check_session())
            except Exception as exc:  # noqa: BLE001
                log.warning("工作流 check_session 异常：%s", exc)
                ok = False
            # 回到 UI 线程
            try:
                QMetaObject.invokeMethod(
                    self, "_on_session_check_done", Qt.ConnectionType.QueuedConnection,
                    Q_ARG(bool, ok)
                )
            except Exception:  # noqa: BLE001
                # fallback：用 singleShot
                QTimer.singleShot(0, lambda: self._on_session_check_done(ok))

        threading.Thread(target=_task, daemon=True).start()

    @Slot(bool)
    def _on_session_check_done(self, ok: bool) -> None:
        """阶段1 完成：根据登录态决定下一步"""
        if ok:
            self._update_cookie_status(True)
            self._append_log("✅ 登录态有效，进入抓取阶段", "OK")
            self._workflow_step3_start_capture()
        else:
            self._update_cookie_status(False)
            self._append_log("⚠️ 登录态无效，跳三体书种 cookie", "WARN")
            self._workflow_step2_load_book_for_login()

    def _workflow_step2_load_book_for_login(self) -> None:
        """阶段2: 跳三体书 URL + 触发"我已登录完成"按钮逻辑（cookie 同步）"""
        self._workflow_state = "LOADING_BOOK_LOGIN"
        login_pg = self._login_page
        if login_pg is None:
            self._append_log("无法访问登录页，工作流中止", "ERROR")
            self._workflow_state = "IDLE"
            return

        # 读三体书 URL
        santi_book_url = str(self._cfg.get("reading.santi_book_url") or "").strip()
        if not santi_book_url:
            santi_book_url = "https://weread.qq.com/web/reader/ce032b305a9bc1ce0b0dd2a"

        # 1. 跳转三体书 URL
        try:
            from PySide6.QtCore import QUrl, QTimer
            web = getattr(login_pg, "_web", None)
            if web is not None:
                self._append_log(f"浏览器跳转：{santi_book_url[:80]}...", "INFO")
                web.load(QUrl(santi_book_url))
            else:
                self._append_log("浏览器控件不可用，工作流中止", "ERROR")
                self._workflow_state = "IDLE"
                return
            # 2. 等 4s 页面加载完后，触发"我已登录完成"按钮的完整逻辑（cookie 采集 + 保存到 config）
            QTimer.singleShot(4000, self._workflow_step2_trigger_done_button)
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"阶段2跳转失败：{exc}", "ERROR")
            self._workflow_step4_need_login()

    def _workflow_step2_trigger_done_button(self) -> None:
        """阶段2.5: 调用 _on_done_clicked 完整逻辑（触发 cookie 采集 + 保存 + session_ready 信号）"""
        login_pg = self._login_page
        if login_pg is None:
            self._workflow_step4_need_login()
            return
        try:
            self._append_log("触发 cookie 采集（_on_done_clicked）...", "INFO")
            login_pg._on_done_clicked()
            # session_ready 信号会触发 _maybe_resume_workflow_after_login
            # 但此处我们走的是 LOADING_BOOK_LOGIN 路径，不会进入恢复分支
            # 所以需要自己接续：8s 后再次自检
            from PySide6.QtCore import QTimer
            QTimer.singleShot(8000, self._workflow_step2_recheck_session)
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"cookie 采集失败：{exc}", "ERROR")
            self._workflow_step4_need_login()

    def _workflow_step2_recheck_session(self) -> None:
        """阶段2.6: cookie 采集完成后再自检一次"""
        self._append_log("阶段2: 重新自检登录态...", "INFO")
        # 复用阶段1的 check_session 流程
        self._workflow_step1_check_session()

    def _workflow_step3_start_capture(self) -> None:
        """阶段3: 注入 JS 劫持 + 启动轮询 + 跳目标章节"""
        self._workflow_state = "CAPTURING"
        login_pg = self._login_page
        if login_pg is None:
            self._append_log("无法访问登录页，工作流中止", "ERROR")
            self._workflow_state = "IDLE"
            return

        # 读配置
        santi_book_url = str(self._cfg.get("reading.santi_book_url") or "").strip()
        santi_chapter_url = str(self._cfg.get("reading.santi_chapter_url") or "").strip()
        if not santi_book_url:
            santi_book_url = "https://weread.qq.com/web/reader/ce032b305a9bc1ce0b0dd2a"
        if not santi_chapter_url:
            santi_chapter_url = "https://weread.qq.com/web/reader/ce032b305a9bc1ce0b0dd2ak92c3210025c92cc22753209"

        # 0. 先确保浏览器在三体书页（防止用户离开过）
        try:
            from PySide6.QtCore import QUrl, QTimer
            web = getattr(login_pg, "_web", None)
            if web is not None:
                current_url = web.url().toString().strip()
                if santi_book_url not in current_url:
                    self._append_log(f"先加载三体书页：{santi_book_url[:80]}...", "INFO")
                    web.load(QUrl(santi_book_url))
                    # 等 3s 加载完再启动抓取
                    QTimer.singleShot(3000, lambda: login_pg.start_auto_capture_workflow(santi_chapter_url))
                else:
                    login_pg.start_auto_capture_workflow(santi_chapter_url)
            else:
                self._append_log("浏览器控件不可用，工作流中止", "ERROR")
                self._workflow_state = "IDLE"
                return
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"阶段3启动失败：{exc}", "ERROR")
            self._workflow_step4_need_login()
            return

        # 启动超时定时器
        timeout_sec = int(self._cfg.get("reading.capture_timeout_sec") or 15)
        from PySide6.QtCore import QTimer
        self._capture_timeout_timer = QTimer(self)
        self._capture_timeout_timer.setSingleShot(True)
        self._capture_timeout_timer.timeout.connect(self._workflow_step4_validate)
        self._capture_timeout_timer.start(timeout_sec * 1000)

        # 启动满意度轮询（每 1s 检查 is_capture_satisfied）
        self._satisfaction_timer = QTimer(self)
        self._satisfaction_timer.timeout.connect(self._check_capture_satisfaction)
        self._satisfaction_timer.start(1000)
        self._append_log(f"阶段3: 已启动抓取+轮询，超时 {timeout_sec}s", "INFO")

    def _check_capture_satisfaction(self) -> None:
        """每秒检查一次抓取数据是否满足条件"""
        try:
            if self._login_page.is_capture_satisfied():
                self._satisfaction_timer.stop()
                self._append_log("✅ 抓取数据已满足阅读条件", "OK")
                self._workflow_step4_validate()
        except Exception as exc:  # noqa: BLE001
            log.warning("工作流满意度检查异常：%s", exc)

    def _workflow_step4_validate(self) -> None:
        """阶段5: 校验抓取数据"""
        # 停止定时器
        for attr in ("_satisfaction_timer", "_capture_timeout_timer"):
            t = getattr(self, attr, None)
            if t is not None:
                try:
                    t.stop()
                except Exception:  # noqa: BLE001
                    pass

        if self._login_page.is_capture_satisfied():
            # 满足 → 关闭抓取，启动阅读
            self._login_page.stop_auto_capture_workflow()
            self._workflow_step5_start_reading()
        else:
            # 不满足 → 重试
            self._capture_retry_count += 1
            max_retry = int(self._cfg.get("reading.workflow_retry_count") or 2)
            if self._capture_retry_count <= max_retry:
                self._append_log(
                    f"抓取数据不完整，重试第 {self._capture_retry_count}/{max_retry} 次",
                    "WARN"
                )
                # 停止当前抓取再重启
                self._login_page.stop_auto_capture_workflow()
                from PySide6.QtCore import QTimer
                QTimer.singleShot(1000, self._workflow_step3_start_capture)
            else:
                # 重试耗尽 → 需要扫码登录
                self._append_log(f"重试 {max_retry} 次仍失败，进入需要登录流程", "WARN")
                self._workflow_step4_need_login()

    def _workflow_step4_need_login(self) -> None:
        """阶段4: 失败时跳转 weread.qq.com 让用户扫码 + 禁用按钮"""
        self._workflow_state = "NEED_LOGIN"
        # 停止抓取
        try:
            self._login_page.stop_auto_capture_workflow()
        except Exception:  # noqa: BLE001
            pass
        # 跳转微信读书首页让用户扫码
        try:
            from PySide6.QtCore import QUrl
            web = getattr(self._login_page, "_web", None)
            if web is not None:
                web.load(QUrl("https://weread.qq.com/"))
        except Exception:  # noqa: BLE001
            pass
        # 弹窗提示
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(
            self, "需要重新登录",
            "登录态失效，已自动跳转微信读书首页。\n\n"
            "请扫码登录后，点击右侧「✅ 我已登录完成」按钮，\n"
            "系统将自动恢复抓取工作流并启动阅读。"
        )
        # 禁用开始按钮
        try:
            self._btn_start.setEnabled(False)
        except Exception:  # noqa: BLE001
            pass
        self._append_log("工作流暂停：等待用户扫码登录", "WARN")

    def _workflow_resume_after_login(self) -> None:
        """用户扫码后点击"我已登录完成"按钮触发的恢复入口。

        重新走 阶段2 → 阶段1 → 阶段3 流程。
        """
        # 恢复按钮可点击
        try:
            self._btn_start.setEnabled(True)
        except Exception:  # noqa: BLE001
            pass
        self._capture_retry_count = 0
        self._workflow_state = "LOADING_BOOK_LOGIN"
        self._append_log("🎯 用户登录完成，恢复工作流：跳三体书种 cookie", "INFO")
        self._workflow_step2_load_book_for_login()

    def _workflow_step5_start_reading(self) -> None:
        """阶段6: 启动 scheduler 执行阅读循环"""
        self._workflow_state = "READING"
        # 复用旧 _do_normal_start 的核心启动逻辑
        try:
            if not self._api.check_session():
                if not self._api.ensure_session():
                    self._update_cookie_status(False)
                    self._workflow_step4_need_login()
                    return
            self._update_cookie_status(True)
            # 🔧 阅读中：关闭 JS 拦截器（避免干扰）
            if hasattr(self, "_login_page") and self._login_page:
                self._login_page.set_reader_active(True)
            if not self._scheduler.isRunning():
                self._scheduler.start()
                self._append_log("阅读任务已启动（拦截器已关闭）", "OK")
            else:
                self._scheduler.resume()
                self._append_log("阅读任务已恢复", "OK")
            # 启动后 2.5s 刷新一次摘要
            from PySide6.QtCore import QTimer
            QTimer.singleShot(2500, lambda: self._refresh_reading_summary(force=False))
            self._tick_summary_status()
            self._on_state_changed(self._lbl_state.text() or "阅读中")
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"启动 scheduler 失败：{exc}", "ERROR")
            self._workflow_state = "IDLE"

    def _on_pause(self) -> None:
        self._scheduler.pause()
        self._append_log("阅读任务已暂停", "WARN")

    def _on_stop(self) -> None:
        self._scheduler.stop()
        # 🔧 停止阅读：重新开启 JS 拦截器
        if hasattr(self, "_login_page") and self._login_page:
            self._login_page.set_reader_active(False)
        self._append_log("阅读任务已停止（拦截器已恢复）", "WARN")
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
    def _tick_summary_status(self) -> None:
        """顶栏"摘要状态"徽标：统一提示官方数据自动拉取规则。"""
        if not hasattr(self, "_lbl_skill_status"):
            return
        self._lbl_skill_status.setText("官方数据同步：登录后自动拉取；每 3 分钟刷新")
        self._lbl_skill_status.setStyleSheet("color:#93a0b8;font-size:12px;")

    def _refresh_reading_summary(self, *, force: bool) -> None:  # noqa: FBT001
        """触发一次官方阅读时长抓取（单路径：fetch_daily_reading_summary）。

        缓存策略：TTL 3 分钟；force=True 强制跳过缓存。
        """
        self._tick_summary_status()
        now = time.time()
        ttl = 3 * 60
        cached_ok = (
            self._summary_cache is not None
            and (now - self._summary_cache_ts) < ttl
        )
        if cached_ok and not force:
            self._apply_reading_summary(self._summary_cache)
            return
        if getattr(self, "_summary_worker_running", False):
            if force:
                self._lbl_summary_tip.setText("正在请求微信读书官方数据...")
            return
        self._summary_worker_running = True
        self._btn_refresh_summary.setEnabled(False)
        self._btn_refresh_summary.setText("🔄 立即刷新")
        self._lbl_summary_tip.setText("正在请求微信读书官方数据...")

        def _worker() -> None:
            try:
                res = self._api.fetch_daily_reading_summary()
                err = res.get("error") if isinstance(res, dict) else None
                if isinstance(res, dict) and err != "no_cookies":
                    self._summary_cache_ts = time.time()
                    self._summary_cache = res
            except Exception as exc:  # noqa: BLE001
                res = {"error": f"exception: {exc}"}
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

    def _unlock_summary_controls(self) -> None:
        """所有"结束出口"最后统一调一次：保证按钮一定能再点。"""
        self._summary_worker_running = False
        try:
            if self._btn_refresh_summary is not None:
                self._btn_refresh_summary.setEnabled(True)
        except Exception:  # noqa: BLE001
            pass

    def _apply_reading_summary(self, payload: dict) -> None:
        """UI 线程：把 reading_summary 数据灌进"官方阅读时长卡"。"""
        self._summary_worker_running = False
        self._btn_refresh_summary.setEnabled(True)
        self._btn_refresh_summary.setText("🔄 立即刷新")

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
        else:
            self._sum_week_val.setText("—")
        if isinstance(month_hm, str):
            self._sum_month_val.setText(month_hm)
        else:
            self._sum_month_val.setText("—")
        if isinstance(total_hm, str):
            self._sum_total_val.setText(total_hm)
        else:
            self._sum_total_val.setText("—")

        if err == "no_cookies":
            self._lbl_summary_tip.setText(
                "⚠️ 尚未检测到登录态；请先前往「扫码登录」完成登录后再刷新。"
            )
            if self._summary_cache_ts <= 0:
                self._lbl_summary_refresh.setText("暂无数据")
        elif isinstance(source, str) and isinstance(today_sec, int):
            self._lbl_summary_tip.setText(
                f"✅ 数据来源：微信读书官方接口（{source}）。每 3 分钟自动刷新；"
                f"数值与微信读书 App「我 → 统计」一致。"
            )
        elif isinstance(source, str):
            self._lbl_summary_tip.setText(
                f"数据来源：微信读书官方接口（{source}）。每 3 分钟自动刷新。"
            )
        else:
            self._lbl_summary_tip.setText(
                "⚠️ 暂时未获取到阅读统计（首次需登录后等 1-2 分钟，或点立即刷新）。"
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
