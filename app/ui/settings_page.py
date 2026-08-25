"""配置中心（v5 · 米白纸 Paper Studio · 2×3 Grid · SpinBox ▲▼ 文字绘制）。

布局（两行三列均分）：
  📖 阅读参数    ·  🔑 Skill API Key  ·  📢 WxPusher 推送
  💻 系统设置    ·  🧹 数据维护        ·  ( 空 · 留白 )

底部横栏：「保存配置」墨黑主按钮（右对齐）

特性：
  - Skill API Key 异步验证
  - 清除 Cookie 并重启
  - SpinBox 箭头通过 QProxyStyle 绘制为 "▲" / "▼" 字符
    （完全规避 PySide6 + Fusion Style + 高 DPI 下的「边框三角形黑块」Bug）
  - 所有输入/复选/GroupBox 统一交给 styles.py QSS
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QSize, Qt, QThread, QTimer, QRect, Signal
from PySide6.QtGui import QIcon, QPainter, QPalette, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProxyStyle,
    QPushButton,
    QSpinBox,
    QStyle,
    QStyleOption,
    QStyleOptionSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.core.config import ConfigStore
from app.core.skill_api import SkillAPI
from app.core.weread_api import WeReadApi
from app.ui.icon_store import (
    app_version_display,
    icon_back_home,
    icon_config_center,
    icon_save,
    icon_verify,
)
from app.utils.autostart import is_autostart_enabled, set_autostart
from app.utils.logger import get_logger

log = get_logger(__name__)


# =========================================================================
# SpinBox ▲ / ▼ 文字箭头 ProxyStyle（根治高 DPI 下 border-triangle 黑块 Bug）
# =========================================================================
class _ArrowTextSpinBoxStyle(QProxyStyle):
    """用字符 "▲" / "▼" 画 SpinBox 上下箭头。

    PySide6 Fusion Style 在高 DPI 下会把 QSS 的 border-三角形渲染为「黑方块」。
    本 style 绕过 QSS::up-arrow / ::down-arrow，直接在对应 subControlRect 内
    以 JetBrains Mono 8pt 绘制字符，保证跨 DPI 稳定。
    """

    def __init__(self, base_style: QStyle | None = None) -> None:
        super().__init__(base_style or QApplication.style())
        self._arrow_font = QFont("JetBrains Mono")
        self._arrow_font.setPointSize(8)
        self._arrow_font.setBold(True)

    def drawComplexControl(  # noqa: N802  (Qt 接口命名)
        self,
        cc: QStyle.ComplexControl,
        opt: QStyleOption,
        p: QPainter,
        widget: QWidget | None = None,
    ) -> None:
        if cc != QStyle.ComplexControl.CC_SpinBox or not isinstance(opt, QStyleOptionSpinBox):
            super().drawComplexControl(cc, opt, p, widget)
            return

        # 先调用父级绘制背景/边框（让 styles.py 的 QSS Input 样式完全生效）
        super().drawComplexControl(cc, opt, p, widget)

        # 只重画 up/down 两个箭头的文字（覆盖 Fusion 默认画法）
        up_rect = self.subControlRect(cc, opt, QStyle.SubControl.SC_SpinBoxUp, widget)
        dn_rect = self.subControlRect(cc, opt, QStyle.SubControl.SC_SpinBoxDown, widget)

        p.save()
        try:
            p.setFont(self._arrow_font)
            # 启用状态：墨黑 / 禁用：中性灰（与 styles.py LIGHT 色板对齐）
            enabled = bool(opt.state & QStyle.StateFlag.State_Enabled)
            up_active = bool(opt.state & QStyle.StateFlag.State_UpArrow)
            dn_active = bool(opt.state & QStyle.StateFlag.State_DownArrow)
            up_color = "#111827" if (enabled and up_active) else ("#6B7280" if not enabled else "#374151")
            dn_color = "#111827" if (enabled and dn_active) else ("#6B7280" if not enabled else "#374151")

            p.setPen(up_color)
            self._draw_center_text(p, up_rect, "▲")
            p.setPen(dn_color)
            self._draw_center_text(p, dn_rect, "▼")
        finally:
            p.restore()

    @staticmethod
    def _draw_center_text(p: QPainter, rect: QRect, text: str) -> None:
        # 居中绘制（按绘制设备坐标）
        p.drawText(rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, text)


# =========================================================================
# 异步 Skill API Key 验证
# =========================================================================
class _ApiKeyChecker(QThread):
    """异步验证 Skill API Key。"""

    result = Signal(bool, str)

    def __init__(self, skill: SkillAPI) -> None:
        super().__init__()
        self._skill = skill

    def run(self) -> None:  # noqa: D401
        ok, msg = self._skill.verify_api_key()
        self.result.emit(bool(ok), str(msg))


# =========================================================================
# 配置中心页面
# =========================================================================
class SettingsPage(QWidget):
    """配置中心页面（双主题）。"""

    def __init__(
        self,
        config: ConfigStore,
        api: WeReadApi,
        skill: SkillAPI,
        main_window: QWidget | None = None,
    ) -> None:
        super().__init__()
        self._cfg = config
        self._api = api
        self._skill = skill
        self._main_window = main_window
        self._checker: _ApiKeyChecker | None = None
        self._theme: str = "light"

        self._build_ui()
        self._load_values()
        # 先按 light 给图标染色，MainWindow.apply_theme 会再同步一次
        self._apply_theme_to_icons("light")

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 20)
        root.setSpacing(14)

        # ---- 顶部：标题 + 版本文本 + 返回主界面 ----
        title_row = QHBoxLayout()
        title = QLabel("⚙️  配置中心")
        title.setObjectName("settingsTitle")
        title.setProperty("class", "h1")
        title_row.addWidget(title)
        # 版本号小胶囊（标题右、返回按钮左之间）
        version_chip = QLabel(f"v{app_version_display()}")
        version_chip.setStyleSheet(
            "color:#6B7280;padding:2px 10px;border:1px solid #E5E5DF;"
            "border-radius:999px;background:#F9F9F5;"
            "font-family:'JetBrains Mono',Consolas,monospace;font-size:11px;"
            "letter-spacing:0.5px;"
        )
        title_row.addWidget(version_chip, 0, Qt.AlignmentFlag.AlignVCenter)
        title_row.addStretch(1)
        self._btn_back = QPushButton(" 返回主界面 ")
        self._btn_back.setProperty("role", "primary")
        self._btn_back.setIconSize(QSize(20, 20))
        self._btn_back.setMinimumHeight(40)
        self._btn_back.clicked.connect(self._on_back_to_main)
        title_row.addWidget(self._btn_back)
        root.addLayout(title_row)

        # ---- 行 0：3 列（📖 阅读 / 🔑 Key / 📢 推送）----
        # ---- 行 1：2 列（💻 系统 / 🧹 数据） · 移除「🎯 说明」独立组
        #           每条说明直接写在对应组框底部（用户本次需求）
        grid = QGridLayout()
        grid.setSpacing(12)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)
        # 行 0 三列均分；行 1 改为两列各占 1.5 → 用 setColumnStretch(0..2 各 1)
        #   → 让 💻 / 🧹 各自跨 1.5 列：简单做法是设三列拉伸全为 1，
        #     然后第二行的两个 group 各自 span 1 列 + 2 列（1 宽 + 2 宽）
        #     不，这里用「2 列 stretch 比 3:3」——直接简单做法：
        #   保持 3 列拉伸 1；第二行：💻 span 1~1、🧹 span 1~2 合并 col1+col2
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)

        # --- row 0 : 📖 / 🔑 / 📢 （三列均分）---
        reading_box = self._build_reading_box()
        grid.addWidget(reading_box, 0, 0)

        skill_box = self._build_skill_box()
        grid.addWidget(skill_box, 0, 1)

        push_box = self._build_push_box()
        grid.addWidget(push_box, 0, 2)

        # --- row 1 : 💻 col 0 列 1 宽；🧹 col 1 跨 2 列合并（span=2）---
        sys_box = self._build_sys_box()
        grid.addWidget(sys_box, 1, 0)

        data_box = self._build_data_box()
        # 合并 col1 + col2，视觉宽度 ≈ 两倍 💻，避免右下留空
        grid.addWidget(data_box, 1, 1, 1, 2)

        root.addLayout(grid, 1)

        # ---- 💾 保存（右对齐）----
        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self._btn_save = QPushButton(" 保存配置 ")
        self._btn_save.setProperty("role", "primary")
        self._btn_save.setIconSize(QSize(20, 20))
        self._btn_save.setMinimumHeight(42)
        self._btn_save.clicked.connect(self._on_save)
        save_row.addWidget(self._btn_save)
        root.addLayout(save_row)

        # 安装 SpinBox 箭头 ProxyStyle（作用到本页所有 SpinBox/DoubleSpinBox）
        self._apply_spinbox_style()

    # =====================================================================
    # 6 个分组组件
    # =====================================================================
    def _build_reading_box(self) -> QGroupBox:
        reading_box = QGroupBox("📖  阅读参数")
        reading_form = QFormLayout(reading_box)
        reading_form.setSpacing(6)
        reading_form.setContentsMargins(14, 20, 14, 12)

        hours_row = QHBoxLayout()
        self._min_hours = QDoubleSpinBox()
        self._min_hours.setRange(0.5, 24.0)
        self._min_hours.setSingleStep(0.5)
        self._min_hours.setSuffix(" 小时")
        self._max_hours = QDoubleSpinBox()
        self._max_hours.setRange(0.5, 24.0)
        self._max_hours.setSingleStep(0.5)
        self._max_hours.setSuffix(" 小时")
        hours_row.addWidget(QLabel("最少"))
        hours_row.addWidget(self._min_hours, 1)
        hours_row.addWidget(QLabel("最多"))
        hours_row.addWidget(self._max_hours, 1)
        reading_form.addRow("每日时长范围：", hours_row)

        interval_row = QHBoxLayout()
        self._min_interval = QSpinBox()
        self._min_interval.setRange(5, 600)
        self._min_interval.setSuffix(" 秒")
        self._max_interval = QSpinBox()
        self._max_interval.setRange(5, 600)
        self._max_interval.setSuffix(" 秒")
        interval_row.addWidget(QLabel("下限"))
        interval_row.addWidget(self._min_interval, 1)
        interval_row.addWidget(QLabel("上限"))
        interval_row.addWidget(self._max_interval, 1)
        reading_form.addRow("单页停留范围：", interval_row)

        startup_row = QHBoxLayout()
        self._startup_min = QSpinBox()
        self._startup_min.setRange(0, 600)
        self._startup_min.setSuffix(" 秒")
        self._startup_max = QSpinBox()
        self._startup_max.setRange(0, 600)
        self._startup_max.setSuffix(" 秒")
        startup_row.addWidget(QLabel("下限"))
        startup_row.addWidget(self._startup_min, 1)
        startup_row.addWidget(QLabel("上限"))
        startup_row.addWidget(self._startup_max, 1)
        reading_form.addRow("启动延迟范围：", startup_row)

        longrest_row = QHBoxLayout()
        self._long_rest_every = QSpinBox()
        self._long_rest_every.setRange(5, 100)
        self._long_rest_every.setSuffix(" 次")
        self._long_rest_min = QSpinBox()
        self._long_rest_min.setRange(10, 600)
        self._long_rest_min.setSuffix(" 秒")
        self._long_rest_max = QSpinBox()
        self._long_rest_max.setRange(10, 600)
        self._long_rest_max.setSuffix(" 秒")
        longrest_row.addWidget(QLabel("每"))
        longrest_row.addWidget(self._long_rest_every)
        longrest_row.addWidget(QLabel("休"))
        longrest_row.addWidget(self._long_rest_min, 1)
        longrest_row.addWidget(QLabel("~"))
        longrest_row.addWidget(self._long_rest_max, 1)
        reading_form.addRow("长休息设置：", longrest_row)

        cooldown_row = QHBoxLayout()
        self._fail_cd_min = QSpinBox()
        self._fail_cd_min.setRange(0, 600)
        self._fail_cd_min.setSuffix(" 秒")
        self._fail_cd_max = QSpinBox()
        self._fail_cd_max.setRange(0, 600)
        self._fail_cd_max.setSuffix(" 秒")
        cooldown_row.addWidget(QLabel("下限"))
        cooldown_row.addWidget(self._fail_cd_min, 1)
        cooldown_row.addWidget(QLabel("上限"))
        cooldown_row.addWidget(self._fail_cd_max, 1)
        reading_form.addRow("失败冷却范围：", cooldown_row)

        # 对应「说明」Tip（原独立组右下，现已下放此框底部）
        #   Tip #1 + #2  组合在一起，对应每日目标/启动延迟 + 失败冷却
        tip = QLabel("💡 建议：默认每日目标 2 小时；启动延迟 15~25 秒；失败冷却 30~60 秒。")
        tip.setWordWrap(True)
        tip.setStyleSheet(
            "color:#6B7280;font-size:12px;line-height:1.55;"
            "padding:6px 0 0 0;margin:0;border:none;"
            "font-family:'Microsoft YaHei UI',sans-serif;"
        )
        reading_form.addRow(tip)
        return reading_box

    def _build_skill_box(self) -> QGroupBox:
        skill_box = QGroupBox("🔑  Skill API Key")
        skill_layout = QVBoxLayout(skill_box)
        skill_layout.setSpacing(8)
        skill_layout.setContentsMargins(14, 20, 14, 12)
        key_row = QHBoxLayout()
        self._api_key_input = QLineEdit()
        self._api_key_input.setPlaceholderText("wrk-xxxxxxxxxxxxxxxxxxxxxxxx")
        self._api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._btn_verify = QPushButton(" 验证")
        self._btn_verify.setProperty("role", "secondary")
        self._btn_verify.setIconSize(QSize(18, 18))
        self._btn_verify.setMinimumHeight(36)
        self._btn_verify.clicked.connect(self._on_verify_api_key)
        key_row.addWidget(self._api_key_input, 1)
        key_row.addWidget(self._btn_verify)
        skill_layout.addLayout(key_row)
        self._lbl_key_status = QLabel("状态：未验证")
        self._lbl_key_status.setProperty("class", "muted")
        skill_layout.addWidget(self._lbl_key_status)

        refresh_row = QHBoxLayout()
        refresh_row.addWidget(QLabel("Skill 数据刷新间隔："))
        self._refresh_interval = QSpinBox()
        self._refresh_interval.setRange(1, 240)
        self._refresh_interval.setSuffix(" 分钟")
        self._refresh_interval.setToolTip(
            "调度器每 N 分钟调用 Skill 覆盖阅读统计和书籍进度"
        )
        refresh_row.addWidget(self._refresh_interval)
        refresh_row.addStretch(1)
        skill_layout.addLayout(refresh_row)

        # Tip #3：长休息（写在 Skill 底部 —— 调度频率相关）
        tip = QLabel("💡 每 20 次翻页安排一次长休息（60~180 秒）。")
        tip.setWordWrap(True)
        tip.setStyleSheet(
            "color:#6B7280;font-size:12px;line-height:1.55;"
            "padding:4px 0 0 0;margin:0;border:none;"
            "font-family:'Microsoft YaHei UI',sans-serif;"
        )
        skill_layout.addWidget(tip)
        skill_layout.addStretch(1)
        return skill_box

    def _build_push_box(self) -> QGroupBox:
        push_box = QGroupBox("📢  WxPusher 推送")
        push_layout = QVBoxLayout(push_box)
        push_layout.setSpacing(8)
        push_layout.setContentsMargins(14, 20, 14, 12)
        spt_row = QHBoxLayout()
        spt_row.addWidget(QLabel("SPT:"))
        self._spt_input = QLineEdit()
        self._spt_input.setPlaceholderText("SPT_xxxxxxxxxxxx")
        spt_row.addWidget(self._spt_input, 1)
        push_layout.addLayout(spt_row)
        rules_label = QLabel("通知规则（多选）：")
        rules_label.setProperty("class", "strong")
        push_layout.addWidget(rules_label)
        self._chk_daily_start = QCheckBox("每日首次开始（包含今日目标）")
        self._chk_cookie_fail = QCheckBox("Cookie 失效通知")
        self._chk_daily_done = QCheckBox("任务完成发送")
        self._chk_login_success = QCheckBox("登录成功通知")
        push_layout.addWidget(self._chk_daily_start)
        push_layout.addWidget(self._chk_cookie_fail)
        push_layout.addWidget(self._chk_daily_done)
        push_layout.addWidget(self._chk_login_success)

        # Tip #4：推送时机 + 冷却（与失败冷却 Tip 同语义，移到这里简洁）
        tip = QLabel("💡 建议至少开启「Cookie 失效通知」，掉线后可第一时间处理。")
        tip.setWordWrap(True)
        tip.setStyleSheet(
            "color:#6B7280;font-size:12px;line-height:1.55;"
            "padding:4px 0 0 0;margin:0;border:none;"
            "font-family:'Microsoft YaHei UI',sans-serif;"
        )
        push_layout.addWidget(tip)
        push_layout.addStretch(1)
        return push_box

    def _build_sys_box(self) -> QGroupBox:
        sys_box = QGroupBox("💻  系统设置")
        sys_layout = QVBoxLayout(sys_box)
        sys_layout.setSpacing(10)
        sys_layout.setContentsMargins(14, 20, 14, 14)
        self._chk_autostart = QCheckBox("开机自启动")
        self._chk_autostart.toggled.connect(self._on_autostart_toggled)
        self._chk_minimize_tray = QCheckBox("点击关闭时最小化到托盘")
        sys_layout.addWidget(self._chk_autostart)
        sys_layout.addWidget(self._chk_minimize_tray)

        # Tip #5：书架预加载
        tip = QLabel("💡 书架仅前 10 本预加载章节池，减少启动开销。")
        tip.setWordWrap(True)
        tip.setStyleSheet(
            "color:#6B7280;font-size:12px;line-height:1.55;"
            "padding:4px 0 0 0;margin:0;border:none;"
            "font-family:'Microsoft YaHei UI',sans-serif;"
        )
        sys_layout.addWidget(tip)
        sys_layout.addStretch(1)
        return sys_box

    def _build_data_box(self) -> QGroupBox:
        data_box = QGroupBox("🧹  数据维护")
        data_layout = QVBoxLayout(data_box)
        data_layout.setSpacing(8)
        data_layout.setContentsMargins(14, 20, 14, 14)
        clean_row = QHBoxLayout()
        clean_label = QLabel("清除 Cookie 并重启程序")
        self._btn_clean = QPushButton(" 清  除 ")
        self._btn_clean.setProperty("role", "danger")
        self._btn_clean.setIconSize(QSize(18, 18))
        self._btn_clean.setMinimumHeight(38)
        self._btn_clean.clicked.connect(self._on_clean_cookie)
        clean_row.addWidget(clean_label, 1)
        clean_row.addWidget(self._btn_clean)
        data_layout.addLayout(clean_row)
        hint = QLabel("说明：清除后需重新扫码登录")
        hint.setProperty("class", "muted")
        data_layout.addWidget(hint)

        # Tip #6：调度器即时拉取
        tip = QLabel("💡 其他书籍被调度器选中时会即时拉取，无需手动刷新章节。")
        tip.setWordWrap(True)
        tip.setStyleSheet(
            "color:#6B7280;font-size:12px;line-height:1.55;"
            "padding:4px 0 0 0;margin:0;border:none;"
            "font-family:'Microsoft YaHei UI',sans-serif;"
        )
        data_layout.addWidget(tip)
        data_layout.addStretch(1)
        return data_box

    # ------------------------------------------------------------------
    # SpinBox ProxyStyle 安装
    # ------------------------------------------------------------------
    def _apply_spinbox_style(self) -> None:
        """为本页所有 QSpinBox/QDoubleSpinBox 安装 ▲▼ 文字箭头样式。"""
        try:
            style = _ArrowTextSpinBoxStyle()
        except Exception:  # noqa: BLE001 - 兜底不降崩溃
            style = None
        if style is None:
            return
        for cls in (QSpinBox, QDoubleSpinBox):
            for w in self.findChildren(cls):
                try:
                    w.setStyle(style)  # QWidget takes ownership
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------
    # 图标染色（单主题米白纸 · theme 参数兼容保留，永远按 light 染色）
    # ------------------------------------------------------------------
    def _apply_theme_to_icons(self, theme_name: str) -> None:
        # v5 起只按 light 染色（与单主题 QSS 对齐）
        self._theme = "light"
        self._btn_back.setIcon(icon_back_home(22, "light"))
        self._btn_save.setIcon(icon_save(22, "light"))
        self._btn_verify.setIcon(icon_verify(20, "light"))

    # ------------------------------------------------------------------
    # apply_theme（MainWindow 转发入口 · 单主题无操作，仅兼容保留）
    # ------------------------------------------------------------------
    def apply_theme(self, theme_name: str) -> None:
        """单主题（米白纸）：无切换动作。仅兼容 MainWindow.apply_theme 调用。"""
        self._apply_theme_to_icons("light")

    # ------------------------------------------------------------------
    # 加载当前配置
    # ------------------------------------------------------------------
    def _load_values(self) -> None:
        reading = self._cfg.get("reading", {}) or {}
        self._min_hours.setValue(float(reading.get("min_hours", 1.5)))
        self._max_hours.setValue(float(reading.get("max_hours", 3.0)))
        self._min_interval.setValue(int(reading.get("min_interval_sec", 30)))
        self._max_interval.setValue(int(reading.get("max_interval_sec", 45)))
        self._startup_min.setValue(int(reading.get("startup_delay_min_sec", 15)))
        self._startup_max.setValue(int(reading.get("startup_delay_max_sec", 25)))
        self._long_rest_every.setValue(int(reading.get("long_rest_every", 20)))
        self._long_rest_min.setValue(int(reading.get("long_rest_min_sec", 60)))
        self._long_rest_max.setValue(int(reading.get("long_rest_max_sec", 180)))
        self._fail_cd_min.setValue(int(reading.get("fail_cooldown_min_sec", 30)))
        self._fail_cd_max.setValue(int(reading.get("fail_cooldown_max_sec", 60)))

        self._api_key_input.setText(str(self._cfg.get("skill.api_key", "")))
        self._refresh_interval.setValue(int(self._cfg.get("skill.refresh_interval_min", 30)))
        self._spt_input.setText(str(self._cfg.get("push.wxpusher_spt", "")))
        self._chk_daily_start.setChecked(bool(self._cfg.get("push.notify_daily_start", True)))
        self._chk_cookie_fail.setChecked(bool(self._cfg.get("push.notify_cookie_fail", True)))
        self._chk_daily_done.setChecked(bool(self._cfg.get("push.notify_daily_done", True)))
        self._chk_login_success.setChecked(bool(self._cfg.get("push.notify_login_success", False)))

        self._chk_autostart.setChecked(is_autostart_enabled())
        self._chk_minimize_tray.setChecked(bool(self._cfg.get("app.minimize_to_tray", True)))

    # ------------------------------------------------------------------
    # 按钮槽
    # ------------------------------------------------------------------
    def _on_verify_api_key(self) -> None:
        api_key = self._api_key_input.text().strip()
        if not api_key:
            QMessageBox.warning(self, "提示", "请先输入 API Key")
            return
        # 先保存到 config（verify_api_key 会从 config 读取）
        self._cfg.set("skill.api_key", api_key, auto_save=False)
        self._cfg.save()
        self._lbl_key_status.setText("状态：⏳ 验证中...")
        self._lbl_key_status.setProperty("class", "warn")
        # 触发 QSS refresh
        self._lbl_key_status.style().unpolish(self._lbl_key_status)
        self._lbl_key_status.style().polish(self._lbl_key_status)
        self._btn_verify.setEnabled(False)
        self._btn_verify.setText(" 验证中…")

        self._checker = _ApiKeyChecker(self._skill)
        self._checker.result.connect(self._on_verify_done)
        self._checker.start()
        QTimer.singleShot(
            30000,
            lambda: (
                self._on_verify_done(False, "❌ 超时")
                if self._checker and self._checker.isRunning()
                else None
            ),
        )

    def _on_verify_done(self, ok: bool, msg: str) -> None:
        self._btn_verify.setEnabled(True)
        self._btn_verify.setText(" 验证")
        self._btn_verify.setIcon(icon_verify(20, self._theme))
        if ok:
            self._lbl_key_status.setText(f"状态：{msg}")
            self._lbl_key_status.setProperty("class", "ok")
        else:
            self._lbl_key_status.setText(f"状态：{msg}")
            self._lbl_key_status.setProperty("class", "danger")
        self._lbl_key_status.style().unpolish(self._lbl_key_status)
        self._lbl_key_status.style().polish(self._lbl_key_status)

    def _on_autostart_toggled(self, checked: bool) -> None:
        ok, msg = set_autostart(checked)
        if not ok:
            QMessageBox.warning(self, "设置失败", msg)
            self._chk_autostart.blockSignals(True)
            self._chk_autostart.setChecked(is_autostart_enabled())
            self._chk_autostart.blockSignals(False)

    def _on_clean_cookie(self) -> None:
        reply = QMessageBox.question(
            self,
            "确认清除 Cookie",
            "确定要清除 Cookie 并重启程序吗？\n\n清除后需要重新扫码登录。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        log.info("用户确认清除 Cookie 并重启")
        try:
            self._cfg.clear_cookies()
            self._cfg.set("app.pending_clear", True, auto_save=True)
            log.info("Cookie 已清除，pending_clear 已设置")
        except Exception as exc:  # noqa: BLE001
            log.error("清除 Cookie 失败：%s", exc)
            QMessageBox.critical(self, "错误", f"清除 Cookie 失败：{exc}")
            return
        QMessageBox.information(
            self, "即将重启", "Cookie 已清除，程序将在 3 秒后自动重启。"
        )
        QTimer.singleShot(3000, self._restart_app)

    def _restart_app(self) -> None:
        """通过 QProcess::startDetached 重启程序。"""
        exe = Path(sys.executable)
        if exe.name.lower() in ("python.exe", "pythonw.exe"):
            main_py = Path(__file__).resolve().parents[2] / "main.py"
            pythonw = exe.with_name("pythonw.exe")
            runner = pythonw if pythonw.exists() else exe
            QProcess.startDetached(str(runner), [str(main_py)])
        else:
            QProcess.startDetached(str(exe), [])
        qapp = QApplication.instance()
        if qapp is not None:
            qapp.quit()
        sys.exit(0)

    def _on_back_to_main(self) -> None:
        """返回主界面。"""
        if self._main_window and hasattr(self._main_window, "show_main_page"):
            self._main_window.show_main_page()

    def _on_save(self) -> None:
        """保存所有设置到 config.json。"""
        try:
            self._cfg.set("reading.min_hours", self._min_hours.value(), auto_save=False)
            self._cfg.set("reading.max_hours", self._max_hours.value(), auto_save=False)
            self._cfg.set(
                "reading.min_interval_sec", self._min_interval.value(), auto_save=False
            )
            self._cfg.set(
                "reading.max_interval_sec", self._max_interval.value(), auto_save=False
            )
            self._cfg.set(
                "reading.startup_delay_min_sec",
                self._startup_min.value(),
                auto_save=False,
            )
            self._cfg.set(
                "reading.startup_delay_max_sec",
                self._startup_max.value(),
                auto_save=False,
            )
            self._cfg.set(
                "reading.long_rest_every", self._long_rest_every.value(), auto_save=False
            )
            self._cfg.set(
                "reading.long_rest_min_sec",
                self._long_rest_min.value(),
                auto_save=False,
            )
            self._cfg.set(
                "reading.long_rest_max_sec",
                self._long_rest_max.value(),
                auto_save=False,
            )
            self._cfg.set(
                "reading.fail_cooldown_min_sec",
                self._fail_cd_min.value(),
                auto_save=False,
            )
            self._cfg.set(
                "reading.fail_cooldown_max_sec",
                self._fail_cd_max.value(),
                auto_save=False,
            )
            self._cfg.set(
                "skill.api_key",
                self._api_key_input.text().strip(),
                auto_save=False,
            )
            self._cfg.set(
                "skill.refresh_interval_min",
                self._refresh_interval.value(),
                auto_save=False,
            )
            self._cfg.set(
                "push.wxpusher_spt",
                self._spt_input.text().strip(),
                auto_save=False,
            )
            self._cfg.set(
                "push.notify_daily_start",
                self._chk_daily_start.isChecked(),
                auto_save=False,
            )
            self._cfg.set(
                "push.notify_cookie_fail",
                self._chk_cookie_fail.isChecked(),
                auto_save=False,
            )
            self._cfg.set(
                "push.notify_daily_done",
                self._chk_daily_done.isChecked(),
                auto_save=False,
            )
            self._cfg.set(
                "push.notify_login_success",
                self._chk_login_success.isChecked(),
                auto_save=False,
            )
            self._cfg.set(
                "app.minimize_to_tray",
                self._chk_minimize_tray.isChecked(),
                auto_save=True,
            )
            log.info("配置中心：保存成功")
            QMessageBox.information(self, "成功", "设置已保存 ✅")
            self._on_back_to_main()
        except Exception as exc:  # noqa: BLE001
            log.error("保存设置失败：%s", exc)
            QMessageBox.critical(self, "错误", f"保存失败：{exc}")
