"""清新简约风格的全局 QSS 样式：浅色+蓝色主色，卡片感，圆角。"""

GLOBAL_QSS = """
QWidget {
    font-family: "Microsoft YaHei UI", "PingFang SC", "Segoe UI", sans-serif;
    font-size: 13px;
    color: #2f3b52;
    background-color: #f6f8fc;
}

QMainWindow, QDialog {
    background: #f6f8fc;
}

/* ============ 标签页（Tab） ============ */
QTabWidget::pane {
    border: 1px solid #e5eaf2;
    border-radius: 12px;
    background: #ffffff;
    top: -1px;
}
QTabBar::tab {
    background: transparent;
    padding: 8px 18px;
    margin-right: 4px;
    border-radius: 8px;
    color: #7a879f;
    font-weight: 500;
}
QTabBar::tab:selected {
    background: #2d6cdf;
    color: #ffffff;
}
QTabBar::tab:hover:!selected {
    background: #e7eefb;
    color: #2d6cdf;
}

/* ============ 状态栏 ============ */
QStatusBar {
    background: #ffffff;
    border-top: 1px solid #eef2f8;
    color: #5f6c85;
}

/* ============ 滚动条 ============ */
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 4px 2px 4px 2px;
}
QScrollBar::handle:vertical {
    background: #c7cee0;
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: #9ea7c1;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
}
QScrollBar::handle:horizontal {
    background: #c7cee0;
    border-radius: 5px;
    min-width: 30px;
}
QScrollBar::handle:horizontal:hover {
    background: #9ea7c1;
}

/* ============ 进度条 ============ */
QProgressBar {
    border: none;
    border-radius: 8px;
    background: #eef2f8;
    height: 18px;
    text-align: center;
    color: #ffffff;
    font-weight: 600;
}
QProgressBar::chunk {
    border-radius: 8px;
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                                stop:0 #58a1ff, stop:1 #2d6cdf);
}

/* ============ 通用按钮（当 UI 内未覆盖样式时使用） ============ */
QPushButton {
    border-radius: 8px;
    padding: 6px 14px;
    background: #eef2f8;
    color: #2f3b52;
    border: none;
}
QPushButton:hover { background: #e2e8f4; }
QPushButton:pressed { background: #d6deee; }
QPushButton:disabled { background: #eef2f8; color: #b5bccf; }

/* ============ 输入框 ============ */
QLineEdit, QComboBox, QSpinBox {
    border: 1px solid #dfe4ef;
    border-radius: 6px;
    padding: 0 8px;
    min-height: 32px;
    background: #ffffff;
    selection-background-color: #2d6cdf;
    selection-color: #ffffff;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
    border: 1px solid #2d6cdf;
}
QSpinBox::up-button, QSpinBox::down-button {
    width: 22px;
    border: none;
    background: transparent;
}
QSpinBox::up-arrow, QSpinBox::down-arrow {
    width: 9px;
    height: 9px;
    background: transparent;
}
QSpinBox::up-arrow {
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid #4a5675;
}
QSpinBox::down-arrow {
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #4a5675;
}
QSpinBox:disabled QSpinBox::up-arrow { border-bottom-color: #b5bccf; }
QSpinBox:disabled QSpinBox::down-arrow { border-top-color: #b5bccf; }

/* ============ 复选框 ============ */
QCheckBox {
    min-height: 24px;
    padding: 0;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border: 1px solid #c7cee0;
    border-radius: 4px;
    background: #ffffff;
}
QCheckBox::indicator:hover { border: 1px solid #2d6cdf; }
QCheckBox::indicator:checked {
    background: #2d6cdf;
    border: 1px solid #2d6cdf;
}

/* ============ 组合框下拉 ============ */
QComboBox {
    min-height: 32px;
}
QComboBox::drop-down {
    width: 24px;
    height: 30px;
    border: none;
}
QComboBox QAbstractItemView {
    background: #ffffff;
    border: 1px solid #e5eaf2;
    border-radius: 8px;
    padding: 4px;
    selection-background-color: #e7eefb;
    selection-color: #2d6cdf;
    outline: 0;
}

/* ============ 纯文本日志框 ============ */
QPlainTextEdit {
    background: #fbfcfe;
    border: 1px solid #e5eaf2;
    border-radius: 10px;
    padding: 10px;
    color: #34405b;
    selection-background-color: #d7e6ff;
}

/* ============ 进度卡片容器 ============ */
CardPanel {
    background: #ffffff;
    border: 1px solid #e5eaf2;
    border-radius: 14px;
}

/* ============ 分组标题 ============ */
QGroupBox {
    border: 1px solid #e5eaf2;
    border-radius: 10px;
    margin-top: 16px;
    padding-top: 10px;
    background: #ffffff;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: #2d6cdf;
}
"""


class CardPanel:
    """占位类：仅用于 QSS 中命中。真正使用请用 QWidget 并设 objectName。"""


def apply_global_qss(app) -> None:  # type: ignore[no-untyped-def]
    app.setStyleSheet(GLOBAL_QSS)
