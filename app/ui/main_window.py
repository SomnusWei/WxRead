"""主窗口（v2 上下布局 + 托盘 + 单例）。

布局：
  ┌─────────────────────────┐
  │ 上：主界面/设置页（QStackedWidget）│
  ├─────────────────────────┤
  │ 下：工作日志区            │
  └─────────────────────────┘

功能：
  - 系统托盘（最小化到托盘、双击恢复、右键菜单退出）
  - 单例 SHOW 请求处理（activate_and_restore）
  - 集成 scheduler / api / skill_api / local_db / notifier
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMenu,
    QSystemTrayIcon,
    QStackedWidget,
    QWidget,
    QLabel,
)
from PySide6.QtCore import Signal

from app.core.config import ConfigStore
from app.core.local_db import LocalDB
from app.core.notifier import WxPusherNotifier
from app.core.scheduler import Scheduler
from app.core.skill_api import SkillAPI
from app.core.weread_api import WeReadApi
from app.utils.logger import get_logger

log = get_logger(__name__)


def _make_app_icon(*, size: int = 64) -> QIcon:
    """用 QPainter 绘制书本图标，避免外部资源依赖。"""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    w, h = size, size
    path_d = 0.18 * size
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor("#2d6cdf")))
    path = QPainterPath()
    path.addRoundedRect(0, 0, w, h, path_d, path_d)
    painter.drawPath(path)

    bx, by = int(0.22 * w), int(0.28 * h)
    bw, bh = int(0.56 * w), int(0.44 * h)
    painter.setBrush(QBrush(QColor("#ffffff")))
    painter.drawRoundedRect(bx, by, bw, bh, 4, 4)
    painter.setBrush(QBrush(QColor("#e8eefc")))
    painter.drawRoundedRect(bx, by, bw // 2, bh, 4, 4)
    painter.setPen(QPen(QColor("#2d6cdf"), max(1, size // 40), Qt.PenStyle.SolidLine))
    painter.drawLine(bx + bw // 2, by + 2, bx + bw // 2, by + bh - 2)

    painter.setPen(QColor("#ffffff"))
    font = QFont()
    font.setBold(True)
    font.setPointSize(max(8, size // 5))
    painter.setFont(font)
    painter.drawText(
        pix.rect(),
        Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight,
        "W",
    )
    painter.end()
    return QIcon(pix)


class MainWindow(QMainWindow):
    """主窗口（上下布局 + 托盘 + 单例）。"""

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

        self._main_page: QWidget | None = None
        self._settings_page: QWidget | None = None
        self._stack: QStackedWidget | None = None
        self._tray: QSystemTrayIcon | None = None

        self._build_ui()
        self._build_tray()

        # 加载 .ico 图标
        _icon_path = Path(__file__).parent.parent / "resources" / "app.ico"
        if not _icon_path.exists():
            _icon_path = Path(
                getattr(sys, "_MEIPASS", Path(__file__).parent.parent)
            ) / "app" / "resources" / "app.ico"
        if _icon_path.exists():
            self.setWindowIcon(QIcon(str(_icon_path)))
        else:
            self.setWindowIcon(_make_app_icon())

        # 启动后异步检测登录态
        QTimer.singleShot(300, self._async_check_session)

        # 启动状态
        if start_minimized and self._tray is not None:
            self.hide()
        else:
            self.show()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.setWindowTitle("WxReadAssistant v2.0")
        self.resize(1280, 820)
        self.setMinimumSize(900, 600)

        # 中央容器：QStackedWidget（main_page / settings_page）
        # MainPage 内部已自带上下布局（状态区 + 工作日志）
        self._stack = QStackedWidget()
        self._main_page = self._create_main_page()
        self._settings_page = self._create_settings_page()
        self._stack.addWidget(self._main_page)
        self._stack.addWidget(self._settings_page)
        self._stack.setCurrentIndex(0)
        self.setCentralWidget(self._stack)

    def _create_main_page(self) -> QWidget:
        """创建主界面（Phase 9 替换为真正的 MainPage）。"""
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
            log.info("[MainWindow] 已加载 MainPage")
            return page
        except ImportError:
            log.warning("[MainWindow] main_page 未实现，使用占位")
            placeholder = QLabel(
                "📖 主界面（Phase 9 实现）\n\n"
                "上左：状态+控制\n"
                "上右：统计+操作\n"
                "下：工作日志"
            )
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setStyleSheet(
                "QLabel{background:#f4f7fc;color:#2f3b52;font-size:14px;padding:20px;}"
            )
            return placeholder

    def _create_settings_page(self) -> QWidget:
        """创建设置页（Phase 9 替换为真正的 SettingsPage）。"""
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
        except ImportError:
            log.warning("[MainWindow] settings_page 未实现，使用占位")
            placeholder = QLabel("⚙️ 设置页（Phase 9 实现）")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setStyleSheet(
                "QLabel{background:#f4f7fc;color:#2f3b52;font-size:14px;padding:20px;}"
            )
            return placeholder

    # ------------------------------------------------------------------
    # 页面切换
    # ------------------------------------------------------------------
    def show_main_page(self) -> None:
        if self._stack:
            self._stack.setCurrentIndex(0)

    def show_settings_page(self) -> None:
        if self._stack:
            self._stack.setCurrentIndex(1)

    # ------------------------------------------------------------------
    # 系统托盘
    # ------------------------------------------------------------------
    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.warning("系统托盘不可用")
            self._tray = None
            return
        tray = QSystemTrayIcon(_make_app_icon(size=24), self)
        tray.setToolTip("微信读书助手 v2.0")
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
                    "微信读书助手 v2.0",
                    "已切换到运行中的实例 ✅",
                    _make_app_icon(size=32),
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
                "微信读书助手 v2.0",
                "已最小化到托盘，双击图标可恢复。",
                _make_app_icon(size=32),
                3000,
            )

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
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
        QApplication.instance().quit()
        sys.exit(0)

    # ------------------------------------------------------------------
    # 异步检测登录态
    # ------------------------------------------------------------------
    def _async_check_session(self) -> None:
        """启动后异步检测登录态（避免阻塞 UI）。"""
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
