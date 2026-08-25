"""🏆 单主题 QSS · 米白纸书房 Paper Studio。

v5 起不再提供方案 B 星辰夜读切换（主页/设置页仅运行单一浅色主题）。

配色令牌约定（LIGHT = 米白纸 Paper Studio）
  ┌────────────┬────────────┬───────────────────────────────────────┐
  │ paper      │ #F9F9F5    │ 米白纸（页面/面板底色）               │
  │ card       │ #FFFFFF    │ 纯白（卡片/组框底色，更亮一层）        │
  │ border     │ #E5E5DF    │ 细边框灰（分割线/默认边框）           │
  │ ink        │ #1F2937    │ 墨黑（正文，主文字）                  │
  │ ink-soft   │ #374151    │ 墨灰（次级正文/标题字）               │
  │ muted      │ #6B7280    │ 中性灰（辅助文字/muted）             │
  │ brand      │ #1F2937    │ 墨黑（主按钮/主色，实色无装饰）       │
  │ brand2     │ #111827    │ 深墨黑（按钮 hover / 强调）           │
  │ ok         │ #2D9D3C    │ 青苔绿（成功/完成·状态编码）          │
  │ gold       │ #D99B2A    │ 琥珀金（警告/暂停·状态编码）          │
  │ danger     │ #D14343    │ 朱砂红（错误/停止·状态编码）          │
  │ ink-slate  │ #2A303C    │ 深墨 slate（日志底）                  │
  │ scroll-ink │ #D9DEE6    │ slate 日志文字                        │
  │ buff       │ #FBF3DE    │ 米黄（册页纸面 · 当前阅读内卡）       │
  │ buff-bd    │ #E6D7B0    │ 米黄描边                             │
  └────────────┴────────────┴───────────────────────────────────────┘
  字体：Microsoft YaHei UI / PingFang SC → 标题/正文
        JetBrains Mono → ID/时间/数值
  编码：仅金 / 绿 / 红 用作状态点 + 状态按钮；其余所有主色 = 墨黑
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication  # pragma: no cover


# ---------------------------------------------------------------------------
# Light QSS（🏆 白天 · 米白纸 Paper Studio）
# ---------------------------------------------------------------------------
THEME_LIGHT_QSS = r"""
* { outline: 0; }

QWidget {
    font-family: "Microsoft YaHei UI", "PingFang SC", "Segoe UI",
                 "Source Han Sans SC", "Noto Sans SC", sans-serif;
    font-size: 13px;
    color: #1F2937;
    background-color: #F9F9F5;
}

QMainWindow, QDialog { background: #F9F9F5; }

/* ============== 排版分级 ============== */
QLabel.h1 {
    font-size: 24px;
    font-weight: 700;
    color: #1F2937;
    letter-spacing: 2px;
    font-family: "Microsoft YaHei UI", "PingFang SC", sans-serif;
}
QLabel.h2 {
    font-size: 16px;
    font-weight: 600;
    color: #374151;
    letter-spacing: 0.5px;
}
QLabel.strong { font-weight: 600; color: #1F2937; }
QLabel.muted  { color: #6B7280; }

QLabel.mono, QLabel.version, QPlainTextEdit#logView, QLabel.kpi-num {
    /* 等宽：ID / 时间 / 数值 */
    font-family: "JetBrains Mono", "Consolas", "Cascadia Mono", monospace;
}
QLabel.version {
    color: #374151;
    padding: 3px 12px;
    border: 1px solid #E5E5DF;
    border-radius: 999px;
    background: #FFFFFF;
    font-family: "JetBrains Mono", Consolas, monospace;
    font-size: 11px;
    letter-spacing: 0.5px;
}

/* ============== 通用按钮（一行文字 + 图标，无副说明） ============== */
QPushButton {
    border-radius: 10px;
    padding: 8px 16px;
    background: #FFFFFF;
    color: #1F2937;
    border: 1px solid #E5E5DF;
    font-weight: 600;
    font-family: "Microsoft YaHei UI", "PingFang SC", sans-serif;
}
QPushButton:hover   { border-color: #1F2937; color: #111827; background: #FFFFFF; }
QPushButton:pressed { background: #F4F4F0; border-color: #1F2937; }
QPushButton:disabled{ background: #FBFBF7; border-color: #EEEAE3; color: #9AA0AA; }

/* 主按钮：墨黑 primary（= 主操作用，非装饰） */
QPushButton[role="primary"] {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                                stop:0 #2A303C, stop:1 #111827);
    color: #FFFFFF;
    border: 1px solid #0E1319;
    font-weight: 700;
    letter-spacing: 1px;
}
QPushButton[role="primary"]:hover {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                                stop:0 #374151, stop:1 #1F2937);
}
QPushButton[role="primary"]:disabled{ background: #9AA0AA; color: #FFFFFF; border-color:#7E858F; }

/* 状态按钮（成功色 = 绿） */
QPushButton[role="ok"] {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                                stop:0 #39B14B, stop:1 #2D9D3C);
    color: #FFFFFF; border: 1px solid #1E7B2E; font-weight: 700; letter-spacing:1px;
}
QPushButton[role="ok"]:hover {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                                stop:0 #48C25C, stop:1 #1E7B2E);
}
/* 状态按钮（警告色 = 金） */
QPushButton[role="warn"] {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                                stop:0 #EDB44C, stop:1 #D99B2A);
    color: #1F2937; border: 1px solid #9F711D; font-weight: 700; letter-spacing:1px;
}
QPushButton[role="warn"]:hover {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                                stop:0 #F7C25E, stop:1 #9F711D);
}
/* 状态按钮（危险色 = 红） */
QPushButton[role="danger"] {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                                stop:0 #DF5B5B, stop:1 #D14343);
    color: #FFFFFF; border: 1px solid #A72D2D; font-weight: 700; letter-spacing:1px;
}
QPushButton[role="danger"]:hover {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                                stop:0 #EC7070, stop:1 #A72D2D);
}

/* ============== 组框 QGroupBox ============== */
QGroupBox {
    border: 1px solid #E5E5DF;
    border-radius: 12px;
    margin-top: 18px;
    padding-top: 14px;
    padding-left: 12px;
    padding-right: 12px;
    padding-bottom: 12px;
    background: #FFFFFF;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 10px;
    color: #1F2937;
    font-weight: 700;
    font-size: 13px;
    letter-spacing: 0.5px;
    background: #F9F9F5;
    border-left: 2px solid #1F2937;
    border-radius: 2px;
    font-family: "Microsoft YaHei UI", "PingFang SC", sans-serif;
}

/* 三列主卡片（外层 Card Frame） */
QFrame.card {
    background: #FFFFFF;
    border: 1px solid #E5E5DF;
    border-radius: 12px;
}

/* KPI 通栏卡片（米白纸紧凑版） */
QFrame.kpi {
    background: #FFFFFF;
    border: 1px solid #E5E5DF;
    border-radius: 12px;
    min-height: 124px;
}
QLabel.kpi-num {
    font-family: "JetBrains Mono", Consolas, monospace;
    font-size: 26px;
    font-weight: 800;
    color: #1F2937;
    letter-spacing: 0.3px;
}
QLabel.kpi-lbl {
    color: #6B7280;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 1px;
    font-family: "Microsoft YaHei UI", "PingFang SC", sans-serif;
}

/* 仿书脊封面卡片（当前阅读书籍）：左侧 墨黑 细书脊 */
QFrame#bookSpineCard {
    background: #FFFFFF;
    border: 1px solid #E5E5DF;
    border-radius: 12px;
    border-left: 6px solid #1F2937;
}

/* ============== 输入控件 ============== */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    border: 1px solid #E5E5DF;
    border-radius: 8px;
    padding: 0 10px;
    min-height: 32px;
    background: #FFFFFF;
    color: #1F2937;
    selection-background-color: #1F2937;
    selection-color: #FFFFFF;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border: 1px solid #1F2937;
    background: #FFFFFF;
}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    width: 22px; border: none; background: transparent;
}
QSpinBox::up-arrow   { width:9px; height:9px; background:transparent;
                       border-left:4px solid transparent;
                       border-right:4px solid transparent;
                       border-bottom:5px solid #6B7280; }
QSpinBox::down-arrow { width:9px; height:9px; background:transparent;
                       border-left:4px solid transparent;
                       border-right:4px solid transparent;
                       border-top:5px solid #6B7280; }
QSpinBox:disabled QSpinBox::up-arrow   { border-bottom-color: #D0D4DA; }
QSpinBox:disabled QSpinBox::down-arrow { border-top-color:    #D0D4DA; }

QComboBox { min-height: 32px; }
QComboBox::drop-down { width: 26px; height: 30px; border: none; }
QComboBox::down-arrow { width:8px; height:8px;
    border-left:5px solid transparent; border-right:5px solid transparent;
    border-top:6px solid #6B7280; }
QComboBox QAbstractItemView {
    background: #FFFFFF;
    border: 1px solid #E5E5DF;
    border-radius: 8px;
    padding: 4px;
    selection-background-color: #F4F4F0;
    selection-color: #1F2937;
    outline: 0;
}

/* ============== CheckBox / 夜间开关 ============== */
QCheckBox { min-height: 26px; spacing: 8px; padding: 0; color: #1F2937; }
QCheckBox::indicator {
    width: 20px; height: 20px;
    border: 1px solid #1F2937; border-radius: 5px;
    background: #FFFFFF;
}
QCheckBox::indicator:hover        { border-color: #111827; }
QCheckBox::indicator:checked      { background: #1F2937; border-color: #111827; }
QCheckBox::indicator:checked:hover{ background: #111827; }

/* 夜间模式专用切换（objectName="nightModeToggle"）
   日间态：墨黑空心描边；选中态：墨黑实心+星白字 */
QCheckBox#nightModeToggle {
    min-height: 30px;
    padding: 0 16px;
    color: #1F2937;
    background: #FFFFFF;
    border: 1px solid #1F2937;
    border-radius: 999px;
    font-weight: 600;
    letter-spacing: 1px;
    font-family: "Microsoft YaHei UI", "PingFang SC", sans-serif;
}
QCheckBox#nightModeToggle:hover    { border-color: #111827; color: #111827; background: #FFFFFF; }
QCheckBox#nightModeToggle:checked  {
    color: #FFFFFF;
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                                stop:0 #2A303C, stop:1 #1F2937);
    border: 1px solid #0E1319;
}
QCheckBox#nightModeToggle::indicator { width: 0; height: 0; border: none; }

/* ============== 进度条 ============== */
QProgressBar {
    border: 1px solid #E5E5DF;
    border-radius: 999px;
    background: #F1F1ED;
    height: 14px;
    text-align: center;
    color: #1F2937;
    font-weight: 700;
    font-family: "JetBrains Mono", Consolas, monospace;
    letter-spacing: 0.5px;
}
QProgressBar::chunk {
    border-radius: 999px;
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                                stop:0   #374151,
                                stop:1   #1F2937);
}
QProgressBar[role="ok"]::chunk {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                                stop:0 #39B14B, stop:1 #2D9D3C);
}

/* ============== 滚动条（低调细灰） ============== */
QScrollBar:vertical {
    background: transparent; width: 10px; margin: 4px 2px;
}
QScrollBar::handle:vertical {
    background: #D0D4DA; border-radius: 5px; min-height: 30px;
}
QScrollBar::handle:vertical:hover { background: #1F2937; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

QScrollBar:horizontal { background: transparent; height: 10px; }
QScrollBar::handle:horizontal {
    background: #D0D4DA; border-radius: 5px; min-width: 30px;
}
QScrollBar::handle:horizontal:hover { background: #1F2937; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

/* ============== 状态栏 ============== */
QStatusBar {
    background: #FFFFFF;
    border-top: 1px solid #E5E5DF;
    color: #6B7280;
    font-family: "Microsoft YaHei UI", sans-serif;
}

/* ============== 标签页（预留） ============== */
QTabWidget::pane {
    border: 1px solid #E5E5DF; border-radius: 12px;
    background: #FFFFFF; top: -1px;
}
QTabBar::tab {
    background: transparent; padding: 8px 18px; margin-right: 4px;
    border-radius: 8px; color: #6B7280; font-weight: 600;
    font-family: "Microsoft YaHei UI", sans-serif; letter-spacing: 0.5px;
}
QTabBar::tab:selected {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #374151, stop:1 #1F2937);
    color: #FFFFFF;
}
QTabBar::tab:hover:!selected { background: #F4F4F0; color: #1F2937; }

/* ============== 工作日志：深墨 slate 卷 ============== */
QPlainTextEdit#logView {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
                                stop:0 #2A303C, stop:0.5 #313745, stop:1 #2A303C);
    color: #D9DEE6;
    border: 1px solid #1F2937;
    border-radius: 12px;
    padding: 12px 14px;
    font-family: "JetBrains Mono", "Consolas", "Cascadia Mono", monospace;
    font-size: 12px;
    selection-background-color: #1F2937;
    selection-color: #FFFFFF;
}

/* ============== 状态点（绿·金·红·灰 · 仅状态编码） ============== */
QLabel.status-dot {
    min-width: 10px; min-height: 10px; max-width: 10px; max-height: 10px;
    border-radius: 5px;
}
QLabel.status-dot[state="ok"]   { background: #2D9D3C; border: 1px solid #1E7B2E; }
QLabel.status-dot[state="warn"] { background: #D99B2A; border: 1px solid #9F711D; }
QLabel.status-dot[state="bad"]  { background: #D14343; border: 1px solid #A72D2D; }
QLabel.status-dot[state="idle"] { background: #D0D4DA; border: 1px solid #A9AFB8; }

QLabel.cookie-label {
    padding: 4px 14px;
    border-radius: 999px;
    background: #FFFFFF;
    border: 1px solid #E5E5DF;
    font-size: 12px;
    color: #374151;
    font-family: "Microsoft YaHei UI", "PingFang SC", sans-serif;
    letter-spacing: 0.5px;
}
QLabel.cookie-label[state="ok"]  {
    color: #1E7B2E; border-color: #2D9D3C; background: #EFFBF0;
}
QLabel.cookie-label[state="bad"] {
    color: #A72D2D; border-color: #D14343; background: #FCEEED;
}
"""


# ---------------------------------------------------------------------------
# Dark QSS（v5 起不再作为独立主题使用；完全等同 LIGHT · 单主题「米白纸 Paper Studio」）
# 保留 key 只为兼容 apply_global_qss / get_theme_qss 接口稳定。
# ---------------------------------------------------------------------------
THEME_DARK_QSS = THEME_LIGHT_QSS


THEMES = {
    "light": THEME_LIGHT_QSS,
    "dark":  THEME_LIGHT_QSS,   # alias：米白纸单一主题
}


def get_theme_qss(theme_name: str) -> str:
    """获取主题 QSS（v5 起恒为「米白纸 Paper Studio」，仅为接口兼容保留参数。）"""
    return THEME_LIGHT_QSS


def apply_global_qss(app: "QApplication", theme_name: str = "light") -> str:
    """把 QSS 应用到 QApplication。返回实际生效 key（恒 light）。"""
    app.setStyleSheet(THEME_LIGHT_QSS)
    return "light"


class CardPanel:
    """占位类：仅用于 objectName 时命中。使用时直接 QWidget 设 objectName 即可。"""
