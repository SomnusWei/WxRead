"""主窗口：Tab 导航（状态 / 扫码登录 / 设置）+ 系统托盘 + 全局服务实例组装。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QMainWindow,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QStatusBar,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.core.config import ConfigStore
from app.core.notifier import WxPusherNotifier
from app.core.scheduler import ReadingScheduler
from app.core.weread_api import WeReadApi
from app.ui.login_page import LoginPage
from app.ui.settings_page import SettingsPage
from app.ui.status_page import StatusPage
from app.utils.logger import get_logger, log_bus

log = get_logger(__name__)


def _make_app_icon(*, size: int = 64) -> QIcon:
    """用 QPainter 绘制一个简单的书本图标，避免外部资源文件依赖。"""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    # 圆角底色
    w, h = size, size
    path_d = 0.18 * size
    color_bg = QColor("#2d6cdf")
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(color_bg))
    path = QPainterPath()
    path.addRoundedRect(0, 0, w, h, path_d, path_d)
    painter.drawPath(path)

    # 书本：白底 + 蓝书脊线
    bx, by, bw, bh = int(0.22 * w), int(0.28 * h), int(0.56 * w), int(0.44 * h)
    painter.setBrush(QBrush(QColor("#ffffff")))
    painter.drawRoundedRect(bx, by, bw, bh, 4, 4)
    painter.setBrush(QBrush(QColor("#e8eefc")))
    painter.drawRoundedRect(bx, by, bw // 2, bh, 4, 4)
    painter.setPen(QPen(QColor("#2d6cdf"), max(1, size // 40), Qt.PenStyle.SolidLine))
    painter.drawLine(bx + bw // 2, by + 2, bx + bw // 2, by + bh - 2)

    # 字母 "W"
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor("#ffffff")))
    font = QFont()
    font.setBold(True)
    font.setPointSize(max(8, size // 5))
    painter.setFont(font)
    painter.setPen(QColor("#ffffff"))
    painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight, "W")
    painter.end()
    return QIcon(pix)


class MainWindow(QMainWindow):
    _session_check_done = Signal(bool)

    def __init__(
        self,
        *,
        start_minimized: bool = False,
    ) -> None:
        super().__init__()
        # 核心服务（按依赖顺序构造）
        self._config = ConfigStore()
        self._api = WeReadApi(self._config)
        self._notifier = WxPusherNotifier(self._config)
        self._scheduler = ReadingScheduler(self._api, self._config, self._notifier)

        self._build_ui()
        self._build_tray()
        # —— 需求5：加载 .ico 图标（EXE 图标与进程图标一致）——
        # 开发模式：app/resources/app.ico（__file__ = app/ui/main_window.py → parent.parent = app/）
        _icon_path = Path(__file__).parent.parent / "resources" / "app.ico"
        if not _icon_path.exists():
            # 打包模式：sys._MEIPASS = _internal/，app/resources/app.ico
            _icon_path = Path(getattr(sys, "_MEIPASS", Path(__file__).parent.parent)) / "app" / "resources" / "app.ico"
        if _icon_path.exists():
            self.setWindowIcon(QIcon(str(_icon_path)))
        else:
            self.setWindowIcon(_make_app_icon())  # fallback：运行时绘制

        # 登录态检测完成信号 → 状态页更新 + 兜底恢复"上次会话阅读本"
        #   （此时 Skill/书架同步可能已写入更权威的书 → 恢复函数内部会检测 current_book.url 非空就跳过）
        from PySide6.QtCore import QTimer as _QTimer
        def _on_session_ok_wrapper(_ok: bool) -> None:
            try:
                self._status_page._update_cookie_status(_ok)
            except Exception:  # noqa: BLE001
                pass
            _QTimer.singleShot(200, self._restore_last_session_book)
        self._session_check_done.connect(_on_session_ok_wrapper)

        # —— 跨页信号的"懒加载版"桥接（解决崩溃根因 v2）——
        #   _login_page/_settings_page 此时 = None（占位），不能直接连信号。
        #   用"转发 slot"包一层：懒加载完成后 _ensure_* 内部再真实连；
        #   没加载好之前的信号请求 → 直接走 _queue_navigate_after_init / _queue_restore_after_init。
        # （上面的 _build_ui 里已经把 request_navigate_reader/request_restore_browser_session
        #   分别连到了这两个排队入口）

        # 启动阶段"分段 QTimer 拆解"（丝滑化关键 v3）：
        #   把"服务预热 + 登录态检查 + 最大化窗口"拆成 4 个独立的单步事件，
        #   避免 __init__ 里一次性堆 200+ms 的同步任务导致首帧迟迟不画。
        from PySide6.QtCore import QTimer
        # 阶段 1：窗口显示后 40ms → 最大化（先让用户看到"有窗口出来了"）
        if not (start_minimized or self._config.get("app.start_minimized", False)):
            if self._config.get("app.start_maximized", True):
                QTimer.singleShot(40, self.showMaximized)
        else:
            QTimer.singleShot(200, self.hide_to_tray)
        # 阶段 2：120ms → 后台异步检查登录态（走子线程，不卡 GUI）
        QTimer.singleShot(120, self._post_start)
        # 阶段 3：220ms → 轻量预热（保留位，可扩展）

    # ---------------- UI ----------------
    def _build_ui(self) -> None:
        self.setWindowTitle("微信读书助手 · 自动阅读")
        self.resize(1080, 740)
        # —— 缩小窗口时：让 Tab 内容走滚动条，不把卡片/按钮挤压成像素块
        #    860×580 是"不滚动也能舒适看到 4 张卡片 + 日志窗口"的下限；
        #    低于这个由各 Tab 的 QScrollArea 接手（不再无限压控件）。
        self.setMinimumSize(860, 580)

        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 10, 14, 6)
        root.setSpacing(8)

        # 标题条
        header = QLabel("🌿 微信读书助手")
        header.setStyleSheet(
            "font-size:16px;font-weight:700;color:#2f3b52; padding:2px 6px;"
        )
        subtitle = QLabel("每天智能阅读 · 轻松完成挑战赛目标")
        subtitle.setStyleSheet("color:#7a879f;font-size:12px; padding:0 6px 4px;")
        root.addWidget(header)
        root.addWidget(subtitle)

        # Tab 容器
        self._tabs = QTabWidget()
        self._tabs.setTabShape(QTabWidget.Rounded)
        self._tabs.setDocumentMode(True)
        # —— 切页卡顿优化（v2）：把绘制卸载到 QPixmap 缓存，避免重算复杂布局抖动；
        #    同时用 currentChanged 触发登录页懒加载 WebEngine。
        try:
            self._tabs.setStyleSheet(
                "QTabWidget::pane{border:1px solid #e3e8f1;border-radius:10px;"
                "top:-1px;background:#f4f7fc;}"
                "QTabBar::tab{background:#eef2f8;color:#5f6c85;padding:8px 20px;"
                "border-top-left-radius:10px;border-top-right-radius:10px;"
                "font-size:13px;font-weight:600;margin-right:6px;min-width:110px;}"
                "QTabBar::tab:selected{background:#ffffff;color:#2d6cdf;"
                "border:1px solid #e3e8f1;border-bottom-color:#ffffff;}"
                "QTabBar::tab:hover:!selected{background:#e4ebf7;}"
            )
        except Exception:  # noqa: BLE001
            pass
        # 懒加载 Tab：启动时只构造最轻的状态页；登录/设置页 defer 到首次点击（降低 30%+ 冷启动耗时）
        self._login_page: LoginPage | None = None
        self._settings_page: SettingsPage | None = None

        self._status_page = StatusPage(self._scheduler, self._api, self._config, self._tabs)

        # —— 需求1：状态页不再用 QScrollArea 包裹（内嵌浏览器有自己的尺寸管理）——
        self._tabs.addTab(self._status_page, "🟢 状态")
        # —— 需求1：删除扫码登录 Tab，LoginPage 直接嵌入状态页右边 30% ——
        self._login_placeholder = None  # 不再需要占位 Tab
        self._settings_placeholder = self._make_placeholder_tab("⚙️ 设置", "点击切换后将加载设置面板。")
        self._tabs.addTab(self._settings_placeholder, "⚙️ 设置")
        self._tabs.currentChanged.connect(self._on_tab_changed)

        # —— 把 LoginPage 创建后嵌入 status_page 右边容器 ——
        #    延迟到首帧后（QTimer.singleShot 0），避免冷启动被 WebEngine 拖慢
        from PySide6.QtCore import QTimer as _QTimer2
        _QTimer2.singleShot(100, self._ensure_login_page_embedded)

        # —— 状态页请求"在扫码浏览器打开 / 恢复登录态"时，如果登录页还没懒加载，
        #    先触发 _ensure_login_page 再把 URL / restore 请求排队（否则会 AttributeError）。
        self._status_page.request_navigate_reader.connect(self._queue_navigate_after_init)
        self._status_page.request_restore_browser_session.connect(self._queue_restore_after_init)
        self._pending_navigate_url: str | None = None
        self._pending_restore = False

        root.addWidget(self._tabs, 1)

        # 状态栏
        sb = QStatusBar(self)
        self.setStatusBar(sb)
        self._sb_label = QLabel("准备就绪")
        self._sb_label.setStyleSheet("padding: 0 8px;")
        sb.addWidget(self._sb_label, 1)

    # ------ UI helpers ------
    def _wrap_in_scroll(self, inner: QWidget) -> QScrollArea:
        """把 Page 包进 QScrollArea：窗口缩小时滚动，不挤压卡片/按钮；拉大时跟随扩展。"""
        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame) if hasattr(QFrame, "Shape") else None
        try:
            scroll.setFrameStyle(QFrame.NoFrame)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # 背景对齐 tab pane：视觉上不割裂
        scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollBar:vertical{background:#f1f5fb;width:10px;margin:2px;border-radius:5px;}"
            "QScrollBar::handle:vertical{background:#c8d3e8;min-height:40px;border-radius:5px;}"
            "QScrollBar::handle:vertical:hover{background:#94a7cd;}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical{height:0;}"
            "QScrollBar:horizontal{background:#f1f5fb;height:10px;margin:2px;border-radius:5px;}"
            "QScrollBar::handle:horizontal{background:#c8d3e8;min-width:40px;border-radius:5px;}"
            "QScrollBar::handle:horizontal:hover{background:#94a7cd;}"
            "QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal{width:0;}"
        )
        # 让内部 widget 自动扩展/收缩（Expanding）以填满 scroll viewport
        inner.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        return scroll

    def _make_placeholder_tab(self, title: str, tip: str) -> QWidget:
        """懒加载占位页：用户第一次切到该 Tab 再初始化真实 Page（减少冷启动 WebEngine/设置页渲染负担）。"""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(40, 40, 40, 40)
        v.addStretch(1)
        h = QLabel(f"⏳ {title}  页面加载中…")
        h.setStyleSheet("font-size:15px;font-weight:600;color:#2f3b52;")
        t = QLabel(tip)
        t.setStyleSheet("color:#7a879f;font-size:13px;")
        t.setWordWrap(True)
        v.addWidget(h)
        v.addSpacing(10)
        v.addWidget(t)
        v.addStretch(1)
        return w

    def _on_tab_changed(self, index: int) -> None:
        """Tab 切换懒加载：设置页（index 1）首次进入才构造。"""
        # Tab 顺序约定：0=状态, 1=设置（扫码登录已删除，嵌入状态页右边）
        if index == 1 and self._settings_page is None:
            self._ensure_settings_page()

    def _ensure_login_page_embedded(self) -> None:
        """需求1：创建 LoginPage 并嵌入 status_page 右边 30% 容器。"""
        try:
            self._login_page = LoginPage(self._api, self._config, self._tabs)
        except Exception as exc:  # noqa: BLE001
            log.exception("嵌入 LoginPage 失败：%s", exc)
            return
        # 建立跨页信号连接
        try:
            self._login_page.session_ready.connect(self._on_session_ready)
            self._login_page.reader_navigated.connect(self._on_reader_navigated)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._status_page.request_navigate_reader.connect(self._login_page.navigate)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._status_page.request_restore_browser_session.connect(
                self._login_page.restore_session_from_config
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            self._login_page.chapters_refresh_requested.connect(self._on_request_refresh_all_chapters)
        except Exception as _e:
            log.debug("连 chapters_refresh_requested 失败：%s", _e)
        try:
            self._login_page.numeric_book_id_detected.connect(self._on_numeric_book_id_detected)
        except Exception as _e:
            log.debug("连 numeric_book_id_detected 失败：%s", _e)
        # 把 LoginPage 设为 status_page 的 _login_page 属性（_on_start 需要访问浏览器 URL）
        self._status_page._login_page = self._login_page
        # 隐藏占位标签，把 LoginPage 嵌入 right_container
        try:
            if hasattr(self._status_page, "_lbl_browser_placeholder"):
                self._status_page._lbl_browser_placeholder.hide()
            self._status_page._right_layout.addWidget(self._login_page)
        except Exception as exc:  # noqa: BLE001
            log.warning("嵌入 LoginPage 到 right_container 失败：%s", exc)
        # 处理排队的 restore/navigate 请求
        if self._pending_restore:
            self._pending_restore = False
            try:
                self._login_page.restore_session_from_config()
            except Exception as exc:  # noqa: BLE001
                log.warning("嵌入登录页排队 restore_session 失败：%s", exc)
        if self._pending_navigate_url:
            url = self._pending_navigate_url
            self._pending_navigate_url = None
            try:
                self._login_page.navigate(url)
            except Exception as exc:  # noqa: BLE001
                log.warning("嵌入登录页排队 navigate 失败：%s", exc)

    def _ensure_login_page(self) -> None:
        """兼容入口：扫码登录 Tab 已删除，LoginPage 嵌入状态页右边。"""
        if self._login_page is None:
            self._ensure_login_page_embedded()

    def _queue_navigate_after_init(self, url: str) -> None:
        """状态页请求"在扫码浏览器打开某本书"时触发。
        登录页已经初始化 → 直接 navigate；否则先触发嵌入初始化并把 URL 排队。"""
        u = str(url or "").strip()
        if not u:
            return
        self._tabs.setCurrentIndex(0)  # 切到状态页（LoginPage 嵌在右边）
        if self._login_page is not None:
            try:
                self._login_page.navigate(u)
            except Exception as exc:  # noqa: BLE001
                log.warning("直接导航到扫码浏览器失败：%s", exc)
            return
        self._pending_navigate_url = u
        self._ensure_login_page_embedded()

    def _queue_restore_after_init(self) -> None:
        """状态页点"恢复浏览器会话"时触发。"""
        self._tabs.setCurrentIndex(0)  # 切到状态页
        if self._login_page is not None:
            try:
                self._login_page.restore_session_from_config()
            except Exception as exc:  # noqa: BLE001
                log.warning("直接恢复扫码浏览器会话失败：%s", exc)
            return
        self._pending_restore = True
        self._ensure_login_page_embedded()

    def _ensure_settings_page(self) -> None:
        from PySide6.QtCore import QTimer
        self._sb_label.setText("正在加载设置页...")

        def _do_build():
            try:
                self._settings_page = SettingsPage(self._api, self._config, self._tabs)
            except Exception as exc:  # noqa: BLE001
                log.exception("懒加载设置页失败：%s", exc)
                self._sb_label.setText(f"设置页加载失败：{exc}")
                return
            # ===== v2 修复：懒加载完成后再真实连接 settings 信号（避免 None 崩溃）=====
            self._settings_page.settings_changed.connect(self._on_settings_changed)
            self._settings_page.verify_requested.connect(self._status_page._on_verify)
            new_widget = self._wrap_in_scroll(self._settings_page)
            self._replace_tab(index=1, new_widget=new_widget)
            self._tabs.setCurrentIndex(1)
            self._sb_label.setText("⚙️ 设置页已就绪")

        QTimer.singleShot(0, _do_build)

    def _replace_tab(self, *, index: int, new_widget: QWidget) -> None:
        """把 Tab 页第 index 个占位 widget 替换为真实页；保留 tab 文字与图标。"""
        if index < 0 or index >= self._tabs.count():
            return
        text = self._tabs.tabText(index)
        icon = self._tabs.tabIcon(index)
        tooltip = self._tabs.tabToolTip(index)
        old = self._tabs.widget(index)
        self._tabs.removeTab(index)
        self._tabs.insertTab(index, new_widget, icon, text)
        if tooltip:
            self._tabs.setTabToolTip(index, tooltip)
        # 把 old 占位 widget 延后回收（避免 removeTab 直接释放后仍在 Qt 事件链里引用）
        if old is not None:
            try:
                old.setParent(None)
                old.deleteLater()
            except Exception:  # noqa: BLE001
                pass


    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = None
            return
        tray = QSystemTrayIcon(_make_app_icon(size=24), self)
        tray.setToolTip("微信读书助手")
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
        self._tray: QSystemTrayIcon | None = tray

    # ---------------- Tray helpers ----------------
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
        """单例请求：把本窗口从托盘 / 最小化 / 后台状态 → 恢复显示并置顶（foreground）。
        供 main.py 的 QLocalServer 在收到第二个实例发来的 SHOW 请求时调用。"""
        from PySide6.QtCore import Qt
        log.info("[singleton] 收到新实例的激活请求 → 恢复主窗口")
        # 1) 如果是最小化/隐藏 → 先 Normal + show
        if self.isMinimized():
            self.showNormal()
        if not self.isVisible():
            self.show()
        # 2) 拉到最前（raise + activateWindow）
        self.raise_()
        self.activateWindow()
        # 3) Win 10/11 系统层强行置顶（SetForegroundWindow 有很多限制，这里尽量再触发一次 WindowState）
        try:
            flags = self.windowFlags()
            # 临时切换 "WindowStaysOnTopHint" 一下，确保系统注意到
            self.setWindowFlags(flags | Qt.WindowType.WindowStaysOnTopHint)
            self.show()
            self.setWindowFlags(flags)
            self.show()
        except Exception:  # noqa: BLE001
            pass
        self.raise_()
        self.activateWindow()
        # 4) 托盘也发个提示（视觉反馈，用户知道收到了请求）
        try:
            if self._tray is not None:
                from app.ui.main_window import _make_app_icon
                self._tray.showMessage(
                    "微信读书助手",
                    "已经有一个实例在运行，已切换到原窗口 ✅",
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
                "微信读书助手",
                "已最小化到右下角任务栏，双击图标可恢复主界面。",
                _make_app_icon(size=32),
                3000,
            )

    # ---------------- Events ----------------
    def closeEvent(self, event) -> None:  # type: ignore[override]
        minimize_to_tray = self._config.get("app.minimize_to_tray", True)
        if minimize_to_tray and self._tray is not None:
            event.ignore()
            self.hide_to_tray()
            return
        self._exit_app()
        event.ignore()

    def _exit_app(self) -> None:
        log.info("正在退出程序...")

        # =============== 关闭前：持久化"当前正在读的书"（下次启动自动恢复）===============
        # 参考经验 532789：即使 URL 为空（用户点了【清除】），也要真写入空字符串，
        # 不能"空就不 save"，否则旧值永远残留 → 用户清除了下次启动又被恢复出来。
        try:
            book_fn = self._api.current_book
            book = (book_fn() if callable(book_fn) else book_fn) or {}
            url_out = str(book.get("url") or "")
            title_out = str(book.get("title") or "")
            book_id_out = str(book.get("bookId") or "")
            reader_id_out = str(book.get("readerId") or "")
            progress_out = str(book.get("progress_text") or "")
            try:
                locked_out = bool(self._api.is_book_locked())
            except Exception:  # noqa: BLE001
                locked_out = False
            # 多次 set 并最后统一 save（避免每 set 写一次磁盘）
            self._config.set("app.last_book.url", url_out, auto_save=False)
            self._config.set("app.last_book.title", title_out, auto_save=False)
            self._config.set("app.last_book.bookId", book_id_out, auto_save=False)
            self._config.set("app.last_book.readerId", reader_id_out, auto_save=False)
            self._config.set("app.last_book.progress_text", progress_out, auto_save=False)
            self._config.set(
                "app.last_book.locked",
                "1" if locked_out else "0",
                auto_save=False,
            )
            self._config.save()
            log.info(
                "[持久化·退出] 已保存上次会话阅读本：url=%s  locked=%s",
                url_out[:90], locked_out,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("[持久化·退出] 保存上次会话阅读本失败：%s", exc)

        try:
            if self._scheduler.isRunning():
                self._scheduler.stop()
        except Exception:  # noqa: BLE001
            log.exception("停止调度器时出错")
        try:
            if self._tray is not None:
                self._tray.hide()
        except Exception:  # noqa: BLE001
            pass
        QApplication.instance().quit()
        sys.exit(0)

    def _restore_last_session_book(self) -> None:
        """启动后：如果当前还没有任何来源设置过"正在读的书"，就从 config 读取"上次会话"的记忆恢复。

        注意：
          · 只有在 current_book 没有 URL 时才恢复（= Skill / 书架 / 导航信号还没写入更权威的书时）。
          · 如果 config.last_book.url 为空字符串 → 不恢复（用户上次明确点了清除 → 尊重用户意图）。
          · 锁状态：退出时 locked=1 则恢复后自动上锁，保持和上次会话一致。
          · 恢复来源写 "last_session_memory"，不属于 USER_INITIATED_SOURCES，不会自动上锁。
        """
        try:
            book_now_fn = self._api.current_book
            book_now = (book_now_fn() if callable(book_now_fn) else book_now_fn) or {}
            if book_now.get("url"):
                # 当前已经有书了（被书架同步/Skill/导航信号先写入了）→ 不覆盖，避免更权威的来源被旧记忆替换
                return
            url = self._config.get("app.last_book.url") or ""
            if not url:
                return  # 没有记忆，或用户上次明确清除（写入 ""）→ 跳过
            payload_raw = {
                "url": url,
                "title": self._config.get("app.last_book.title") or "",
                "bookId": self._config.get("app.last_book.bookId") or "",
                "readerId": self._config.get("app.last_book.readerId") or "",
                "progress_text": self._config.get("app.last_book.progress_text") or "",
            }
            # 去掉空字段，避免 set_current_book 用 None/"" 覆盖潜在已有值（即使它没 URL 也可能有部分字段）
            payload = {k: v for k, v in payload_raw.items() if v}
            final = self._api.set_current_book(payload, source="last_session_memory")
            # 锁恢复：上次退出时上锁，这里也上锁（在 set_current_book 之后，避免来源非 USER 而被拒绝上锁——
            #   lock_current_book 是显式调用，不受来源白名单限制）
            locked_raw = str(self._config.get("app.last_book.locked", "0") or "0")
            if locked_raw == "1":
                try:
                    self._api.lock_current_book(True)
                except Exception:  # noqa: BLE001
                    pass
            final_title = str((final or {}).get("title") or payload_raw.get("title") or "")
            log.info(
                "[持久化·启动] 已恢复上次会话的阅读本：title=%s  url=%s  locked=%s",
                final_title[:60], url[:90], (locked_raw == "1"),
            )
            self._sb_label.setText(f"📚 已恢复上次会话阅读本：{final_title[:40] or '（未命名）'}")
        except Exception as exc:  # noqa: BLE001
            log.exception("[持久化·启动] 恢复上次会话阅读本失败：%s", exc)

    # ---------------- post start ----------------
    def _post_start(self) -> None:
        # 快速检测登录态（非阻塞：另开线程）
        def _task() -> None:
            ok = bool(self._api.check_session())
            self._session_check_done.emit(ok)
        import threading
        threading.Thread(target=_task, daemon=True).start()

        # 持久化·启动：尝试恢复上次会话的阅读本（第一次尝试）
        #   如果用户没有历史登录态，_session_check_done 会很快 emit(False) → 也会再次兜底调用；
        #   这里先放一枪：确保即使检测线程慢，用户也能立即在状态页看到记忆的书。
        from PySide6.QtCore import QTimer as _QTimer2
        _QTimer2.singleShot(400, self._restore_last_session_book)

        # 若配置了账号，则自动开始阅读
        if self._config.get("cookies.wr_vid") or self._config.get("cookies.wr_skey"):
            if self._config.get("app.start_minimized", False):
                # 静默启动：不弹提示
                pass
            else:
                self._sb_label.setText("已检测到历史登录态，可直接开始阅读")

    # ---------------- Handlers ----------------
    def _on_reader_navigated(self, payload: dict) -> None:
        """扫码浏览器进入 reader 页：同步 URL → 当前书，并提示切回状态页能看到。"""
        if not isinstance(payload, dict):
            return
        # 把登录页浏览器的「书的 url」灌回 api.set_current_book，统一用"扫码浏览器导航"标记来源
        book = self._api.set_current_book(dict(payload), source="login_browser_nav")
        if book is None:
            return
        self._sb_label.setText(
            f"已同步当前书：《{(book.get('title') or '未命名')[:16]}》（切回「🟢 状态」可见）"
        )

    def _on_session_ready(self) -> None:
        self._sb_label.setText("登录态已更新，可前往「状态」页开始阅读")
        # 改动4：扫码后解锁 HARD 失效锁定
        # QTimer.singleShot(0,...) 保证在 UI 线程执行 unlock_after_relogin（操作按钮需要 UI 线程）
        try:
            if hasattr(self, "_status_page") and self._status_page is not None:
                from PySide6.QtCore import QTimer
                QTimer.singleShot(0, self._status_page.unlock_after_relogin)
        except Exception as exc:  # noqa: BLE001
            log.debug("session_ready → 解锁 HARD 锁失败：%s", exc)
        def _t() -> None:
            ok = bool(self._api.check_session())
            self._session_check_done.emit(ok)
        import threading
        threading.Thread(target=_t, daemon=True).start()

    def _on_settings_changed(self) -> None:
        self._sb_label.setText("设置已保存，下一轮阅读将使用新参数")
        # 设置变更后：刷新状态页的摘要状态徽标（用户保存后立刻可见最新提示）
        try:
            if hasattr(self, "_status_page") and self._status_page is not None:
                from PySide6.QtCore import QTimer
                QTimer.singleShot(0, self._status_page._tick_summary_status)
        except Exception as exc:  # noqa: BLE001
            log.debug("settings changed → 刷新摘要状态徽标失败：%s", exc)

    def _on_request_refresh_all_chapters(self, book_id_hint: str = "") -> None:
        """✅ 我已登录完成 后触发：拉章节池。
        book_id_hint: 可选，拦截器捕获到的数字 bookId（优先使用）。
        """
        import threading
        hint = str(book_id_hint or "").strip()
        if hint and hint.isdigit():
            log.debug("_on_request_refresh_all_chapters 使用拦截器捕获的数字 bookId=%s", hint)
        def _task():
            try:
                book_id = None
                # 方式 0：优先使用拦截器捕获的数字 bookId
                if hint and hint.isdigit():
                    book_id = hint
                    # 同时：从当前 reader URL 提取 hex id，注册映射
                    try:
                        cur_url = ""
                        if hasattr(self._login_page, "_web"):
                            try:
                                cur_url = self._login_page._web.url().toString()
                            except Exception:
                                pass
                        if cur_url:
                            import re as _re
                            # 提取 /reader/<hex> 中的 hex id
                            m_hex = _re.search(r"/reader/([A-Za-z0-9_]+)", cur_url)
                            if m_hex:
                                hex_id = m_hex.group(1)
                                self._api.register_book_id_mapping(hex_id, hint)
                                # 更新 current_book.book_id 为 numeric（覆盖 hex）
                                cb = self._api.current_book() or {}
                                if cb:
                                    cb["book_id"] = hint
                                    self._api.set_current_book(cb, source="interceptor_numeric")
                                    log.info("已将 current_book.book_id 从 hex 更新为 numeric=%s", hint)
                    except Exception as _ex:
                        log.debug("注册 bookId 映射失败：%s", _ex)
                # 方式 1：从 login_page 浏览器 URL 解析
                if not book_id:
                    try:
                        cur_url = ""
                        if hasattr(self._login_page, "_edit_current_url"):
                            cur_url = self._login_page._edit_current_url.text().strip()
                        if not cur_url and hasattr(self._login_page, "_web"):
                            try:
                                cur_url = self._login_page._web.url().toString()
                            except Exception:
                                cur_url = ""
                        if cur_url:
                            import re as _re
                            m = _re.search(r"/(\d+)(?:\?|#|$)", cur_url)
                            if not m:
                                m2 = _re.search(r"bookDetail[=/](\d+)", cur_url)
                                if m2:
                                    book_id = m2.group(1)
                            else:
                                book_id = m.group(1)
                    except Exception as _ex:
                        log.debug("解析 book_id 失败：%s", _ex)
                # 方式 2：config 里锁定的 book_id（兜底）
                if not book_id:
                    book_id = (self._config.get("reading.locked_book.book_id") or "").strip()
                if not book_id:
                    log_bus.emit_log("INFO", "暂未检测到已打开的书籍，章节池未拉取。可在状态页锁定书籍后自动刷新。")
                    return
                log_bus.emit_log("INFO", f"📖 尝试拉取书籍 {book_id} 的章节池（web/chapterInfos）...")
                # 确保会话有效
                ok = self._api.ensure_session()
                if not ok:
                    log_bus.emit_log("WARN", "登录态仍无效，跳过章节池拉取。")
                    return
                # 调用 weread_api.refresh_chapters_for_book（内部会 resolve hex→numeric）
                refreshed = self._api.refresh_chapters_for_book(book_id)
                if refreshed:
                    bucket = self._api.scoped_chapters.get(book_id) or {}
                    uids = bucket.get("uids") or []
                    log_bus.emit_log("OK", f"书籍 {book_id} 章节池已加载（共 {len(uids)} 章）。")
                else:
                    log_bus.emit_log("WARN", f"书籍 {book_id} 章节拉取失败，开始阅读时会再次尝试。")
            except Exception as exc:  # noqa: BLE001
                log_bus.emit_log("ERROR", f"登录后拉章节池异常：{exc}")
        threading.Thread(target=_task, daemon=True, name="ChaptersRefreshOnLogin").start()

    def _on_numeric_book_id_detected(self, book_id: str) -> None:
        """拦截器捕获到数字 bookId 时触发：直接用该 bookId 拉章节池。"""
        bid = str(book_id or "").strip()
        if not bid or not bid.isdigit():
            return
        log.info("拦截器传递数字 bookId=%s，触发章节池拉取", bid)
        self._on_request_refresh_all_chapters(bid)
