"""主界面 v5 · 🏆 米白纸 Paper Studio（T 形宽幅）。

布局严格 T 形：
  ┌─────────────────── 顶部：标题/日期条（中性简约） ──────────────────────┐
  ├────────────── 第二排：4 项 KPI 通栏（今日本周本月总计 · 充实 5 层） ────────┤
  ├──────────────┬─────────────────────────┬─────────────────────────────┤
  │  左列·状态    │  中列·章节与封面书脊    │  右列·操作与 Cookie/开关/版本 │
  │  · 登录态     │  · 书脊卡片（墨黑书脊）  │  · 扫码登录 查看书架          │
  │  · 今日目标   │  · 墨黑进度条（ok 绿）   │  · 获取数据（墨黑主按钮）    │
  │  · 已完成     │  · 当前章节 / 已读次数   │  · 刷新统计 / 刷新进度        │
  │  · 下一次请求 │                         │  · 配置中心                  │
  │  · 控制按钮   │                         │  · Cookie 状态 |夜间开关| v2.1 │
  └──────────────┴─────────────────────────┴─────────────────────────────┘
  ┌──────────────────── 底部跨栏：深墨 slate 卷 工作日志 ────────────────────┐

按钮严格一行文字 + 图标（无副说明）；设置 → "配置中心"；B 方案开关位于 右列底部
「Cookie 状态 · 🌙/☀️ 夜间开关 · 版本号」三者中间。
配色：米白纸 #F9F9F5 + 细边框灰 #E5E5DF + 墨黑 #1F2937 · 仅金/绿做状态编码
"""
from __future__ import annotations

import time
from typing import Any

from PySide6.QtCore import QTimer, Qt, Signal, QThread, QObject, QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QSizePolicy,
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
from app.ui.icon_store import (
    APP_VERSION,
    app_version_display,
    icon_config_center,
    icon_fetch,
    icon_login,
    icon_pause,
    icon_refresh_progress,
    icon_refresh_stats,
    icon_shelf,
    icon_start,
    icon_stop,
)
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
            self.log_msg.emit(f"[fetch_shelf] 返回 {len(books)} 本")
            if books:
                self._db.update_shelf(books)
                self.log_msg.emit(f"书架更新完成：{len(books)} 本")
            else:
                self.log_msg.emit("[warn] fetch_shelf 返回空，请检查 Skill API Key 或响应格式")
            unread = self._db.get_unread_books()
            self.log_msg.emit(f"未读完书：{len(unread)} 本，开始拉章节池/进度（前 10 本）")
            for book in unread[:10]:
                bid = str(book.get("bookId"))
                title = str(book.get("title", "未知"))
                if not self._db.has_chapters(bid):
                    chapters = self._skill.fetch_chapters(bid)
                    if chapters:
                        self._db.update_chapters(bid, title, chapters)
                        self.log_msg.emit(f"章节池：{title} -> {len(chapters)} 章")
                progress_data = self._skill.fetch_progress(bid)
                if progress_data:
                    percent = progress_data.get("percent")
                    if percent is not None:
                        try:
                            self._db.update_book_progress(bid, int(float(percent)))
                        except (TypeError, ValueError):
                            pass
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
                self.log_msg.emit(
                    f"统计已更新：今日 {local_stats['today_seconds'] // 60} 分钟"
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("获取数据失败")
            self.log_msg.emit(f"[x] 获取数据失败：{exc}")
        finally:
            self.done.emit()


class _SkillRefreshThread(QThread):
    """后台刷新 Skill 单项数据（统计 or 当前进度）。"""

    log_msg = Signal(str)
    stats_updated = Signal(dict)
    book_progress_updated = Signal(str, int)
    done = Signal()

    def __init__(
        self,
        skill: SkillAPI,
        db: "LocalDB",
        mode: str,
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
            self.log_msg.emit(f"[x] Skill 刷新失败：{exc}")
        finally:
            self.done.emit()

    def _refresh_stats(self) -> None:
        self.log_msg.emit("正在获取 Skill 阅读统计...")
        stats = self._skill.fetch_reading_stats()
        if not stats:
            self.log_msg.emit("[x] 获取阅读统计失败（返回空）")
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
            f"[ok] 阅读统计已刷新：今日 {local_stats['today_seconds'] // 60} 分钟"
        )

    def _refresh_progress(self) -> None:
        if not self._book_id:
            self.log_msg.emit("[warn] 无当前书籍，无法刷新进度")
            return
        self.log_msg.emit(f"正在获取 Skill 书籍进度：bookId={self._book_id}")
        progress_data = self._skill.fetch_progress(self._book_id)
        if not progress_data:
            self.log_msg.emit("[x] 获取书籍进度失败（返回空）")
            return
        percent = progress_data.get("percent")
        if percent is None:
            self.log_msg.emit("[x] 书籍进度返回无 percent 字段")
            return
        try:
            new_progress = int(float(percent))
        except (TypeError, ValueError):
            self.log_msg.emit(f"[x] percent 格式异常：{percent}")
            return
        self._db.update_book_progress(self._book_id, new_progress)
        self.book_progress_updated.emit(self._book_id, new_progress)
        self.log_msg.emit(f"[ok] 书籍进度已刷新：{new_progress}%")


class MainPage(QWidget):
    """主界面（🏆 米白纸 Paper Studio · T 形宽幅）。"""

    # 注：v3 起不再支持 dark / 夜间开关，仅保留单一米白纸主题。

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
        self._theme: str = "light"

        self._log_buffer: list[str] = []
        self._log_auto_scroll = True
        self._current_book_id: str | None = None
        self._skill_refresh_thread: _SkillRefreshThread | None = None

        self._build_ui()
        self._connect_signals()
        self._init_log_flush_timer()
        self._apply_theme_to_icons(self._theme)
        self._refresh_login_status()
        self._refresh_reading_stats()

    # ------------------------------------------------------------------
    # UI 构建（T 形：Logo+日期 → 4KPI 独立白卡 → 三列主区 → 日志+右下角版本号）
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 16)
        root.setSpacing(14)

        # -- 1. 顶部：Logo（左） + 日期胶囊 · 状态点 · Cookie 胶囊（右） --
        root.addLayout(self._build_header_row(), 0)

        # -- 2. 4 张 KPI 独立白卡 --
        root.addWidget(self._build_kpi_panel(), 0)

        # -- 3. 三列主区（状态 · 章节/书脊 · 藏书楼操作） = 3 : 4 : 4 --
        root.addWidget(self._build_three_cols(), 3)

        # -- 4. 跨栏：深墨 slate 卷 工作日志（含右下角 vX.Y.Z 小字） --
        root.addWidget(self._build_log_panel(), 2)

    # ========== 顶行：左 Logo「微信读书助手」· 右 日期胶囊 + 状态点 + Cookie 胶囊 ==========
    def _build_header_row(self) -> QHBoxLayout:
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(12)

        # 左：Logo 字（32px 粗黑，无卡片、无边框、不套 Frame）
        logo = QLabel("微信读书助手")
        logo.setObjectName("titleLabel")
        logo.setStyleSheet(
            "QLabel#titleLabel{color:#1F2937;font-size:32px;font-weight:700;"
            "letter-spacing:3px;"
            "font-family:'Microsoft YaHei UI','PingFang SC',sans-serif;}"
        )
        h.addWidget(logo)
        h.addStretch(1)

        # 右：日期胶囊
        self._lbl_date = QLabel("-")
        self._lbl_date.setObjectName("headerDate")
        self._lbl_date.setStyleSheet(
            "QLabel#headerDate{color:#1F2937;font-size:14px;font-weight:600;"
            "padding:6px 18px;border:1px solid #E5E5DF;border-radius:6px;"
            "background:#F9F9F5;letter-spacing:0.5px;"
            "font-family:'Microsoft YaHei UI',sans-serif;}"
        )
        # 状态点
        self._cookie_dot = QLabel()
        self._cookie_dot.setProperty("class", "status-dot")
        self._cookie_dot.setStyleSheet(
            "min-width:10px;min-height:10px;max-width:10px;max-height:10px;"
            "border-radius:5px;background:#D0D4DA;border:1px solid #A9AFB8;"
        )
        # Cookie 胶囊（绿底 or 红底）
        self._cookie_label = QLabel("Cookie：检查中")
        self._cookie_label.setProperty("class", "cookie-label")
        self._cookie_label.setStyleSheet(
            "padding:4px 14px;border-radius:6px;background:#FFFFFF;"
            "border:1px solid #E5E5DF;font-size:12px;color:#374151;"
            "font-family:'Microsoft YaHei UI',sans-serif;letter-spacing:0.5px;"
        )

        right_wrap = QHBoxLayout()
        right_wrap.setSpacing(8)
        right_wrap.addWidget(self._lbl_date)
        right_wrap.addSpacing(4)
        right_wrap.addWidget(self._cookie_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        right_wrap.addWidget(self._cookie_label, 0, Qt.AlignmentFlag.AlignVCenter)
        h.addLayout(right_wrap)
        return h

    # ========== 4KPI 通栏：今日 / 本周 / 本月 / 累计  ·  整板条（非卡片） ==========
    def _build_kpi_panel(self) -> QWidget:
        """放弃卡片形式 · 一张整板 + 内 4 段（段间距 18px · 段等宽）。

        结构要点（本轮用户反馈：4 段间距一点都没有）：
          · 外框 = 一张整板（白底 + 1px 细边 + 圆角 12） → 非 4 独立卡
          · 内部 = QGridLayout 4 列 × 4 行，4 列全 setColumnStretch(col,1) → 等宽
          · horizontalSpacing = 18 → 段与段之间有明确的 gap
          · 4 行全部 setRowMinimumHeight + setRowStretch=0 → 零弹性，
            彻底避免 Windows Fusion 高 DPI 下 "数值/标题被吃成 0 高"

        垂直预算（硬预算，绝不参与弹性压缩）：
            行 0  标题         28   (18px 字)
            行 1  数值         54   (36px 字 · baseline 完整)
            行 2  aux          18   (12px muted)
            行 3  进度条      12   (条 4 + 底留 8)
            padding  20 / 16 / 20 / 12
            --------------------------
            合计  112 + 内距 28 → 整板 minHeight = 148 兜底
        """
        # ── 面板：单一整板外框 ──
        panel = QFrame()
        panel.setProperty("class", "kpi-panel")
        panel.setStyleSheet(
            "QFrame.kpi-panel, QFrame[class='kpi-panel']{"
            "background:#FFFFFF;border:1px solid #E5E5DF;border-radius:12px;"
            "}"
        )
        panel.setMinimumHeight(148)
        panel.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )

        # ── 内部统一 Grid（4 列 × 4 行）──
        #   4 列 = 今日 / 本周 / 本月 / 累计，纯数据列，无额外 SEP 列
        #   段间间距 = horizontalSpacing = 18
        inner = QGridLayout(panel)
        inner.setContentsMargins(20, 16, 20, 12)   # 板内：左右 20 / 顶 16 / 底 12
        inner.setHorizontalSpacing(18)             # 段间距：18px（本轮核心修复）
        inner.setVerticalSpacing(4)

        # 4 列全 stretch=1 → 严格等宽
        for c in range(4):
            inner.setColumnStretch(c, 1)
        # 4 行 stretch 全 0 → 只按 RowMinimumHeight 展开
        for r in range(4):
            inner.setRowStretch(r, 0)
        inner.setRowMinimumHeight(0, 28)
        inner.setRowMinimumHeight(1, 54)
        inner.setRowMinimumHeight(2, 18)
        inner.setRowMinimumHeight(3, 12)

        # ── 4 段配置 ──
        cfg = [
            ("today", "今日阅读"),
            ("week",  "本周阅读"),
            ("month", "本月阅读"),
            ("total", "累计阅读"),
        ]

        # 数值（36px · 微信蓝 · JetBrains Mono 800）
        self._lbl_today = QLabel("-")
        self._lbl_week = QLabel("-")
        self._lbl_month = QLabel("-")
        self._lbl_total = QLabel("-")
        self._kpi_labels = {
            "today": self._lbl_today,
            "week":  self._lbl_week,
            "month": self._lbl_month,
            "total": self._lbl_total,
        }
        VALUE_QSS = (
            "color:#307CFF;font-size:36px;font-weight:800;letter-spacing:0.5px;"
            "padding:0;margin:0;border:none;background:transparent;"
            "font-family:'JetBrains Mono',Consolas,monospace;"
        )

        # aux（12px muted）
        self._kpi_aux = {k: QLabel("-") for k, _ in cfg}
        AUX_QSS = (
            "color:#6B7280;font-size:12px;letter-spacing:0.2px;"
            "padding:0;margin:0;border:none;background:transparent;"
            "font-family:'JetBrains Mono','Microsoft YaHei UI',sans-serif;"
        )

        # 进度条（4px 无边框 · 贴底 · 距底留 8px）
        self._kpi_bars = {k: QProgressBar() for k, _ in cfg}
        BAR_QSS = (
            "QProgressBar{border:none;border-radius:0px;"
            "background:#F1F1ED;height:4px;min-height:4px;max-height:4px;"
            "margin:0;padding:0;}"
            "QProgressBar::chunk{border-radius:0px;background:#374151;}"
        )

        TITLE_QSS = (
            "color:#1F2937;font-size:18px;font-weight:700;letter-spacing:1.5px;"
            "padding:0;margin:0;border:none;background:transparent;"
            "font-family:'Microsoft YaHei UI','PingFang SC',sans-serif;"
        )

        for col, (key, title) in enumerate(cfg):
            # ── 标题 18px 墨黑 ──
            t = QLabel(title)
            t.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            t.setStyleSheet(TITLE_QSS)
            t.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            t.setMinimumHeight(28)
            inner.addWidget(t, 0, col,
                            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

            # ── 数值 36px 微信蓝 ──
            v = self._kpi_labels[key]
            v.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            v.setStyleSheet(VALUE_QSS)
            v.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            v.setMinimumHeight(54)
            inner.addWidget(v, 1, col,
                            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

            # ── aux 12px muted ──
            a = self._kpi_aux[key]
            a.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            a.setStyleSheet(AUX_QSS)
            a.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            a.setMinimumHeight(18)
            inner.addWidget(a, 2, col,
                            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

            # ── 进度条 4px 贴底（距底留 8）──
            bar = self._kpi_bars[key]
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setFormat("")
            bar.setTextVisible(False)
            bar.setMinimumHeight(4)
            bar.setMaximumHeight(4)
            bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            bar.setStyleSheet(BAR_QSS)

            pb_wrap = QWidget()
            pb_wrap.setStyleSheet("background:transparent;")
            pb_wrap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            pb_wrap.setMinimumHeight(12)
            pbl = QVBoxLayout(pb_wrap)
            pbl.setContentsMargins(0, 0, 0, 8)   # 距板底 8px（用户约定"关键反转"）
            pbl.setSpacing(0)
            pbl.addWidget(bar)
            inner.addWidget(pb_wrap, 3, col)

        return panel

    # ========== 三列主区（左·中·右） ==========
    def _build_three_cols(self) -> QWidget:
        wrap = QWidget()
        h = QHBoxLayout(wrap)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(12)
        h.addWidget(self._build_col_status(), 3)
        h.addWidget(self._build_col_chapter(), 4)
        h.addWidget(self._build_col_ops(), 4)
        return wrap

    # ---------- 左列·状态 ----------
    def _build_col_status(self) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        card.setStyleSheet(
            "QFrame{background:#FFFFFF;border:1px solid #E5E5DF;border-radius:14px;}"
        )
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(8)

        v.addWidget(self._make_deco_title("📋  阅  读  状  态"))

        # 登录态 / 状态 / 下次请求 （2x3 grid）
        grid = QGridLayout()
        grid.setSpacing(6)
        grid.setHorizontalSpacing(14)
        self._lbl_login = QLabel("登录态：检查中...")
        self._lbl_status = QLabel("运行状态：待机")
        self._lbl_target = QLabel("今日目标：- 分钟")
        self._lbl_done = QLabel("已完成：- 分钟 (-%)")
        self._lbl_next = QLabel("下次请求：-")
        self._lbl_success_fail = QLabel("成功：0   失败：0")
        for lbl in (
            self._lbl_login, self._lbl_status, self._lbl_target,
            self._lbl_done, self._lbl_next, self._lbl_success_fail,
        ):
            lbl.setStyleSheet(
                "color:#1F2937;font-size:13px;"
                "font-family:'Microsoft YaHei UI','PingFang SC',sans-serif;"
                "padding:2px 0;"
            )
        grid.addWidget(self._lbl_login,         0, 0)
        grid.addWidget(self._lbl_status,        0, 1)
        grid.addWidget(self._lbl_target,        1, 0)
        grid.addWidget(self._lbl_done,          1, 1)
        grid.addWidget(self._lbl_next,          2, 0)
        grid.addWidget(self._lbl_success_fail,  2, 1)
        v.addLayout(grid)

        v.addWidget(self._solid_sep())

        # 控制按钮：始 / 暂 / 止
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._btn_start = QPushButton(" 开  始 ")
        self._btn_start.setProperty("role", "ok")
        self._btn_start.setIconSize(QSize(20, 20))
        self._btn_start.setMinimumHeight(42)
        self._btn_start.clicked.connect(self._on_start)
        self._btn_pause = QPushButton(" 暂  停 ")
        self._btn_pause.setProperty("role", "warn")
        self._btn_pause.setIconSize(QSize(20, 20))
        self._btn_pause.setMinimumHeight(42)
        self._btn_pause.setEnabled(False)
        self._btn_pause.clicked.connect(self._on_pause)
        self._btn_stop = QPushButton(" 停  止 ")
        self._btn_stop.setProperty("role", "danger")
        self._btn_stop.setIconSize(QSize(20, 20))
        self._btn_stop.setMinimumHeight(42)
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._on_stop)
        btn_row.addWidget(self._btn_start, 1)
        btn_row.addWidget(self._btn_pause, 1)
        btn_row.addWidget(self._btn_stop, 1)
        v.addLayout(btn_row)

        v.addStretch(1)
        return card

    # ---------- 中列·当前阅读（米黄底 · 墨黑细书脊 · 仿摊开册页） ----------
    def _build_col_chapter(self) -> QWidget:
        card = QFrame()
        card.setObjectName("chapterCard")
        # 三列中列底 = 纯白外框（圆角 14px）+ 米黄内卡（仿摊开的册页纸面）
        card.setStyleSheet(
            "QFrame#chapterCard{"
            "background:#FFFFFF;border:1px solid #E5E5DF;border-radius:14px;"
            "}"
        )
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(8)

        v.addWidget(self._make_deco_title("📖  当  前  阅  读"))

        # 仿书脊封面卡（米黄底 #FBF3DE · 左 6px 墨黑书脊）
        spine = QFrame()
        spine.setObjectName("bookSpineCard")
        spine.setStyleSheet(
            "QFrame#bookSpineCard{"
            "background:#FBF3DE;"   # ← 米黄底（当前阅读·册页感）
            "border:1px solid #E6D7B0;"
            "border-left:6px solid #1F2937;"
            "border-radius:12px;"
            "min-height:160px;"
            "}"
        )
        sv = QHBoxLayout(spine)
        sv.setContentsMargins(14, 12, 14, 12)
        sv.setSpacing(10)

        info_wrap = QWidget()
        info_layout = QVBoxLayout(info_wrap)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(6)
        self._lbl_book_title = QLabel("书  名：— 尚 未 开 卷 —")
        self._lbl_book_title.setStyleSheet(
            "color:#1F2937;font-size:16px;font-weight:700;letter-spacing:1px;"
            "font-family:'Microsoft YaHei UI','PingFang SC',sans-serif;"
        )
        self._lbl_book_progress = QLabel("-")
        self._lbl_book_progress.setStyleSheet(
            "color:#374151;font-size:12px;letter-spacing:0.5px;"
            "font-family:'JetBrains Mono',Consolas,monospace;"
        )
        # 进度条（墨黑渐变 or ok 绿 · 贴米黄纸面更有质感）
        self._bar_book = QProgressBar()
        self._bar_book.setRange(0, 100)
        self._bar_book.setValue(0)
        self._bar_book.setFormat("%p% · 开卷有益")
        self._bar_book.setMinimumHeight(18)
        self._bar_book.setStyleSheet(
            "QProgressBar{border:1px solid #E6D7B0;border-radius:999px;"
            "background:#F7ECC6;height:18px;text-align:center;"
            "color:#1F2937;font-weight:700;letter-spacing:0.5px;"
            "font-family:'JetBrains Mono',monospace;}"
            "QProgressBar::chunk{border-radius:999px;"
            "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #374151, stop:1 #1F2937);}"
        )

        self._lbl_chapter = QLabel("当 前 章 节 ——")
        self._lbl_chapter.setStyleSheet(
            "color:#374151;font-size:13px;"
            "font-family:'Microsoft YaHei UI',sans-serif;"
        )
        self._lbl_chapter_count = QLabel("本章已读：- 次")
        self._lbl_chapter_count.setStyleSheet(
            "color:#6B7280;font-size:12px;letter-spacing:0.5px;"
            "font-family:'JetBrains Mono',Consolas,monospace;"
        )

        info_layout.addWidget(self._lbl_book_title)
        info_layout.addWidget(self._lbl_book_progress)
        info_layout.addWidget(self._bar_book)
        info_layout.addWidget(self._solid_sep())
        info_layout.addWidget(self._lbl_chapter)
        info_layout.addWidget(self._lbl_chapter_count)
        info_layout.addStretch(1)
        sv.addWidget(info_wrap, 1)

        v.addWidget(spine, 2)
        v.addStretch(1)
        return card

    # ---------- 右列·藏书楼操作（3×2 均分网格） ----------
    def _build_col_ops(self) -> QWidget:
        card = QFrame()
        card.setProperty("class", "card")
        card.setStyleSheet(
            "QFrame{background:#FFFFFF;border:1px solid #E5E5DF;border-radius:14px;}"
        )
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(10)

        v.addWidget(self._make_deco_title("🔘  藏  书  楼  操  作"))

        # 3×2 操作网格（按用户指定顺序，两列均分宽度 stretch=1）
        #   行 1：扫码登录 · 查看书架
        #   行 2：获取数据 · 刷新统计
        #   行 3：刷新进度 · 配置中心
        g = QGridLayout()
        g.setSpacing(8)
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(10)

        self._btn_login = QPushButton(" 扫码登录")
        self._btn_login.setProperty("role", "secondary")
        self._btn_login.setIconSize(QSize(20, 20))
        self._btn_login.setMinimumHeight(42)
        self._btn_login.clicked.connect(self._on_cdp_login)

        self._btn_shelf = QPushButton(" 查看书架")
        self._btn_shelf.setProperty("role", "secondary")
        self._btn_shelf.setIconSize(QSize(20, 20))
        self._btn_shelf.setMinimumHeight(42)
        self._btn_shelf.clicked.connect(self._on_view_shelf)

        self._btn_fetch = QPushButton(" 获取数据")
        self._btn_fetch.setProperty("role", "primary")
        self._btn_fetch.setIconSize(QSize(20, 20))
        self._btn_fetch.setMinimumHeight(42)
        self._btn_fetch.clicked.connect(self._on_fetch_data)

        self._btn_refresh_stats = QPushButton(" 刷新统计")
        self._btn_refresh_stats.setProperty("role", "secondary")
        self._btn_refresh_stats.setIconSize(QSize(20, 20))
        self._btn_refresh_stats.setMinimumHeight(42)
        self._btn_refresh_stats.clicked.connect(self._on_refresh_stats)

        self._btn_refresh_progress = QPushButton(" 刷新进度")
        self._btn_refresh_progress.setProperty("role", "secondary")
        self._btn_refresh_progress.setIconSize(QSize(20, 20))
        self._btn_refresh_progress.setMinimumHeight(42)
        self._btn_refresh_progress.clicked.connect(self._on_refresh_progress)

        self._btn_settings = QPushButton(" 配置中心")   # 原名：设置
        self._btn_settings.setProperty("role", "secondary")
        self._btn_settings.setIconSize(QSize(20, 20))
        self._btn_settings.setMinimumHeight(42)
        self._btn_settings.clicked.connect(self._on_open_settings)

        for b in (
            self._btn_login, self._btn_shelf, self._btn_fetch,
            self._btn_refresh_stats, self._btn_refresh_progress, self._btn_settings,
        ):
            b.setCursor(Qt.CursorShape.PointingHandCursor)

        # 行 1：登录 / 书架
        g.addWidget(self._btn_login,            0, 0)
        g.addWidget(self._btn_shelf,            0, 1)
        # 行 2：获取数据 / 刷新统计
        g.addWidget(self._btn_fetch,            1, 0)
        g.addWidget(self._btn_refresh_stats,    1, 1)
        # 行 3：刷新进度 / 配置中心
        g.addWidget(self._btn_refresh_progress, 2, 0)
        g.addWidget(self._btn_settings,         2, 1)
        # 两列均分宽度（防止主按钮 primary 视觉拉伸不均）
        g.setColumnStretch(0, 1)
        g.setColumnStretch(1, 1)
        v.addLayout(g)

        v.addWidget(self._solid_sep())

        # 最后刷新
        self._lbl_last_refresh = QLabel("最后刷新：-")
        self._lbl_last_refresh.setProperty("class", "muted")
        self._lbl_last_refresh.setStyleSheet(
            "color:#6B7280;font-size:12px;"
            "font-family:'JetBrains Mono',Consolas,monospace;"
        )
        v.addWidget(self._lbl_last_refresh)

        # (v5 起) cookie 胶囊 / 夜间开关 / 版本 三件套移至别处：
        #   - cookie 胶囊 → header 右
        #   - 夜读开关 → 移除（单主题米白纸）
        #   - 版本号 → 日志面板右下角
        v.addStretch(1)
        return card

    # ========== 工作日志：深墨 slate 卷（右下角 v 版本小胶囊） ==========
    def _build_log_panel(self) -> QWidget:
        box = QFrame()
        box.setObjectName("logCardWrap")
        box.setStyleSheet(
            "QFrame#logCardWrap{"
            "background:#FFFFFF;"
            "border:1px solid #E5E5DF;border-radius:14px;}"
        )
        v = QVBoxLayout(box)
        v.setContentsMargins(14, 12, 14, 14)
        v.setSpacing(8)

        # ── 顶行：📜 日志标题 + 分割线 + ⏸ 暂停滚动 / 🗑️ 清空 ──
        header = QHBoxLayout()
        icon = QLabel("📜")
        icon.setStyleSheet("font-size:16px;")
        title = QLabel("工  作  日  志")
        title.setStyleSheet(
            "color:#1F2937;font-size:15px;font-weight:700;letter-spacing:1px;"
            "font-family:'Microsoft YaHei UI',sans-serif;"
        )
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(
            "border:none;border-top:1px solid #E5E5DF;max-height:1px;min-height:1px;"
        )
        header.addWidget(icon)
        header.addWidget(title)
        header.addWidget(sep, 1)

        self._btn_pause_scroll = QPushButton(" ⏸  暂停滚动 ")
        self._btn_pause_scroll.setCheckable(True)
        self._btn_pause_scroll.setProperty("role", "secondary")
        self._btn_pause_scroll.setMinimumHeight(32)
        self._btn_pause_scroll.clicked.connect(self._on_toggle_scroll)

        self._btn_clear_log = QPushButton(" 🗑️  清  空 ")
        self._btn_clear_log.setProperty("role", "secondary")
        self._btn_clear_log.setMinimumHeight(32)
        self._btn_clear_log.clicked.connect(self._on_clear_log)
        header.addWidget(self._btn_pause_scroll)
        header.addWidget(self._btn_clear_log)
        v.addLayout(header)

        # ── 日志视图 + 右下角 v 版本胶囊（叠层，版本号右对齐贴底）──
        log_holder = QHBoxLayout()
        log_holder.setContentsMargins(0, 0, 0, 0)
        log_holder.setSpacing(0)

        self._log_view = QPlainTextEdit()
        self._log_view.setObjectName("logView")
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(800)

        # 版本号（米白纸色胶囊 · 小字）叠在右下角 → 实现方法：放 log_view 下一行，与 log_holder 共用 VBox
        log_holder.addWidget(self._log_view, 1)
        v.addLayout(log_holder, 1)

        # ── 底行：左占位，右 v 版本胶囊 ──
        footer = QHBoxLayout()
        footer.setContentsMargins(8, 6, 8, 2)
        footer.setSpacing(0)
        footer.addStretch(1)
        self._lbl_version = QLabel(f"v{app_version_display()}")
        self._lbl_version.setProperty("class", "version")
        self._lbl_version.setStyleSheet(
            "color:#6B7280;padding:2px 12px;border:1px solid #E5E5DF;"
            "border-radius:999px;background:#F9F9F5;"
            "font-family:'JetBrains Mono',Consolas,monospace;font-size:11px;"
            "letter-spacing:0.5px;"
        )
        footer.addWidget(self._lbl_version)
        v.addLayout(footer, 0)
        return box

    # ---------- 小工具：装饰标题 · 实灰线分隔 ----------
    def _make_deco_title(self, text: str) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        # 左边竖线
        lbar = QFrame()
        lbar.setFixedWidth(3)
        lbar.setStyleSheet(
            "background:#1F2937;border-radius:2px;min-height:14px;max-height:14px;"
        )
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "color:#1F2937;font-size:14px;font-weight:700;letter-spacing:1px;"
            "font-family:'Microsoft YaHei UI','PingFang SC',sans-serif;"
        )
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(
            "border:none;border-top:1px solid #E5E5DF;max-height:1px;min-height:1px;"
        )
        h.addWidget(lbar)
        h.addWidget(lbl)
        h.addWidget(sep, 1)
        return w

    @staticmethod
    def _solid_sep() -> QFrame:
        s = QFrame()
        s.setFrameShape(QFrame.Shape.HLine)
        s.setStyleSheet(
            "border:none;border-top:1px solid #E5E5DF;max-height:1px;min-height:1px;"
        )
        return s

    # ---------- 图标主题染色 ----------
    def _apply_theme_to_icons(self, theme_name: str) -> None:
        t = theme_name
        self._btn_login.setIcon(icon_login(22, t))
        self._btn_shelf.setIcon(icon_shelf(22, t))
        self._btn_fetch.setIcon(icon_fetch(22, t))
        self._btn_refresh_stats.setIcon(icon_refresh_stats(22, t))
        self._btn_refresh_progress.setIcon(icon_refresh_progress(22, t))
        self._btn_settings.setIcon(icon_config_center(22, t))
        self._btn_start.setIcon(icon_start(24, t))
        self._btn_pause.setIcon(icon_pause(24, t))
        self._btn_stop.setIcon(icon_stop(24, t))

    # ------------------------------------------------------------------
    # 信号连接
    # ------------------------------------------------------------------
    def _connect_signals(self) -> None:
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

        log_bus.log_emitted.connect(self._on_log_bus)

        self._date_timer = QTimer(self)
        self._date_timer.timeout.connect(self._refresh_date)
        self._date_timer.start(60000)
        self._refresh_date()

        self._countdown_timer = QTimer(self)
        self._countdown_timer.timeout.connect(self._refresh_next_request)
        self._countdown_timer.start(1000)

    def _init_log_flush_timer(self) -> None:
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(100)
        self._flush_timer.timeout.connect(self._flush_log_buffer)
        self._flush_timer.start()

    # ------------------------------------------------------------------
    # UI 更新槽
    # ------------------------------------------------------------------
    def _refresh_date(self) -> None:
        import datetime as _dt
        weekdays = ["一", "二", "三", "四", "五", "六", "日"]
        d = _dt.datetime.now()
        wd = weekdays[d.weekday()]
        self._lbl_date.setText(
            f"{d.year} · {d.month:02d} · {d.day:02d}   星期{wd}"
        )

    def _refresh_next_request(self) -> None:
        next_at = getattr(self._scheduler, "_next_run_at", 0.0)
        if next_at <= 0:
            self._lbl_next.setText("下次请求：—")
            return
        remain = next_at - time.time()
        if remain <= 0:
            self._lbl_next.setText("下次请求：即 将")
            return
        mins = int(remain) // 60
        secs = int(remain) % 60
        self._lbl_next.setText(f"下次请求：{mins:02d}:{secs:02d}")

    def _refresh_login_status(self) -> None:
        cookies = self._cfg.get_cookies_dict()
        wr_skey = cookies.get("wr_skey", "")
        ok = bool(wr_skey and len(str(wr_skey)) >= 8)
        if ok:
            self._lbl_login.setText("登录态：✅ 有 效")
            self._lbl_login.setStyleSheet(
                "color:#1E7B2E;font-size:13px;font-weight:600;"
                "font-family:'Microsoft YaHei UI',sans-serif;padding:2px 0;"
            )
            self._cookie_label.setProperty("state", "ok")
            self._cookie_label.setText("Cookie：有效")
            self._cookie_label.setStyleSheet(
                "padding:4px 14px;border-radius:999px;"
                "background:#EFFBF0;color:#1E7B2E;border:1px solid #2D9D3C;"
                "font-family:'Microsoft YaHei UI',sans-serif;font-size:12px;letter-spacing:0.5px;"
            )
            self._cookie_dot.setStyleSheet(
                "min-width:10px;min-height:10px;max-width:10px;max-height:10px;"
                "border-radius:5px;background:#2D9D3C;border:1px solid #1E7B2E;"
            )
        else:
            self._lbl_login.setText("登录态：❌ 失 效")
            self._lbl_login.setStyleSheet(
                "color:#A72D2D;font-size:13px;font-weight:600;"
                "font-family:'Microsoft YaHei UI',sans-serif;padding:2px 0;"
            )
            self._cookie_label.setProperty("state", "bad")
            self._cookie_label.setText("Cookie：失效")
            self._cookie_label.setStyleSheet(
                "padding:4px 14px;border-radius:999px;"
                "background:#FCEEED;color:#A72D2D;border:1px solid #D14343;"
                "font-family:'Microsoft YaHei UI',sans-serif;font-size:12px;letter-spacing:0.5px;"
            )
            self._cookie_dot.setStyleSheet(
                "min-width:10px;min-height:10px;max-width:10px;max-height:10px;"
                "border-radius:5px;background:#D14343;border:1px solid #A72D2D;"
            )

    @staticmethod
    def _set_kpi_progress_tone(bar: QProgressBar, value: int, maxval: int) -> None:
        """KPI 条状态编码：chunk 用 绿 / 金 / 灰。

        关键：
          - 4px · 无边框（border:none）
          - 无圆角（border-radius:0）· 左右自然到卡片边缘
          - 贴卡底（外部通过 pb_wrap 底 8px 留白控制与卡底距离）
        ≥ 80% → 青苔绿（达标）
        40–79% → 琥珀金（进行中）
        < 40%  → 墨灰（起步）
        """
        ratio = max(0, min(1, (value / maxval) if maxval > 0 else 0))
        if ratio >= 0.8:
            chunk = (
                "qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                "stop:0 #39B14B, stop:1 #2D9D3C)"
            )
        elif ratio >= 0.4:
            chunk = (
                "qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                "stop:0 #EDB44C, stop:1 #D99B2A)"
            )
        else:
            chunk = "#6B7280"
        bar.setStyleSheet(
            "QProgressBar{border:none;border-radius:0px;"
            "background:#F1F1ED;height:4px;min-height:4px;max-height:4px;"
            "margin:0;padding:0;}"
            f"QProgressBar::chunk{{border-radius:0px;background:{chunk};}}"
        )

    def _refresh_reading_stats(self) -> None:
        stats = self._db.get_reading_stats()
        today_sec = int(stats.get("today_seconds", 0))
        week_sec  = int(stats.get("weekly_seconds", 0))
        month_sec = int(stats.get("monthly_seconds", 0))
        total_sec = int(stats.get("total_seconds", 0))
        self._kpi_labels["today"].setText(self._fmt_hm(today_sec))
        self._kpi_labels["week"].setText(self._fmt_hm(week_sec))
        self._kpi_labels["month"].setText(self._fmt_hm(month_sec))
        self._kpi_labels["total"].setText(self._fmt_hm(total_sec))

        # 目标值：今日 120 分钟
        TARGET_MIN = 120
        today_min = today_sec // 60
        today_pct = max(0, min(100, (today_min * 100 // TARGET_MIN)))
        self._kpi_bars["today"].setValue(today_pct)
        self._set_kpi_progress_tone(self._kpi_bars["today"], today_pct, 100)
        self._kpi_aux["today"].setText(
            f"目标 {TARGET_MIN} 分钟 · 完成 {today_pct}%"
        )

        # 本周：参考 7d * TARGET_MIN = 840m
        week_min = week_sec // 60
        week_target = 7 * TARGET_MIN
        week_pct = max(0, min(100, week_min * 100 // max(1, week_target)))
        week_avg = week_min // 7 if week_min > 0 else 0
        self._kpi_bars["week"].setValue(week_pct)
        self._set_kpi_progress_tone(self._kpi_bars["week"], week_pct, 100)
        self._kpi_aux["week"].setText(
            f"日均 {week_avg} 分钟 · 参考进度 {week_pct}%"
        )

        # 本月：参考 30d * TARGET_MIN = 3600m
        month_min = month_sec // 60
        month_target = 30 * TARGET_MIN
        month_pct = max(0, min(100, month_min * 100 // max(1, month_target)))
        import datetime as _dt
        days_active = max(1, _dt.datetime.now().day)
        month_avg = month_min // days_active if month_min > 0 else 0
        self._kpi_bars["month"].setValue(month_pct)
        self._set_kpi_progress_tone(self._kpi_bars["month"], month_pct, 100)
        self._kpi_aux["month"].setText(
            f"{days_active} 天活跃 · 日均 {month_avg} 分钟 · {month_pct}%"
        )

        # 累计
        total_min = total_sec // 60
        total_h = total_min // 60
        # 累计参考：一年目标 365 * TARGET_MIN
        total_ref = 365 * TARGET_MIN
        total_pct = max(0, min(100, total_min * 100 // max(1, total_ref)))
        total_avg = total_min // 365 if total_min > 0 else 0
        self._kpi_bars["total"].setValue(total_pct)
        self._set_kpi_progress_tone(self._kpi_bars["total"], total_pct, 100)
        self._kpi_aux["total"].setText(
            f"合计 {total_h} 小时 · 日均约 {total_avg} 分钟 · {total_pct}%"
        )

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
            return f"{h}h {m:02d}m"
        return f"{m}m"

    # ------------------------------------------------------------------
    # 主题（v5 起仅保留单一「米白纸 Paper Studio」主题）
    # ------------------------------------------------------------------
    def _on_theme_toggled(self, checked: bool) -> None:  # 兼容桩（占位，不再切换）
        return None

    # ------------------------------------------------------------------
    # scheduler 信号槽
    # ------------------------------------------------------------------
    def _on_state_changed(self, text: str) -> None:
        self._lbl_status.setText(f"运行状态：{text}")

    def _on_log_emitted(self, msg: str) -> None:
        self._log_buffer.append(msg)

    def _on_progress(self, info: dict) -> None:
        target = info.get("target_minutes", 0)
        today_sec = info.get("today_seconds", 0)
        today_min = int(today_sec) // 60
        success = info.get("success", 0)
        fail = info.get("fail", 0)
        pct = (today_min * 100 // target) if target > 0 else 0
        self._lbl_target.setText(f"今日目标：{target} 分钟")
        self._lbl_done.setText(f"已完成：{today_min} 分钟 ({pct}%)")
        self._lbl_success_fail.setText(f"成功：{success}   失败：{fail}")
        book = info.get("book")
        if isinstance(book, dict) and book.get("bookId"):
            self._current_book_id = str(book.get("bookId"))

    def _on_book_progress(
        self, title: str, progress: int, chapter_title: str, chapter_count: int
    ) -> None:
        self._lbl_book_title.setText(f"书  名：{title}")
        self._lbl_book_progress.setText(f"进 度：{progress}%")
        self._bar_book.setValue(max(0, min(100, int(progress))))
        self._lbl_chapter.setText(f"当 前 章 节：{chapter_title}")
        self._lbl_chapter_count.setText(f"本章已读：{chapter_count} 次")

    def _on_book_changed(self, title: str, progress: int) -> None:
        self._lbl_book_title.setText(f"书  名：{title}")
        self._lbl_book_progress.setText(f"进 度：{progress}%")
        self._bar_book.setValue(max(0, min(100, int(progress))))

    def _on_chapter_changed(self, title: str, uid: int) -> None:
        self._lbl_chapter.setText(f"当 前 章 节：{title}（uid={uid}）")
        self._lbl_chapter_count.setText("本章已读：0 次")

    def _on_stats_updated(self, stats: dict) -> None:
        self._refresh_reading_stats()
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
        self._lbl_status.setText(f"运行状态：已完成（{minutes} 分钟）")
        self._btn_start.setEnabled(True)
        self._btn_pause.setEnabled(False)
        self._btn_stop.setEnabled(False)

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------
    def _on_log_bus(self, level: str, msg: str) -> None:
        self._log_buffer.append(f"[{level}] {msg}")

    def _flush_log_buffer(self) -> None:
        if not self._log_buffer:
            return
        text = "\n".join(self._log_buffer)
        self._log_buffer.clear()
        self._log_view.appendPlainText(text)
        if self._log_auto_scroll:
            sb = self._log_view.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _on_toggle_scroll(self) -> None:
        self._log_auto_scroll = not self._btn_pause_scroll.isChecked()
        self._btn_pause_scroll.setText(
            " ▶  恢复滚动 " if self._btn_pause_scroll.isChecked() else " ⏸  暂停滚动 "
        )

    def _on_clear_log(self) -> None:
        self._log_view.clear()
        self._log_buffer.clear()

    # ------------------------------------------------------------------
    # 按钮槽
    # ------------------------------------------------------------------
    def _on_start(self) -> None:
        cookies = self._cfg.get_cookies_dict()
        wr_skey = cookies.get("wr_skey", "")
        if not wr_skey or len(str(wr_skey)) < 8:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "未登录", "请先点击「扫码登录」获取 Cookie。")
            return
        shelf = self._db.get_shelf()
        if not shelf:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "无书架数据", "请先点击「获取数据」拉取书架/章节池/进度。")
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
            QMessageBox.information(self, "提示", "书架查看对话框（后续版本实现）")

    def _on_fetch_data(self) -> None:
        self._btn_fetch.setEnabled(False)
        self._btn_fetch.setText(" 获取中… ")
        self._fetch_thread = _FetchThread(self._skill, self._db, parent=self)
        self._fetch_thread.log_msg.connect(self._on_log_emitted)
        self._fetch_thread.stats_updated.connect(self._on_stats_updated)
        self._fetch_thread.done.connect(self._fetch_done)
        self._fetch_thread.start()

    def _fetch_done(self) -> None:
        self._btn_fetch.setEnabled(True)
        self._btn_fetch.setText(" 获取数据")
        self._btn_fetch.setIcon(icon_fetch(22, self._theme))
        self._refresh_reading_stats()

    def _on_refresh_stats(self) -> None:
        self._btn_refresh_stats.setEnabled(False)
        self._btn_refresh_stats.setText(" 刷新中… ")
        self._skill_refresh_thread = _SkillRefreshThread(
            self._skill, self._db, mode="stats", parent=self
        )
        self._skill_refresh_thread.log_msg.connect(self._on_log_emitted)
        self._skill_refresh_thread.stats_updated.connect(self._on_stats_updated)
        self._skill_refresh_thread.done.connect(self._on_skill_refresh_done)
        self._skill_refresh_thread.start()

    def _on_refresh_progress(self) -> None:
        if not self._current_book_id:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(
                self, "无当前书籍",
                "当前没有正在阅读的书籍。\n请先点击「开始」让调度器选书。",
            )
            return
        self._btn_refresh_progress.setEnabled(False)
        self._btn_refresh_progress.setText(" 刷新中… ")
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
        self._bar_book.setValue(max(0, min(100, int(percent))))
        self._lbl_book_progress.setText(f"进 度：{percent}%（Skill 覆盖）")

    def _on_skill_refresh_done(self) -> None:
        self._btn_refresh_stats.setEnabled(True)
        self._btn_refresh_stats.setText(" 刷新统计")
        self._btn_refresh_stats.setIcon(icon_refresh_stats(22, self._theme))
        self._btn_refresh_progress.setEnabled(True)
        self._btn_refresh_progress.setText(" 刷新进度")
        self._btn_refresh_progress.setIcon(icon_refresh_progress(22, self._theme))

    def _on_open_settings(self) -> None:
        if self._main_window:
            self._main_window.show_settings_page()


# （原 _circle_css 模块级函数：v5 起 KPI 卡不再使用圆形徽章，已移除。）
