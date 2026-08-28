r"""从 app.ui.icon_store.make_app_icon 生成多尺寸 Windows 图标 assets/app_icon.ico。

用法（项目根目录）：
    python scripts\make_icon.py

流程：
  1. 离屏 QApplication + QPainter，按 runtime 同一设计渲染 16/24/32/48/64/128/256
     （<36px 自动走 make_app_icon 的简化分支，与任务栏小图标观感一致）
  2. Pillow 以 append_images 按精确尺寸合成 .ico（≤48 为经典 BMP 项、256 为 PNG 项）

运行时窗口/托盘图标仍是 QPainter 实时绘制；本 ico 供 EXE 资源 /
Inno Setup（SetupIconFile）/ 文件管理器 / 开始菜单 / 快捷方式使用。
改动 make_app_icon 设计后需重跑本脚本。
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SIZES = [16, 24, 32, 48, 64, 128, 256]
OUT = ROOT / "assets" / "app_icon.ico"


def _pixmap_to_pil(pix) -> "Image.Image":  # noqa: F821
    from PySide6.QtCore import QBuffer, QByteArray

    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    pix.save(buf, "PNG")
    buf.close()
    from PIL import Image

    return Image.open(io.BytesIO(bytes(ba))).convert("RGBA")


def main() -> None:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    assert app is not None

    from app.ui.icon_store import make_app_icon

    frames = []
    for s in SIZES:
        pix = make_app_icon(s).pixmap(s, s)
        if pix.isNull():
            raise SystemExit(f"[FAIL] make_app_icon({s}) 渲染失败")
        frames.append(_pixmap_to_pil(pix))

    from PIL import Image

    OUT.parent.mkdir(parents=True, exist_ok=True)
    base = frames[-1]  # 256 设计稿
    base.save(
        OUT,
        format="ICO",
        sizes=[(s, s) for s in SIZES],
        append_images=frames,
        bitmap_format="bmp",
    )
    kb = OUT.stat().st_size / 1024
    print(f"OK  {OUT.relative_to(ROOT)}  ({kb:.1f} KB, sizes={SIZES})")


if __name__ == "__main__":
    main()
