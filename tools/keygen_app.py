r"""WxReadAssistant 注册码生成器（开发者专用，离线）。

用法：
    python tools\keygen_app.py

特性：
    - 一键生成 16 位注册码（XXXX-XXXX-XXXX-XXXX，Crockford Base32）
    - 一键复制到剪贴板
    - 与 app/core/licensing.py 共享同一算法与密钥（单一事实来源）
    - 纯 tkinter，零第三方依赖，不参与应用打包
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tkinter as tk
from tkinter import font as tkfont

from app.core.licensing import generate_code

BG = "#F7F6F1"
FG = "#1F2937"
ACCENT = "#2563EB"
OK_GREEN = "#16A34A"


class KeygenApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("WxReadAssistant 注册码生成器")
        self.configure(bg=BG)
        self.resizable(False, False)
        self._build()
        self._generated_at = ""
        self._generate()  # 启动即给一个码，省一次点击

    # ---------- UI ----------
    def _build(self) -> None:
        pad = dict(padx=24, pady=6)

        title = tk.Label(
            self,
            text="WxReadAssistant 注册码生成器",
            bg=BG,
            fg=FG,
            font=("Microsoft YaHei UI", 14, "bold"),
        )
        title.grid(row=0, column=0, columnspan=2, pady=(20, 2))

        subtitle = tk.Label(
            self,
            text="本地算法 · 无网络校验 · 与应用内嵌密钥一致",
            bg=BG,
            fg="#6B7280",
            font=("Microsoft YaHei UI", 9),
        )
        subtitle.grid(row=1, column=0, columnspan=2, pady=(0, 14))

        mono = tkfont.Font(family="Consolas", size=18, weight="bold")
        self._var_code = tk.StringVar(value="-" * 19)
        self._entry = tk.Entry(
            self,
            textvariable=self._var_code,
            font=mono,
            justify="center",
            state="readonly",
            readonlybackground="#FFFFFF",
            fg=ACCENT,
            relief="flat",
            highlightthickness=1,
            highlightbackground="#D1D5DB",
            highlightcolor=ACCENT,
        )
        self._entry.configure(width=21)
        self._entry.grid(row=2, column=0, columnspan=2, ipady=8, **pad)

        self._var_meta = tk.StringVar(value=" ")
        meta = tk.Label(
            self,
            textvariable=self._var_meta,
            bg=BG,
            fg="#9CA3AF",
            font=("Microsoft YaHei UI", 8),
        )
        meta.grid(row=3, column=0, columnspan=2)

        btn_gen = tk.Button(
            self,
            text="🎲  生成注册码",
            command=self._generate,
            bg=ACCENT,
            fg="#FFFFFF",
            activebackground="#1D4ED8",
            activeforeground="#FFFFFF",
            relief="flat",
            font=("Microsoft YaHei UI", 11, "bold"),
            cursor="hand2",
        )
        btn_gen.grid(row=4, column=0, padx=(24, 6), pady=(14, 6), ipadx=14, ipady=6, sticky="e")

        btn_copy = tk.Button(
            self,
            text="📋  一键复制",
            command=self._copy,
            bg="#E5E7EB",
            fg=FG,
            activebackground="#D1D5DB",
            relief="flat",
            font=("Microsoft YaHei UI", 11, "bold"),
            cursor="hand2",
        )
        btn_copy.grid(row=4, column=1, padx=(6, 24), pady=(14, 6), ipadx=14, ipady=6, sticky="w")

        self._var_feedback = tk.StringVar(value=" ")
        feedback = tk.Label(
            self,
            textvariable=self._var_feedback,
            bg=BG,
            fg=OK_GREEN,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        feedback.grid(row=5, column=0, columnspan=2, pady=(2, 18))

        hint = tk.Label(
            self,
            text="提示：注册码不绑定设备，同一注册码可在任意用户端激活",
            bg=BG,
            fg="#9CA3AF",
            font=("Microsoft YaHei UI", 8),
        )
        hint.grid(row=6, column=0, columnspan=2, pady=(0, 14))

    # ---------- 行为 ----------
    def _generate(self) -> None:
        code = generate_code()
        self._var_code.set(code)
        self._generated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._var_meta.set(f"生成时间：{self._generated_at}")
        self._var_feedback.set(" ")
        # 自动复制，少点一次
        self._copy(show_feedback=False)

    def _copy(self, show_feedback: bool = True) -> None:
        code = self._var_code.get()
        if not code or set(code) == {"-"}:
            return
        self.clipboard_clear()
        self.clipboard_append(code)
        self.update()  # 确保关闭窗口后剪贴板仍保留
        if show_feedback:
            self._var_feedback.set("✓ 已复制到剪贴板")
            self.after(2000, lambda: self._var_feedback.set(" "))


def main() -> None:
    app = KeygenApp()
    app.mainloop()


if __name__ == "__main__":
    main()
