"""设置页（v2）。

布局参考方案 3.1：
  📖 阅读参数 / 🔑 Skill API Key / 📢 WxPusher / 💻 系统设置 / 🧹 数据维护 / 💾 保存

特性：
  - Skill API Key 验证按钮（QThread 异步）
  - 清除 Cookie 并重启按钮（QProcess::startDetached）
  - 所有配置变更即时写入 config.json
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Qt, QProcess, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
    QFormLayout,
)
from PySide6.QtGui import QFont

from app.core.config import ConfigStore
from app.core.skill_api import SkillAPI
from app.core.weread_api import WeReadApi
from app.utils.autostart import is_autostart_enabled, set_autostart
from app.utils.logger import get_logger

log = get_logger(__name__)


class _ApiKeyChecker(QThread):
    """异步验证 Skill API Key。"""
    result = Signal(bool, str)

    def __init__(self, skill: SkillAPI) -> None:
        super().__init__()
        self._skill = skill

    def run(self) -> None:  # noqa: D401
        ok, msg = self._skill.verify_api_key()
        self.result.emit(ok, msg)


class SettingsPage(QWidget):
    """设置页。"""

    def __init__(
        self,
        config: ConfigStore,
        api: WeReadApi,
        skill: SkillAPI,
        main_window: "QWidget | None" = None,
    ) -> None:
        super().__init__()
        self._cfg = config
        self._api = api
        self._skill = skill
        self._main_window = main_window
        self._checker: _ApiKeyChecker | None = None
        self._build_ui()
        self._load_values()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(16)

        title_row = QHBoxLayout()
        title = QLabel("⚙️ 设置")
        title.setStyleSheet("font-size:18px;font-weight:700;color:#2f3b52;")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self._btn_back = QPushButton("🏠 返回主界面")
        self._btn_back.setStyleSheet(
            "QPushButton{background:#2d6cdf;color:#fff;border:none;border-radius:8px;"
            "padding:6px 16px;font-size:12px;font-weight:600;}"
            "QPushButton:hover{background:#2257b8;}"
        )
        self._btn_back.clicked.connect(self._on_back_to_main)
        title_row.addWidget(self._btn_back)
        root.addLayout(title_row)

        # ===== 📖 阅读参数 =====
        reading_box = QGroupBox("📖 阅读参数")
        reading_form = QFormLayout(reading_box)

        # 每日时长范围
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
        hours_row.addWidget(self._min_hours)
        hours_row.addWidget(QLabel("最多"))
        hours_row.addWidget(self._max_hours)
        hours_row.addStretch(1)
        reading_form.addRow("每日时长范围：", hours_row)

        # 单页停留范围
        interval_row = QHBoxLayout()
        self._min_interval = QSpinBox()
        self._min_interval.setRange(5, 600)
        self._min_interval.setSuffix(" 秒")
        self._max_interval = QSpinBox()
        self._max_interval.setRange(5, 600)
        self._max_interval.setSuffix(" 秒")
        interval_row.addWidget(QLabel("下限"))
        interval_row.addWidget(self._min_interval)
        interval_row.addWidget(QLabel("上限"))
        interval_row.addWidget(self._max_interval)
        interval_row.addStretch(1)
        reading_form.addRow("单页停留范围：", interval_row)

        # 启动延迟范围
        startup_row = QHBoxLayout()
        self._startup_min = QSpinBox()
        self._startup_min.setRange(0, 600)
        self._startup_min.setSuffix(" 秒")
        self._startup_max = QSpinBox()
        self._startup_max.setRange(0, 600)
        self._startup_max.setSuffix(" 秒")
        startup_row.addWidget(QLabel("下限"))
        startup_row.addWidget(self._startup_min)
        startup_row.addWidget(QLabel("上限"))
        startup_row.addWidget(self._startup_max)
        startup_row.addStretch(1)
        reading_form.addRow("启动延迟范围：", startup_row)

        # 长休息频率 + 时长
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
        longrest_row.addWidget(QLabel("次休息"))
        longrest_row.addWidget(self._long_rest_min)
        longrest_row.addWidget(QLabel("~"))
        longrest_row.addWidget(self._long_rest_max)
        longrest_row.addStretch(1)
        reading_form.addRow("长休息：", longrest_row)

        # 失败冷却范围
        cooldown_row = QHBoxLayout()
        self._fail_cd_min = QSpinBox()
        self._fail_cd_min.setRange(0, 600)
        self._fail_cd_min.setSuffix(" 秒")
        self._fail_cd_max = QSpinBox()
        self._fail_cd_max.setRange(0, 600)
        self._fail_cd_max.setSuffix(" 秒")
        cooldown_row.addWidget(QLabel("下限"))
        cooldown_row.addWidget(self._fail_cd_min)
        cooldown_row.addWidget(QLabel("上限"))
        cooldown_row.addWidget(self._fail_cd_max)
        cooldown_row.addStretch(1)
        reading_form.addRow("失败冷却范围：", cooldown_row)

        root.addWidget(reading_box)

        # ===== 🔑 Skill API Key =====
        skill_box = QGroupBox("🔑 Skill API Key")
        skill_layout = QVBoxLayout(skill_box)
        key_row = QHBoxLayout()
        self._api_key_input = QLineEdit()
        self._api_key_input.setPlaceholderText("wrk-xxxxxxxxxxxxxxxx")
        self._api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._btn_verify = QPushButton("✅ 验证")
        self._btn_verify.clicked.connect(self._on_verify_api_key)
        key_row.addWidget(self._api_key_input, 1)
        key_row.addWidget(self._btn_verify)
        skill_layout.addLayout(key_row)
        self._lbl_key_status = QLabel("状态：未验证")
        self._lbl_key_status.setStyleSheet("color:#5f6c85;font-size:12px;")
        skill_layout.addWidget(self._lbl_key_status)

        # Skill 数据刷新频率
        refresh_row = QHBoxLayout()
        refresh_row.addWidget(QLabel("Skill 数据刷新间隔："))
        self._refresh_interval = QSpinBox()
        self._refresh_interval.setRange(1, 240)
        self._refresh_interval.setSuffix(" 分钟")
        self._refresh_interval.setToolTip("调度器每 N 分钟调用 Skill 覆盖阅读统计和书籍进度")
        refresh_row.addWidget(self._refresh_interval)
        refresh_row.addStretch(1)
        skill_layout.addLayout(refresh_row)

        root.addWidget(skill_box)

        # ===== 📢 WxPusher 推送 =====
        push_box = QGroupBox("📢 WxPusher 推送")
        push_layout = QVBoxLayout(push_box)
        spt_row = QHBoxLayout()
        spt_row.addWidget(QLabel("SPT:"))
        self._spt_input = QLineEdit()
        self._spt_input.setPlaceholderText("SPT_xxxxxxxxxxxx")
        spt_row.addWidget(self._spt_input, 1)
        push_layout.addLayout(spt_row)
        rules_label = QLabel("通知规则（多选）：")
        rules_label.setStyleSheet("font-weight:600;margin-top:8px;")
        push_layout.addWidget(rules_label)
        self._chk_daily_start = QCheckBox("每日首次开始（包含今日目标）")
        self._chk_cookie_fail = QCheckBox("Cookie 失效通知")
        self._chk_daily_done = QCheckBox("任务完成发送")
        self._chk_login_success = QCheckBox("登录成功通知")
        push_layout.addWidget(self._chk_daily_start)
        push_layout.addWidget(self._chk_cookie_fail)
        push_layout.addWidget(self._chk_daily_done)
        push_layout.addWidget(self._chk_login_success)
        root.addWidget(push_box)

        # ===== 💻 系统设置 =====
        sys_box = QGroupBox("💻 系统设置")
        sys_layout = QVBoxLayout(sys_box)
        self._chk_autostart = QCheckBox("开机自启动")
        self._chk_autostart.toggled.connect(self._on_autostart_toggled)
        self._chk_minimize_tray = QCheckBox("点击关闭时最小化到托盘")
        sys_layout.addWidget(self._chk_autostart)
        sys_layout.addWidget(self._chk_minimize_tray)
        root.addWidget(sys_box)

        # ===== 🧹 数据维护 =====
        data_box = QGroupBox("🧹 数据维护")
        data_layout = QVBoxLayout(data_box)
        clean_row = QHBoxLayout()
        clean_label = QLabel("清除 Cookie 并重启程序")
        clean_label.setStyleSheet("color:#2f3b52;")
        self._btn_clean = QPushButton("🧹 清除")
        self._btn_clean.setStyleSheet(
            "QPushButton{background:#d9534f;color:#fff;border:none;border-radius:8px;"
            "padding:8px 20px;font-size:13px;font-weight:600;}"
            "QPushButton:hover{background:#c9302c;}"
        )
        self._btn_clean.clicked.connect(self._on_clean_cookie)
        clean_row.addWidget(clean_label, 1)
        clean_row.addWidget(self._btn_clean)
        data_layout.addLayout(clean_row)
        hint = QLabel("说明：清除后需重新扫码登录")
        hint.setStyleSheet("color:#93a0b8;font-size:11px;")
        data_layout.addWidget(hint)
        root.addWidget(data_box)

        # ===== 保存按钮 =====
        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self._btn_save = QPushButton("💾 保存设置")
        self._btn_save.setStyleSheet(
            "QPushButton{background:#2d9d3c;color:#fff;border:none;border-radius:8px;"
            "padding:8px 24px;font-size:13px;font-weight:600;}"
            "QPushButton:hover{background:#258a35;}"
        )
        self._btn_save.clicked.connect(self._on_save)
        save_row.addWidget(self._btn_save)
        root.addLayout(save_row)

        root.addStretch(1)

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
        self._lbl_key_status.setStyleSheet("color:#c49100;font-size:12px;")
        self._btn_verify.setEnabled(False)
        self._btn_verify.setText("⏳ 验证中...")

        self._checker = _ApiKeyChecker(self._skill)
        self._checker.result.connect(self._on_verify_done)
        self._checker.start()
        # 超时保护（30s）
        QTimer.singleShot(30000, lambda: self._on_verify_done(False, "❌ 超时") if self._checker and self._checker.isRunning() else None)

    def _on_verify_done(self, ok: bool, msg: str) -> None:
        self._btn_verify.setEnabled(True)
        self._btn_verify.setText("✅ 验证")
        if ok:
            self._lbl_key_status.setText(f"状态：{msg}")
            self._lbl_key_status.setStyleSheet("color:#2d9d3c;font-size:12px;font-weight:600;")
        else:
            self._lbl_key_status.setText(f"状态：{msg}")
            self._lbl_key_status.setStyleSheet("color:#d9534f;font-size:12px;font-weight:600;")

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
            self,
            "即将重启",
            "Cookie 已清除，程序将在 3 秒后自动重启。",
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
        QApplication.instance().quit()
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
            self._cfg.set("reading.min_interval_sec", self._min_interval.value(), auto_save=False)
            self._cfg.set("reading.max_interval_sec", self._max_interval.value(), auto_save=False)
            self._cfg.set("reading.startup_delay_min_sec", self._startup_min.value(), auto_save=False)
            self._cfg.set("reading.startup_delay_max_sec", self._startup_max.value(), auto_save=False)
            self._cfg.set("reading.long_rest_every", self._long_rest_every.value(), auto_save=False)
            self._cfg.set("reading.long_rest_min_sec", self._long_rest_min.value(), auto_save=False)
            self._cfg.set("reading.long_rest_max_sec", self._long_rest_max.value(), auto_save=False)
            self._cfg.set("reading.fail_cooldown_min_sec", self._fail_cd_min.value(), auto_save=False)
            self._cfg.set("reading.fail_cooldown_max_sec", self._fail_cd_max.value(), auto_save=False)
            self._cfg.set("skill.api_key", self._api_key_input.text().strip(), auto_save=False)
            self._cfg.set("skill.refresh_interval_min", self._refresh_interval.value(), auto_save=False)
            self._cfg.set("push.wxpusher_spt", self._spt_input.text().strip(), auto_save=False)
            self._cfg.set("push.notify_daily_start", self._chk_daily_start.isChecked(), auto_save=False)
            self._cfg.set("push.notify_cookie_fail", self._chk_cookie_fail.isChecked(), auto_save=False)
            self._cfg.set("push.notify_daily_done", self._chk_daily_done.isChecked(), auto_save=False)
            self._cfg.set("push.notify_login_success", self._chk_login_success.isChecked(), auto_save=False)
            self._cfg.set("app.minimize_to_tray", self._chk_minimize_tray.isChecked(), auto_save=True)
            log.info("设置已保存")
            QMessageBox.information(self, "成功", "设置已保存 ✅")
            # 保存成功后自动返回主界面
            self._on_back_to_main()
        except Exception as exc:  # noqa: BLE001
            log.error("保存设置失败：%s", exc)
            QMessageBox.critical(self, "错误", f"保存失败：{exc}")


# 延迟导入避免循环依赖
from PySide6.QtWidgets import QApplication  # noqa: E402
