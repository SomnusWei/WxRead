"""Windows 开机自启：使用当前用户注册表 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run。

非 Windows 平台会静默失败，不抛出异常。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_REG_NAME = "WxReadAssistant"


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _target_command() -> str:
    """生成自启时要执行的完整命令行。

    打包后：直接用 exe 路径 + 参数（如果是单文件 exe，sys.executable 就是它）。
    源码模式：pythonw.exe main.py 路径，避免启动时弹黑窗。
    """
    exe = Path(sys.executable)
    if exe.name.lower() in ("python.exe", "pythonw.exe"):
        main_py = Path(__file__).resolve().parents[2] / "main.py"
        # 优先 pythonw 避免命令行窗口
        pythonw = exe.with_name("pythonw.exe")
        runner = pythonw if pythonw.exists() else exe
        return f'"{runner}" "{main_py}" --minimized'
    return f'"{exe}" --minimized'


def set_autostart(enabled: bool) -> tuple[bool, str]:
    """设置开机自启。返回 (是否成功, 描述信息)。"""
    if not _is_windows():
        return False, "当前平台不支持（仅 Windows）"
    try:
        import winreg  # type: ignore
    except ImportError:
        return False, "缺少 winreg 模块"

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            if enabled:
                cmd = _target_command()
                winreg.SetValueEx(key, APP_REG_NAME, 0, winreg.REG_SZ, cmd)
                return True, f"已写入：{cmd}"
            try:
                winreg.DeleteValue(key, APP_REG_NAME)
            except FileNotFoundError:
                pass
            return True, "已移除开机自启项"
    except OSError as exc:
        return False, f"写注册表失败：{exc}"


def is_autostart_enabled() -> bool:
    if not _is_windows():
        return False
    try:
        import winreg  # type: ignore
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ,
        ) as key:
            try:
                val, _ = winreg.QueryValueEx(key, APP_REG_NAME)
                return bool(val)
            except FileNotFoundError:
                return False
    except Exception:  # noqa: BLE001
        return False
