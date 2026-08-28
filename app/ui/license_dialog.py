"""授权激活对话框（试用过期提示 / 手动输入注册码）。

纯本地校验（app.core.licensing），无任何网络请求。
所有读写都通过传入的共享 LocalDB 实例完成，避免多实例覆盖。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from app.core.licensing import activate, get_status


class LicenseDialog(QDialog):
    """试用结束/手动激活弹窗。activation_done(code) 在成功激活后发射。"""

    activation_done = Signal(str)

    def __init__(self, db, parent=None, *, allow_skip: bool = True) -> None:
        super().__init__(parent)
        self._db = db
        self._activated = False
        self.setWindowTitle("激活 WxReadAssistant")
        self.setModal(True)
        self.setFixedWidth(460)
        self._build_ui(allow_skip)

    # ------------------------------------------------------------------
    def _build_ui(self, allow_skip: bool) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(12)

        title = QLabel("🔑  激活 WxReadAssistant")
        f = QFont()
        f.setPointSize(13)
        f.setBold(True)
        title.setFont(f)
        root.addWidget(title)

        st = get_status(self._db)
        if st["licensed"]:
            tip = f"✅ 当前已激活：{st['masked_code']}\n如需更换注册码，直接输入新码激活即可。"
        elif st["expired"]:
            tip = (
                "⏰ 12 小时免费试用期已结束。\n"
                "请在下方输入 16 位注册码（XXXX-XXXX-XXXX-XXXX）完成激活，\n"
                "激活后可无限期使用全部功能。"
            )
        else:
            tip = (
                f"⏳ 试用期剩余约 {st['hours_left']:.1f} 小时（首次启动：{st['first_run_iso']}）。\n"
                "如已获得注册码，可直接在下方输入激活。"
            )
        tip_label = QLabel(tip)
        tip_label.setWordWrap(True)
        tip_label.setStyleSheet("color:#4B5563;line-height:150%;")
        root.addWidget(tip_label)

        # ---- 输入行 ----
        self._input = QLineEdit()
        self._input.setInputMask("NNNN-NNNN-NNNN-NNNN")
        self._input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mono = QFont("Consolas")
        mono.setPointSize(13)
        mono.setBold(True)
        self._input.setFont(mono)
        self._input.setPlaceholderText("XXXX-XXXX-XXXX-XXXX")
        self._input.textChanged.connect(self._on_input_changed)
        root.addWidget(self._input)

        self._feedback = QLabel(" ")
        self._feedback.setWordWrap(True)
        root.addWidget(self._feedback)

        # ---- 按钮行 ----
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        if allow_skip:
            self._btn_skip = QPushButton("稍后再说")
            self._btn_skip.setMinimumHeight(36)
            self._btn_skip.clicked.connect(self.reject)
            btn_row.addWidget(self._btn_skip)

        self._btn_ok = QPushButton(" 激活 ")
        self._btn_ok.setProperty("role", "primary")
        self._btn_ok.setMinimumHeight(36)
        self._btn_ok.setEnabled(False)
        self._btn_ok.clicked.connect(self._on_activate)
        btn_row.addWidget(self._btn_ok)
        root.addLayout(btn_row)

    # ------------------------------------------------------------------
    def _on_input_changed(self, text: str) -> None:
        # 掩码补位字符是空格；去掉后 16 位即视为可提交
        compact = text.replace(" ", "").replace("-", "")
        self._btn_ok.setEnabled(len(compact) >= 16)
        if self._feedback.property("error"):
            self._feedback.setText(" ")
            self._feedback.setStyleSheet("")
            self._feedback.setProperty("error", False)

    def _on_activate(self) -> None:
        code = self._input.text()
        self._btn_ok.setEnabled(False)
        self._btn_ok.setText("校验中…")
        ok, msg = activate(code, self._db)
        self._btn_ok.setText(" 激活 ")
        if ok:
            self._feedback.setProperty("error", False)
            self._feedback.setStyleSheet("color:#16A34A;font-weight:bold;")
            self._feedback.setText(f"🎉 {msg}")
            self._activated = True
            self._btn_ok.setEnabled(False)
            self.activation_done.emit(msg)
            QTimer.singleShot(1200, self.accept)
        else:
            self._feedback.setProperty("error", True)
            self._feedback.setStyleSheet("color:#DC2626;")
            self._feedback.setText(f"❌ {msg}")
            self._btn_ok.setEnabled(True)
            self._input.selectAll()
            self._input.setFocus()

    # ------------------------------------------------------------------
    def is_activated(self) -> bool:
        return self._activated
