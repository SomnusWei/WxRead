"""设置页：时长范围、请求间隔范围、WxPusher SPT、开机自启、最小化托盘开关。"""
from __future__ import annotations

import sys
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.core.config import ConfigStore
from app.core.weread_api import WeReadApi
from app.core.weread_skills import WeReadSkills
from app.utils.autostart import is_autostart_enabled, set_autostart
from app.utils.logger import get_logger

log = get_logger(__name__)


def _form_spin(min_v: int, max_v: int, value: int, *, suffix: str = "") -> QSpinBox:
    sb = QSpinBox()
    sb.setRange(min_v, max_v)
    sb.setValue(value)
    if suffix:
        sb.setSuffix(suffix)
    # SIZING CONTRACT v2（和 groupbox_qss 完全对齐，避免代码层 & QSS 层互相打架）：
    #   outer 40px = content 38px + border 1+1
    #   up/down button = 宽 26 × 高 19（38/2，能容纳 125%/150% DPI 字体基线）
    sb.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    sb.setStyleSheet(
        "QSpinBox{"
        "  border:1px solid #dfe4ef; border-radius:6px;"
        "  padding:0 42px 0 12px;"
        "  background:#ffffff; color:#1f2a40; selection-background-color:#2d6cdf;"
        "  selection-color:#ffffff;"
        "  min-height:38px; max-height:38px;"
        "}"
        "QSpinBox:disabled{background:#f6f8fc; color:#9aa4bb;}"
        "QSpinBox::up-button{"
        "  subcontrol-origin: content; subcontrol-position: top right;"
        "  width:26px; height:19px; min-width:26px; min-height:19px;"
        "  border:none; background:transparent;"
        "  border-left:1px solid #e5eaf2; border-bottom:1px solid #eef2f8;"
        "  border-top-right-radius:5px;"
        "}"
        "QSpinBox::down-button{"
        "  subcontrol-origin: content; subcontrol-position: bottom right;"
        "  width:26px; height:19px; min-width:26px; min-height:19px;"
        "  border:none; background:transparent;"
        "  border-left:1px solid #e5eaf2; border-top:1px solid #eef2f8;"
        "  border-bottom-right-radius:5px;"
        "}"
        "QSpinBox::up-button:hover, QSpinBox::down-button:hover{background:#eef2f8;}"
        "QSpinBox::up-arrow, QSpinBox::down-arrow{"
        "  width:9px; height:9px; background:transparent;"
        "}"
        "QSpinBox::up-arrow{"
        "  border-left:4px solid transparent;"
        "  border-right:4px solid transparent;"
        "  border-bottom:5px solid #4a5675;"
        "}"
        "QSpinBox::down-arrow{"
        "  border-left:4px solid transparent;"
        "  border-right:4px solid transparent;"
        "  border-top:5px solid #4a5675;"
        "}"
        "QSpinBox::up-button:hover QSpinBox::up-arrow{border-bottom-color:#2d6cdf;}"
        "QSpinBox::down-button:hover QSpinBox::down-arrow{border-top-color:#2d6cdf;}"
        "QSpinBox:disabled QSpinBox::up-arrow{border-bottom-color:#b5bccf;}"
        "QSpinBox:disabled QSpinBox::down-arrow{border-top-color:#b5bccf;}"
    )
    # textMargins(2, 2, 0, 0)：让数字在 40px 高的框里更居中（微软雅黑中文基线偏上）
    try:
        sb.setTextMargins(2, 2, 0, 0)
    except Exception:  # PySide6 < 6.3
        pass
    sb.setMinimumHeight(40)
    sb.setMaximumHeight(40)
    return sb


class SettingsPage(QWidget):
    settings_changed = Signal()
    verify_requested = Signal()  # 让主窗口发起"立即检测登录态"
    _test_push_done = Signal(bool)
    _append_verify_skill_result = Signal(bool, str)

    def __init__(
        self,
        api: WeReadApi,
        config: ConfigStore | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._api = api
        self._cfg = config or ConfigStore()
        self._dirty = False
        self._test_push_done.connect(self._handle_test_push_result)
        self._append_verify_skill_result.connect(self._handle_verify_skill_result)
        self._build_ui()
        self._load_from_config()

    # ----------------- UI -----------------
    def _build_ui(self) -> None:
        # —— 把内容整体包进 QScrollArea：即使 DPI=125%/150% + 新增 Skill 区块后也能滚动，
        #    不会把 SpinBox/CheckBox/按钮硬压缩到尺寸下限导致文字裁切（经验 816112/100017565）
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setObjectName("SettingsScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "#SettingsScrollArea{background:#f6f8fc;border:none;}"
            "#SettingsScrollArea > QWidget > QWidget{background:#f6f8fc;}"
        )

        inner = QWidget()
        inner.setObjectName("SettingsInner")
        inner.setStyleSheet("#SettingsInner{background:#f6f8fc;}")
        root = QVBoxLayout(inner)
        root.setContentsMargins(24, 20, 24, 28)
        root.setSpacing(20)

        # Root-level container scope: explicit background + default label color
        # so child widgets never fall back to the generic "QWidget" wildcard rule.
        self.setObjectName("SettingsPageRoot")
        self.setStyleSheet(
            "#SettingsPageRoot{background:#f6f8fc;}"
            "#SettingsPageRoot > QLabel{color:#2f3b52;}"
        )
        # Size policy: generous vertical minimum so enlarged controls do not
        # clip at the page level on 100% / 125% / 150% DPI.
        self.setMinimumHeight(760)

        title = QLabel("⚙️ 个性化设置")
        title.setStyleSheet("font-size:18px;font-weight:600;color:#2f3b52;")
        root.addWidget(title)

        # Single canonical QGroupBox style block. Enumerate every child
        # control that can accept user edits / paint text so they all have
        # stable, non-negotiable geometry.
        #
        # SIZING CONTRACT v2（single source of truth, 经验 100011018 / 100017565 / 649815）：
        #   所有控件高度统一上调到"能容纳系统 125%/150% DPI 缩放默认字体"的档位：
        #   QSpinBox/QLineEdit/QComboBox  -> 40px outer (inner 38 + border 1+1)
        #   QCheckBox                     -> 34px outer, indicator 20x20
        #   QPushButton inside groupbox   -> 38px outer (inner 36)
        #   QGroupBox QLabel              -> min-height 28px（避免长 WordWrap 被截断）
        #   FormLayout margins 24/30/24/24; verticalSpacing=22
        #   SpinBox buttons 19px high; QComboBox drop-down 38px
        groupbox_qss = (
            "QGroupBox{"
            "  font-weight:600; color:#2f3b52; font-size:13px;"
            "  border:1px solid #e5eaf2; border-radius:10px;"
            "  margin-top:14px; background:#ffffff;"
            "}"
            "QGroupBox::title{"
            "  subcontrol-origin: margin; left:12px; padding:0 8px;"
            "  color:#2f3b52; background:transparent; font-weight:600;"
            "}"
            "QGroupBox QLabel{color:#2f3b52; background:#ffffff; min-height:28px; padding:4px 0;}"
            "QGroupBox QLabel:disabled{color:#9aa4bb; background:#ffffff;}"
            "QGroupBox QLineEdit, QGroupBox QComboBox{"
            "  border:1px solid #dfe4ef; border-radius:6px;"
            "  padding:0 12px; background:#ffffff; color:#1f2a40;"
            "  selection-background-color:#2d6cdf; selection-color:#ffffff;"
            "  min-height:38px; max-height:38px;"
            "}"
            "QGroupBox QLineEdit:focus, QGroupBox QComboBox:focus{"
            "  border:1px solid #2d6cdf;"
            "}"
            "QGroupBox QLineEdit:disabled, QGroupBox QComboBox:disabled{"
            "  background:#f6f8fc; color:#8a95a8;"
            "}"
            "QGroupBox QLineEdit::placeholder, QGroupBox QLineEdit:placeholder-text{"
            "  color:#9aa4bb;"
            "}"
            "QGroupBox QSpinBox{"
            "  border:1px solid #dfe4ef; border-radius:6px;"
            "  padding:0 42px 0 12px; background:#ffffff; color:#1f2a40;"
            "  selection-background-color:#2d6cdf; selection-color:#ffffff;"
            "  min-height:38px; max-height:38px;"
            "}"
            "QGroupBox QSpinBox:disabled{background:#f6f8fc; color:#9aa4bb;}"
            "QGroupBox QCheckBox{"
            "  color:#2f3b52; background:#ffffff; spacing:12px;"
            "  padding:4px 2px; min-height:30px; max-height:30px;"
            "}"
            "QGroupBox QCheckBox::indicator{width:20px; height:20px;}"
            "QGroupBox QCheckBox:disabled{color:#8a95a8; background:#ffffff;}"
            "QGroupBox QPushButton{"
            "  color:#2f3b52; min-height:36px; max-height:36px; padding:0 16px; font-size:12px;"
            "}"
            "QGroupBox QPushButton:disabled{color:#8a95a8;}"
            "QGroupBox QSpinBox::up-button{"
            "  subcontrol-origin: content; subcontrol-position: top right;"
            "  width:26px; height:19px; min-width:26px; min-height:19px;"
            "  border:none; background:transparent;"
            "  border-left:1px solid #e5eaf2; border-bottom:1px solid #eef2f8;"
            "  border-top-right-radius:5px;"
            "}"
            "QGroupBox QSpinBox::down-button{"
            "  subcontrol-origin: content; subcontrol-position: bottom right;"
            "  width:26px; height:19px; min-width:26px; min-height:19px;"
            "  border:none; background:transparent;"
            "  border-left:1px solid #e5eaf2; border-top:1px solid #eef2f8;"
            "  border-bottom-right-radius:5px;"
            "}"
            "QGroupBox QSpinBox::up-arrow, QGroupBox QSpinBox::down-arrow{"
            "  width:9px; height:9px; background:transparent;"
            "}"
            "QGroupBox QSpinBox::up-arrow{"
            "  border-left:4px solid transparent;"
            "  border-right:4px solid transparent;"
            "  border-bottom:5px solid #4a5675;"
            "}"
            "QGroupBox QSpinBox::down-arrow{"
            "  border-left:4px solid transparent;"
            "  border-right:4px solid transparent;"
            "  border-top:5px solid #4a5675;"
            "}"
            "QGroupBox QSpinBox::up-button:hover QGroupBox QSpinBox::up-arrow{border-bottom-color:#2d6cdf;}"
            "QGroupBox QSpinBox::down-button:hover QGroupBox QSpinBox::down-arrow{border-top-color:#2d6cdf;}"
            "QGroupBox QSpinBox:disabled QGroupBox QSpinBox::up-arrow{border-bottom-color:#b5bccf;}"
            "QGroupBox QSpinBox:disabled QGroupBox QSpinBox::down-arrow{border-top-color:#b5bccf;}"
            "QGroupBox QComboBox::drop-down{width:26px; height:38px; border:none;}"
        )

        scroll.setWidget(inner)
        outer.addWidget(scroll)

        # 阅读设置组 -----------------------------------------------------------
        grp_read = QGroupBox("阅读节奏")
        grp_read.setStyleSheet(groupbox_qss)
        form_read = QFormLayout(grp_read)
        form_read.setContentsMargins(24, 30, 24, 24)
        form_read.setHorizontalSpacing(14)
        form_read.setVerticalSpacing(22)
        form_read.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form_read.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self._min_hours = _form_spin(0, 23, 8, suffix=" 小时")
        self._max_hours = _form_spin(1, 24, 10, suffix=" 小时")
        self._min_hours.valueChanged.connect(self._sync_max_lower_bound)
        self._max_hours.valueChanged.connect(self._sync_min_upper_bound)
        form_read.addRow("每日最少阅读时长：", self._min_hours)
        form_read.addRow("每日最多阅读时长：", self._max_hours)

        self._interval_min = _form_spin(10, 120, 25, suffix=" 秒")
        self._interval_max = _form_spin(10, 300, 45, suffix=" 秒")
        self._interval_min.valueChanged.connect(self._sync_interval)
        self._interval_max.valueChanged.connect(self._sync_interval)
        form_read.addRow("单页停留下限：", self._interval_min)
        form_read.addRow("单页停留上限：", self._interval_max)

        # 登录态健康巡检（阅读过程中自动跑 chapter_sync / shelf_sync 并推送告警）
        tip_health = QLabel("阅读过程中会自动检测登录态；连续失败 2 次时通过 WxPusher 推送。")
        tip_health.setStyleSheet(
            "color:#7a879f;font-size:12px;padding:0 0 4px;"
        )
        tip_health.setWordWrap(True)
        form_read.addRow(self._make_form_spacer(), tip_health)
        self._health_first_min = _form_spin(1, 60, 3, suffix=" 分钟")
        self._health_min = _form_spin(3, 240, 12, suffix=" 分钟")
        form_read.addRow("首次巡检延迟：", self._health_first_min)
        form_read.addRow("之后每隔：", self._health_min)

        root.addWidget(grp_read)

        # 推送 -----------------------------------------------------------------
        grp_push = QGroupBox("消息推送（WxPusher）")
        grp_push.setStyleSheet(groupbox_qss)
        form_push = QFormLayout(grp_push)
        form_push.setContentsMargins(24, 30, 24, 24)
        form_push.setHorizontalSpacing(14)
        form_push.setVerticalSpacing(22)
        form_push.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form_push.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self._wxpusher_spt = QLineEdit()
        self._wxpusher_spt.setPlaceholderText(
            "请填入 WxPusher 极简推送 SPT（留空则不推送）"
        )
        # NOTE: _wxpusher_spt sits INSIDE grp_push, which has just had a
        #   QGroupBox-level stylesheet applied.  Apply size constraints LAST
        #   so the groupbox-level QSS can't clobber them via implicit
        #   sizeHint reflow (经验 100017565 — Qt can re-raise minHeight on
        #   descendants after parent stylesheet resolves).
        self._wxpusher_spt.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._wxpusher_spt.setTextMargins(12, 3, 12, 3)
        self._wxpusher_spt.setMinimumHeight(40)
        self._wxpusher_spt.setMaximumHeight(40)
        form_push.addRow("WxPusher SPT：", self._wxpusher_spt)

        self._chk_cookie_fail = QCheckBox("Cookie 失效时提醒我重新登录")
        self._chk_daily = QCheckBox("每天任务完成时推送一条结果")
        self._btn_test_push = QPushButton("发送一条测试消息")
        self._btn_test_push.setStyleSheet(self._btn_secondary())
        self._btn_test_push.clicked.connect(self._on_test_push)
        for w in (self._chk_cookie_fail, self._chk_daily):
            w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            w.setMinimumHeight(34)
            w.setMaximumHeight(34)
        self._btn_test_push.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self._btn_test_push.setMinimumWidth(140)
        self._btn_test_push.setMinimumHeight(38)
        self._btn_test_push.setMaximumHeight(38)

        # Align checkboxes/buttons to the field column (left spacer in label col)
        form_push.addRow(self._make_form_spacer(), self._chk_cookie_fail)
        form_push.addRow(self._make_form_spacer(), self._chk_daily)
        form_push.addRow(self._make_form_spacer(), self._btn_test_push)

        root.addWidget(grp_push)

        # 微信读书 Skill（weread-skills） -----------------------------------------
        grp_skills = QGroupBox("微信读书官方 Skill（获取阅读时长/书名/进度）")
        grp_skills.setStyleSheet(groupbox_qss)
        form_skills = QFormLayout(grp_skills)
        form_skills.setContentsMargins(24, 30, 24, 24)
        form_skills.setHorizontalSpacing(14)
        form_skills.setVerticalSpacing(22)
        form_skills.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form_skills.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        tip_skill = QLabel(
            "使用方法：访问 🔗 weread.qq.com/r/weread-skills 扫码获取以 wrk- 开头的专属 API Key，"
            "粘贴到下方并保存即可。系统在阅读过程中，每成功阅读 N 次就会通过官方 Skill 接口"
            "（POST i.weread.qq.com/api/agent/gateway，内置直连，**完全不需要** 执行 "
            "`npx skills add Tencent/WeChatReading -g` 或任何安装命令）同步今日时长、"
            "书名、作者和书籍进度，保证数据和微信读书 App 一致。"
        )
        tip_skill.setStyleSheet("color:#7a879f;font-size:12px;padding:2px 0 2px;")
        tip_skill.setWordWrap(True)
        form_skills.addRow(self._make_form_spacer(), tip_skill)

        self._skill_api_key = QLineEdit()
        self._skill_api_key.setPlaceholderText("请输入以 wrk- 开头的 Skill API Key（留空则不启用）")
        self._skill_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._skill_api_key.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._skill_api_key.setTextMargins(12, 3, 12, 3)
        self._skill_api_key.setMinimumHeight(40)
        self._skill_api_key.setMaximumHeight(40)

        row_key = QHBoxLayout()
        row_key.setSpacing(10)
        self._btn_skill_toggle = QPushButton("显示")
        self._btn_skill_toggle.setStyleSheet(self._btn_secondary())
        self._btn_skill_toggle.setMinimumWidth(68)
        self._btn_skill_toggle.setMinimumHeight(38)
        self._btn_skill_toggle.setMaximumHeight(38)
        self._btn_skill_toggle.clicked.connect(self._on_toggle_skill_key_visible)
        self._btn_skill_verify = QPushButton("验证 Key 有效性")
        self._btn_skill_verify.setStyleSheet(self._btn_secondary())
        self._btn_skill_verify.setMinimumWidth(130)
        self._btn_skill_verify.setMinimumHeight(38)
        self._btn_skill_verify.setMaximumHeight(38)
        self._btn_skill_verify.clicked.connect(self._on_verify_skill_key)
        row_key.addWidget(self._skill_api_key, 1)
        row_key.addWidget(self._btn_skill_toggle)
        row_key.addWidget(self._btn_skill_verify)
        form_skills.addRow("Skill API Key：", row_key)

        self._skill_refresh_n = _form_spin(1, 500, 10, suffix=" 次成功阅读")
        tip_refresh = QLabel("1 次约等于 30 秒；默认 10 次 ≈ 每 5 分钟同步一次。")
        tip_refresh.setStyleSheet("color:#93a0b8;font-size:12px;padding:2px 0 2px;")
        tip_refresh.setWordWrap(True)
        form_skills.addRow("每：", self._skill_refresh_n)
        form_skills.addRow(self._make_form_spacer(), tip_refresh)

        root.addWidget(grp_skills)

        # 应用 -----------------------------------------------------------------
        grp_app = QGroupBox("应用")
        grp_app.setStyleSheet(groupbox_qss)
        form_app = QFormLayout(grp_app)
        form_app.setContentsMargins(24, 30, 24, 24)
        form_app.setHorizontalSpacing(14)
        form_app.setVerticalSpacing(22)
        form_app.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form_app.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self._chk_autostart = QCheckBox("开机自启动（启动后最小化到右下角任务栏）")
        self._chk_tray = QCheckBox("点击关闭按钮时最小化到托盘（不退出程序）")
        self._chk_start_min = QCheckBox("启动后直接最小化到托盘")
        self._chk_start_max = QCheckBox("启动后窗口默认最大化（推荐）")
        self._btn_verify = QPushButton("立即检测当前登录态是否有效")
        self._btn_verify.setStyleSheet(self._btn_secondary())
        self._btn_verify.clicked.connect(self.verify_requested.emit)
        for w in (
            self._chk_autostart, self._chk_tray, self._chk_start_min, self._chk_start_max,
        ):
            w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            w.setMinimumHeight(34)
            w.setMaximumHeight(34)
        self._btn_verify.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self._btn_verify.setMinimumWidth(190)
        self._btn_verify.setMinimumHeight(38)
        self._btn_verify.setMaximumHeight(38)

        form_app.addRow(self._make_form_spacer(), self._chk_autostart)
        form_app.addRow(self._make_form_spacer(), self._chk_tray)
        form_app.addRow(self._make_form_spacer(), self._chk_start_min)
        form_app.addRow(self._make_form_spacer(), self._chk_start_max)
        form_app.addRow(self._make_form_spacer(), self._btn_verify)

        root.addWidget(grp_app)

        # 底部按钮
        root.addStretch(1)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._btn_save = QPushButton("💾 保存设置")
        self._btn_save.setStyleSheet(self._btn_primary())
        self._btn_save.clicked.connect(self._on_save)
        self._btn_save.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self._btn_save.setMinimumHeight(38)
        self._btn_save.setMaximumHeight(38)
        self._btn_save.setMinimumWidth(160)
        btn_row.addWidget(self._btn_save)
        root.addLayout(btn_row)

    @staticmethod
    def _make_form_spacer() -> QWidget:
        """Zero-size left column widget used for checkboxes/buttons rows.

        Using a real widget keeps QFormLayout horizontal alignment stable
        (avoids creating invisible QLabel placeholders that can get their
        palette hit by wildcard styles on some Qt versions / DPI settings).
        """
        w = QWidget()
        w.setFixedWidth(0)
        w.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        return w

    @staticmethod
    def _btn_primary() -> str:
        return (
            # Primary "保存设置" button sits in root QVBoxLayout outside any
            # groupbox so it gets its own sizing.  Match the secondary
            # button outer-height contract (38px) for visual symmetry.
            "QPushButton{background:#2d6cdf;color:#fff;border:none;border-radius:8px;"
            "padding:0 20px;font-size:13px;font-weight:600;"
            "min-height:38px;max-height:38px;}"
            "QPushButton:hover{background:#265bc0;}"
        )

    @staticmethod
    def _btn_secondary() -> str:
        return (
            # Sizing contract v2：outer 38 = inner 36 (no border here)
            "QPushButton{"
            "background:#eef2f8;color:#2f3b52;border:none;border-radius:8px;"
            "padding:0 14px;font-size:12px;"
            "min-height:38px;max-height:38px;"
            "}"
            "QPushButton:hover{background:#e2e8f4;}"
        )

    # ----------------- Sync bounds -----------------
    def _sync_max_lower_bound(self, v: int) -> None:
        if self._max_hours.value() < v:
            self._max_hours.setValue(v)

    def _sync_min_upper_bound(self, v: int) -> None:
        if self._min_hours.value() > v:
            self._min_hours.setValue(v)

    def _sync_interval(self, *_: Any) -> None:
        lo = self._interval_min.value()
        hi = self._interval_max.value()
        if lo > hi:
            sender = self.sender()
            if sender is self._interval_min:
                self._interval_max.setValue(lo)
            else:
                self._interval_min.setValue(hi)

    # ----------------- Load / Save -----------------
    def _load_from_config(self) -> None:
        reading = self._cfg.get("reading", {}) or {}
        self._min_hours.setValue(int(reading.get("min_hours", 8)))
        self._max_hours.setValue(int(reading.get("max_hours", 10)))
        self._interval_min.setValue(int(reading.get("min_interval_sec", 25)))
        self._interval_max.setValue(int(reading.get("max_interval_sec", 45)))
        self._health_first_min.setValue(int(reading.get("health_check_first_min", 3)))
        self._health_min.setValue(int(reading.get("health_check_min", 12)))

        push = self._cfg.get("push", {}) or {}
        self._wxpusher_spt.setText(str(push.get("wxpusher_spt", "") or ""))
        self._chk_cookie_fail.setChecked(bool(push.get("notify_cookie_fail", True)))
        self._chk_daily.setChecked(bool(push.get("notify_daily_done", True)))

        app = self._cfg.get("app", {}) or {}
        # 开机自启优先取注册表真实状态
        self._chk_autostart.setChecked(is_autostart_enabled())
        self._chk_tray.setChecked(bool(app.get("minimize_to_tray", True)))
        self._chk_start_min.setChecked(bool(app.get("start_minimized", False)))
        self._chk_start_max.setChecked(bool(app.get("start_maximized", True)))

        skills = self._cfg.get("weread_skills", {}) or {}
        self._skill_api_key.setText(str(skills.get("api_key") or ""))
        self._skill_refresh_n.setValue(max(1, min(500, int(skills.get("refresh_every_n_reads") or 10))))

    def _on_toggle_skill_key_visible(self) -> None:
        cur = self._skill_api_key.echoMode()
        if cur == QLineEdit.EchoMode.Password:
            self._skill_api_key.setEchoMode(QLineEdit.EchoMode.Normal)
            self._btn_skill_toggle.setText("隐藏")
        else:
            self._skill_api_key.setEchoMode(QLineEdit.EchoMode.Password)
            self._btn_skill_toggle.setText("显示")

    def _on_verify_skill_key(self) -> None:
        """调 Skill /_list：验证 API Key 有效性，顺便打印可支持的能力。"""
        key = self._skill_api_key.text().strip()
        if not key:
            QMessageBox.information(self, "缺少 Key", "请先粘贴 Skill API Key 再验证。")
            return
        if not key.startswith("wrk-"):
            ok = QMessageBox.question(
                self, "格式提示",
                f"当前 Key 不以 wrk- 开头，仍要尝试验证吗？\n前缀：{key[:16]}…"
            )
            if ok != QMessageBox.StandardButton.Yes:
                return
        # 先保存（避免用户填了新 Key 但未保存，验证用的是旧配置）
        self._cfg.set("weread_skills.api_key", key)
        self._btn_skill_verify.setEnabled(False)
        self._btn_skill_verify.setText("验证中...")

        import threading as _t

        def _task() -> None:
            try:
                skills = WeReadSkills(self._cfg)
                res = skills.list_capabilities()
                ok = bool(res.get("ok"))
                err = res.get("error")
                http = res.get("http")
                data = res.get("data")
                caps: list[str] = []
                if isinstance(data, dict):
                    # 常见字段：capabilities / api_names / list
                    for bucket in ("capabilities", "api_names", "apis", "list"):
                        v = data.get(bucket)
                        if isinstance(v, list):
                            caps = [str(x) for x in v if x is not None]
                            break
                    if not caps and isinstance(data.get("apis"), dict):
                        caps = [f"{k}" for k in list(data.get("apis").keys())[:20]]  # type: ignore[union-attr]
                    if not caps and isinstance(data.get("capabilities"), dict):
                        caps = [f"{k}" for k in list(data.get("capabilities").keys())[:20]]  # type: ignore[union-attr]
                caps_text = "、".join(caps[:12]) + ("…" if len(caps) > 12 else "") if caps else "(未返回能力列表)"
                if ok:
                    self._append_verify_skill_result.emit(
                        True,
                        f"✅ Key 有效（HTTP={http}）。当前 Skill 支持能力：{caps_text}",
                    )
                else:
                    self._append_verify_skill_result.emit(
                        False,
                        f"❌ Key 无效：{err or f'HTTP={http}'}。请重新到 weread.qq.com/r/weread-skills 获取。",
                    )
            except Exception as exc:  # noqa: BLE001
                self._append_verify_skill_result.emit(False, f"❌ 验证异常：{exc}")

        _t.Thread(target=_task, daemon=True).start()

    def _handle_verify_skill_result(self, ok: bool, msg: str) -> None:  # noqa: FBT001
        self._btn_skill_verify.setEnabled(True)
        self._btn_skill_verify.setText("验证 Key 有效性")
        if ok:
            QMessageBox.information(self, "Skill Key 有效", msg)
        else:
            QMessageBox.warning(self, "Skill Key 无效", msg)

    def _on_save(self) -> None:
        # 校验
        if self._min_hours.value() > self._max_hours.value():
            QMessageBox.warning(self, "设置错误", "每日最少时长不能大于最多时长")
            return
        if self._interval_min.value() > self._interval_max.value():
            QMessageBox.warning(self, "设置错误", "单页停留下限不能大于上限")
            return

        self._cfg.update_dict(
            "reading",
            {
                "min_hours": self._min_hours.value(),
                "max_hours": self._max_hours.value(),
                "min_interval_sec": self._interval_min.value(),
                "max_interval_sec": self._interval_max.value(),
                "health_check_first_min": int(self._health_first_min.value()),
                "health_check_min": int(self._health_min.value()),
            },
        )
        self._cfg.update_dict(
            "push",
            {
                "wxpusher_spt": self._wxpusher_spt.text().strip(),
                "notify_cookie_fail": self._chk_cookie_fail.isChecked(),
                "notify_daily_done": self._chk_daily.isChecked(),
            },
        )
        # 开机自启
        ok, msg = set_autostart(self._chk_autostart.isChecked())
        if not ok and self._chk_autostart.isChecked():
            QMessageBox.warning(self, "开机自启设置失败", msg)

        self._cfg.update_dict(
            "app",
            {
                "auto_start": self._chk_autostart.isChecked(),
                "minimize_to_tray": self._chk_tray.isChecked(),
                "start_minimized": self._chk_start_min.isChecked(),
                "start_maximized": self._chk_start_max.isChecked(),
            },
        )
        self._cfg.update_dict(
            "weread_skills",
            {
                "api_key": self._skill_api_key.text().strip(),
                "refresh_every_n_reads": max(1, min(500, int(self._skill_refresh_n.value()))),
            },
        )
        self.settings_changed.emit()
        QMessageBox.information(self, "保存成功", "设置已生效 ✅")

    # ----------------- 测试推送 -----------------
    def _on_test_push(self) -> None:
        spt = self._wxpusher_spt.text().strip()
        if not spt:
            QMessageBox.information(self, "缺少 SPT", "请先填入 WxPusher SPT 再测试")
            return
        # 先保存再发送
        self._cfg.set("push.wxpusher_spt", spt)
        from app.core.notifier import WxPusherNotifier

        notifier = WxPusherNotifier(self._cfg)
        self._btn_test_push.setEnabled(False)
        self._btn_test_push.setText("发送中...")

        def _on_done(ok: bool) -> None:  # noqa: FBT001
            self._test_push_done.emit(bool(ok))

        notifier.send_async("【微信读书助手】测试消息：推送通道正常～", on_done=_on_done)

    def _handle_test_push_result(self, ok: bool) -> None:  # noqa: FBT001
        self._btn_test_push.setEnabled(True)
        self._btn_test_push.setText("发送一条测试消息")
        if ok:
            QMessageBox.information(self, "测试发送", "测试消息已发出，请在微信中查看 ✅")
        else:
            QMessageBox.warning(
                self,
                "测试发送失败",
                "消息未成功送达，请检查 SPT 是否正确，或等待重试后查看。",
            )
