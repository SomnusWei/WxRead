"""主窗口（v3 · 壹式书脊蓝金W图标 + 米白纸/星辰夜读 双主题切换）。

特性：
  - 主图标：壹式·书脊蓝金W（make_app_icon 纯 QPainter 绘制，支持 256/128/64/48/32/24/16）
  - 主题：MainPage.theme_changed → MainWindow.apply_theme() → styles.apply_global_qss()
  - 托盘：最小化到托盘 + 双击恢复 + 右键菜单退出
  - 单例：activate_and_restore 唤起
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMenu,
    QStackedWidget,
    QSystemTrayIcon,
    QWidget,
)

from app.core.config import ConfigStore
from app.core.local_db import LocalDB
from app.core.notifier import WxPusherNotifier
from app.core.scheduler import Scheduler
from app.core.skill_api import SkillAPI
from app.core.weread_api import WeReadApi
from app.ui.icon_store import APP_VERSION, app_version_display, make_app_icon
from app.ui.styles import apply_global_qss
from app.utils.logger import get_logger

if TYPE_CHECKING:
    from app.ui.main_page import MainPage
    from app.ui.settings_page import SettingsPage

log = get_logger(__name__)


class MainWindow(QMainWindow):
    """主窗口（v3）。"""

    session_check_done = Signal(bool)

    def __init__(self, *, start_minimized: bool = False) -> None:
        super().__init__()
        # 核心服务（按依赖顺序构造）
        self._config = ConfigStore()
        self._api = WeReadApi(self._config)
        self._skill = SkillAPI(self._config)
        self._db = LocalDB()
        self._notifier = WxPusherNotifier(self._config)
        self._scheduler = Scheduler(
            self._api, self._skill, self._db, self._config, self._notifier
        )

        self._main_page: MainPage | None = None
        self._settings_page: SettingsPage | None = None
        self._stack: QStackedWidget | None = None
        self._tray: QSystemTrayIcon | None = None
        self._theme: str = "light"   # light / dark

        # 应用壹式主图标（方案壹·书脊蓝金W）
        self._apply_app_icon()

        # 构建 UI
        self._build_ui()
        self._build_tray()

        # 优先尝试加载 app.ico；若不存在则用 make_app_icon()（已设置）
        self._try_load_external_ico()

        # 应用初始主题（从配置读取，默认为 light 米白纸方案A）
        saved_theme = str(self._config.get("app.theme", "light") or "light").strip().lower()
        self.apply_theme(saved_theme if saved_theme in ("light", "dark") else "light", sync_page=False)

        # 启动后异步检测登录态
        QTimer.singleShot(300, self._async_check_session)

        # 启动状态
        if start_minimized and self._tray is not None:
            self.hide()
        else:
            self.show()

    # ==================================================================
    # 图标：壹式·书脊蓝金W（= window icon + tray icon + taskbar）
    # ==================================================================
    def _apply_app_icon(self) -> None:
        icon = make_app_icon(256)
        self.setWindowIcon(icon)
        self.setWindowTitle(f"WxReadAssistant v{app_version_display()}")

    def _try_load_external_ico(self) -> None:
        """优先使用打包资源 app.ico（若存在）。"""
        icon_path = Path(__file__).resolve().parent.parent / "resources" / "app.ico"
        if not icon_path.exists():
            icon_path = Path(
                getattr(sys, "_MEIPASS", Path(__file__).parent.parent)
            ) / "app" / "resources" / "app.ico"
        if icon_path.exists():
            icon = QIcon(str(icon_path))
            if not icon.isNull():
                self.setWindowIcon(icon)
                if self._tray is not None:
                    self._tray.setIcon(icon)

    # ==================================================================
    # UI 构建
    # ==================================================================
    def _build_ui(self) -> None:
        self.resize(1280, 820)
        self.setMinimumSize(900, 600)

        self._stack = QStackedWidget()
        self._main_page = self._create_main_page()
        self._settings_page = self._create_settings_page()
        self._stack.addWidget(self._main_page)      # index 0
        self._stack.addWidget(self._settings_page)  # index 1
        self._stack.setCurrentIndex(0)
        self.setCentralWidget(self._stack)

        # 主题监听：MainPage 的 🌙 夜读星辰开关 → apply_theme
        if self._main_page is not None and hasattr(self._main_page, "theme_changed"):
            self._main_page.theme_changed.connect(self.apply_theme)

    def _create_main_page(self) -> QWidget:
        try:
            from app.ui.main_page import MainPage  # noqa: WPS433
            page = MainPage(
                config=self._config,
                api=self._api,
                skill=self._skill,
                db=self._db,
                notifier=self._notifier,
                scheduler=self._scheduler,
                main_window=self,
            )
            log.info("[MainWindow] 已加载 MainPage(v3)")
            return page
        except ImportError as exc:  # pragma: no cover - fallback
            log.warning("[MainWindow] main_page 加载失败：%s", exc)
            placeholder = QLabel(
                f"📖 WxReadAssistant v{app_version_display()}（主界面加载失败占位）\n\n"
                f"请检查 app/ui/main_page.py 是否存在。\n\n错误：{exc}"
            )
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setStyleSheet(
                "QLabel{background:#F9F9F5;color:#1F2937;font-size:14px;padding:24px;}"
            )
            return placeholder

    def _create_settings_page(self) -> QWidget:
        try:
            from app.ui.settings_page import SettingsPage  # noqa: WPS433
            page = SettingsPage(
                config=self._config,
                api=self._api,
                skill=self._skill,
                main_window=self,
            )
            log.info("[MainWindow] 已加载 SettingsPage")
            return page
        except ImportError as exc:  # pragma: no cover - fallback
            log.warning("[MainWindow] settings_page 加载失败：%s", exc)
            placeholder = QLabel(f"⚙️ 配置中心（未实现 · 错误：{exc}）")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setStyleSheet(
                "QLabel{background:#F9F9F5;color:#1F2937;font-size:14px;padding:24px;}"
            )
            return placeholder

    # ==================================================================
    # 主题切换槽（方案A米白纸 ↔ 方案B星辰夜读）
    # ==================================================================
    def apply_theme(self, theme_name: str, *, sync_page: bool = True) -> None:
        """应用全局主题。

        Args:
            theme_name: "light" / "dark"
            sync_page: 是否同步 MainPage 的开关状态（第一次设置时为 False，
                       避免 setChecked → toggled → 递归 apply_theme）。
        """
        t = str(theme_name or "").strip().lower()
        if t not in ("light", "dark"):
            t = "light"
        self._theme = t

        app = QApplication.instance()
        if app is not None:
            applied = apply_global_qss(app, t)
            log.info("[MainWindow] 主题已应用：%s (QSS key=%s)", t, applied)

        # 让设置页/主页也重绘各自的卡片背景、控件色
        if self._settings_page is not None and hasattr(self._settings_page, "apply_theme"):
            try:
                self._settings_page.apply_theme(t)  # type: ignore[call-arg]
            except Exception:  # noqa: BLE001
                pass

        # 同步 MainPage 开关状态
        if sync_page and self._main_page is not None and hasattr(self._main_page, "_tgl_theme"):
            try:
                tgl = self._main_page._tgl_theme  # noqa: SLF001
                tgl.blockSignals(True)
                tgl.setChecked(t == "dark")
                tgl.blockSignals(False)
                # 手动重染色图标/卡片（因为信号被阻塞）
                if hasattr(self._main_page, "_apply_theme_to_icons"):
                    self._main_page._apply_theme_to_icons(t)  # noqa: SLF001
                if t == "dark" and hasattr(self._main_page, "_apply_dark_card_styles"):
                    self._main_page._apply_dark_card_styles()  # noqa: SLF001
                elif t == "light" and hasattr(self._main_page, "_apply_light_card_styles"):
                    self._main_page._apply_light_card_styles()  # noqa: SLF001
                # 同步标签文字
                new_label = "☀️  米纸日间" if t == "dark" else "🌙  夜读星辰"
                tgl.setText(new_label)
            except Exception:  # noqa: BLE001
                pass

    # ==================================================================
    # 页面切换
    # ==================================================================
    def show_main_page(self) -> None:
        if self._stack:
            self._stack.setCurrentIndex(0)

    def show_settings_page(self) -> None:
        if self._stack:
            self._stack.setCurrentIndex(1)

    # ==================================================================
    # 系统托盘
    # ==================================================================
    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.warning("系统托盘不可用")
            self._tray = None
            return
        tray = QSystemTrayIcon(make_app_icon(24), self)
        tray.setToolTip(f"WxReadAssistant v{app_version_display()}")
        menu = QMenu(self)
        act_show = QAction("显示主界面", self)
        act_show.triggered.connect(self.show_from_tray)
        act_hide = QAction("最小化到托盘", self)
        act_hide.triggered.connect(self.hide_to_tray)
        menu.addSeparator()
        act_exit = QAction("退出程序", self)
        act_exit.triggered.connect(self._exit_app)
        menu.addAction(act_show)
        menu.addAction(act_hide)
        menu.addSeparator()
        menu.addAction(act_exit)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self._tray = tray

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_from_tray()

    def show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def activate_and_restore(self) -> None:
        """单例请求：从托盘/最小化/后台恢复并置顶。"""
        log.info("[singleton] 收到新实例激活请求 → 恢复主窗口")
        if self.isMinimized():
            self.showNormal()
        if not self.isVisible():
            self.show()
        self.raise_()
        self.activateWindow()
        try:
            flags = self.windowFlags()
            self.setWindowFlags(flags | Qt.WindowType.WindowStaysOnTopHint)
            self.show()
            self.setWindowFlags(flags)
            self.show()
        except Exception:  # noqa: BLE001
            pass
        self.raise_()
        self.activateWindow()
        try:
            if self._tray is not None:
                self._tray.showMessage(
                    f"WxReadAssistant v{app_version_display()}",
                    "已切换到运行中的实例 ✅",
                    make_app_icon(32),
                    2000,
                )
        except Exception:  # noqa: BLE001
            pass

    def hide_to_tray(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.hide()
        if self._tray is not None:
            self._tray.showMessage(
                f"WxReadAssistant v{app_version_display()}",
                "已最小化到托盘，双击图标可恢复。",
                make_app_icon(32),
                3000,
            )

    # ==================================================================
    # 事件
    # ==================================================================
    def closeEvent(self, event) -> None:  # type: ignore[override]
        minimize_to_tray = bool(self._config.get("app.minimize_to_tray", True))
        if minimize_to_tray and self._tray is not None:
            event.ignore()
            self.hide_to_tray()
            return
        self._exit_app()
        event.ignore()

    def _exit_app(self) -> None:
        log.info("正在退出程序...")
        try:
            if self._scheduler.isRunning():
                self._scheduler.stop()
        except Exception as exc:  # noqa: BLE001
            log.exception("停止调度器时出错：%s", exc)
        try:
            if self._tray is not None:
                self._tray.hide()
        except Exception:  # noqa: BLE001
            pass
        qapp = QApplication.instance()
        if qapp is not None:
            qapp.quit()
        sys.exit(0)

    # ==================================================================
    # 异步检测登录态
    # ==================================================================
    def _async_check_session(self) -> None:
        from PySide6.QtCore import QThread  # noqa: WPS433

        class _SessionChecker(QThread):
            ok = Signal(bool)

            def __init__(self, api: WeReadApi) -> None:
                super().__init__()
                self._api = api

            def run(self) -> None:  # noqa: D401
                try:
                    result = self._api.check_session()
                    self.ok.emit(bool(result))
                except Exception as exc:  # noqa: BLE001
                    log.warning("登录态检测异常：%s", exc)
                    self.ok.emit(False)

        checker = _SessionChecker(self._api)
        checker.ok.connect(lambda ok: self.session_check_done.emit(ok))
        checker.start()
        self._session_checker = checker  # 保持引用避免 GC
