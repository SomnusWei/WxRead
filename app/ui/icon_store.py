"""方案壹·书脊蓝金W（应用图标） + 陆式功能图标（6 操作 + 3 控制）。

全部使用 QPainter 纯代码绘制（无外部资源依赖），任意尺寸可通过 size 参数自适应。
图标配色与主题同步：
    Light → 墨黑描 #1F2937 + 金/绿做状态编码点缀 + 微信蓝主按钮渐变
    Dark  → 星白描 #B8C4DA + 琥珀/青苔绿点缀
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, QSize
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)


APP_VERSION = "2.3.1"       # 与 主界面 版本号 label 同步；打包脚本会自动改写本值
APP_BUILD_ID = "5421465c · 20260913"   # 打包脚本 build_release.py 会写入实际 git short sha / build-timestamp


def app_version_display() -> str:
    """完整版本字符串：主界面标题 / 设置页 / 日志头 共用。

    例：2.2.0 · 8af3c21 · 20260825
    """
    version = APP_VERSION
    build = APP_BUILD_ID or "dev-local"
    return f"{version} · {build}"



# ===========================================================================
# 主题色获取辅助
# ===========================================================================
def _colors(theme: str) -> dict:
    t = str(theme or "").strip().lower()
    if t == "dark":
        return {
            "ink":    QColor("#B8C4DA"),   # 星白描
            "muted":  QColor("#8D9BBC"),   # 月灰
            "moon":   QColor("#8D9BBC"),   # 月灰（别名）
            "star":   QColor("#B8C4DA"),   # 星白（别名）
            "card":   QColor("#1F2E4D"),   # 星云卡片底
            "paper":  QColor("#0E1726"),   # 深夜纸面
            "brand":  QColor("#30ADFF"),   # 极光蓝
            "brand2": QColor("#7C5CFF"),   # 极光紫
            "ok":     QColor("#4C7A54"),   # 青苔绿
            "gold":   QColor("#E6C36A"),   # 琥珀金
            "danger": QColor("#A23F30"),   # 朱砂红
            "white":  QColor("#FFFFFF"),
        }
    # 兜底 light（🏆 米白纸 Paper Studio：墨黑描 + 金/绿仅状态编码）
    return {
        "ink":    QColor("#1F2937"),   # 墨黑描线（主文字/图标边）
        "muted":  QColor("#6B7280"),   # 中性灰（辅助）
        "moon":   QColor("#6B7280"),   # 别名（dark-only 但防御性给出）
        "star":   QColor("#1F2937"),   # 别名
        "card":   QColor("#FFFFFF"),   # 纯白卡片底
        "paper":  QColor("#F9F9F5"),   # 米白纸
        "brand":  QColor("#1F2937"),   # 墨黑（主按钮）
        "brand2": QColor("#111827"),   # 深墨黑（按钮强调）
        "ok":     QColor("#2D9D3C"),   # 青苔绿（状态编码）
        "gold":   QColor("#D99B2A"),   # 琥珀金（状态编码）
        "danger": QColor("#D14343"),   # 朱砂红（状态编码）
        "white":  QColor("#FFFFFF"),   # 纯白
    }


# ===========================================================================
# 应用主图标：壹 · 书脊蓝金 W（精确复刻 HTML 预览）
# ===========================================================================
def make_app_icon(size: int = 64, *, detailed: bool = True) -> QIcon:
    """壹式·书脊蓝金W。

    Args:
        size: 图标像素尺寸（16/24/32/48/64/256 均可）
        detailed: 是否画细节（小尺寸 <32 自动简化）
    """
    detailed = detailed and size >= 36
    s = size
    pix = QPixmap(s, s)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    rd = int(0.1875 * s)  # 圆角半径

    # 1) 蓝色圆角方底（渐变 #30ADFF → #1664FF）
    g = QLinearGradient(0, 0, 0, s)
    g.setColorAt(0.0, QColor("#30ADFF"))
    g.setColorAt(1.0, QColor("#1664FF"))
    p.setPen(QPen(QColor("#0D47A1"), max(1, int(s * 0.024))))
    p.setBrush(QBrush(g))
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, s, s), rd, rd)
    p.drawPath(path)

    # 2) 顶部高光（圆角矩形半透明白）
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor(255, 255, 255, 40)))
    hi = QPainterPath()
    hi.addRoundedRect(QRectF(0, 0, s, s * 0.46), rd, rd)
    p.drawPath(hi)

    # 内亮框（白细线）
    if detailed:
        pw2 = max(1, int(s * 0.012))
        p.setPen(QPen(QColor(255, 255, 255, 90), pw2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        in2 = s * 0.133
        ir = max(2, int(rd * 0.72))
        ip = QPainterPath()
        ip.addRoundedRect(QRectF(in2, in2, s - 2 * in2, s - 2 * in2), ir, ir)
        p.drawPath(ip)

    # 3) 书本本体（占 60% 宽、57% 高）
    bx = s * 0.20
    by = s * 0.21
    bw = s * 0.60
    bh = s * 0.57
    br = max(2, int(rd * 0.30))

    # 书封（深蓝 #083C82 ~ #072B60 深靛）
    bg2 = QLinearGradient(bx, by, bx, by + bh)
    bg2.setColorAt(0.0, QColor("#0B6EC8"))
    bg2.setColorAt(1.0, QColor("#083C82"))
    p.setPen(QPen(QColor("#072B60"), max(1, int(s * 0.020))))
    p.setBrush(QBrush(bg2))
    bp = QPainterPath()
    bp.addRoundedRect(QRectF(bx, by, bw, bh), br, br)
    p.drawPath(bp)

    # 4) 书脊（最左深色竖条）
    sp_w = bw * 0.235
    sg = QLinearGradient(bx, by, bx, by + bh)
    sg.setColorAt(0.0, QColor("#0B4D94"))
    sg.setColorAt(1.0, QColor("#04295A"))
    p.setBrush(QBrush(sg))
    sp = QPainterPath()
    sp.addRoundedRect(QRectF(bx, by, sp_w, bh), br, br)
    p.drawPath(sp)

    # 书脊分隔线
    p.setPen(QPen(QColor("#042048"), max(1, int(s * 0.010))))
    p.drawLine(QPointF(bx + sp_w, by + 1), QPointF(bx + sp_w, by + bh - 1))

    # 5) 书脊上部：金色微信小气泡装饰
    gx = bx + sp_w * 0.5
    gy = by + bh * 0.23
    rr = max(1, sp_w * 0.22)
    if detailed:
        gold_g = QLinearGradient(gx - rr, gy - rr, gx + rr, gy + rr)
        gold_g.setColorAt(0.0, QColor("#FFE49A"))
        gold_g.setColorAt(0.5, QColor("#F4C96B"))
        gold_g.setColorAt(1.0, QColor("#D99B2A"))
        p.setPen(QPen(QColor("#A06C0C"), max(1, int(s * 0.008))))
        p.setBrush(QBrush(gold_g))
        p.drawEllipse(QPointF(gx, gy), rr, rr)
        # 气泡尾巴
        tail = QPainterPath()
        tx = gx + rr * 0.4
        ty = gy + rr * 0.7
        tail.moveTo(QPointF(tx - rr * 0.35, ty + rr * 0.15))
        tail.lineTo(QPointF(tx + rr * 0.35, ty + rr * 0.85))
        tail.lineTo(QPointF(tx + rr * 0.1,  ty + rr * 0.15))
        tail.closeSubpath()
        p.setPen(QPen(QColor("#A06C0C"), max(1, int(s * 0.006))))
        p.setBrush(QBrush(gold_g))
        p.drawPath(tail)
    else:
        # 小尺寸：直接一金色圆点代
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor("#F4C96B")))
        p.drawEllipse(QPointF(gx, gy), rr, rr)

    # 书脊底部两横金
    if detailed:
        p.setPen(QPen(QColor("#F4C96B"), max(1, int(s * 0.012))))
        ly1 = by + bh * 0.80
        ly2 = by + bh * 0.88
        lx1 = bx + sp_w * 0.20
        lx2 = bx + sp_w * 0.80
        p.drawLine(QPointF(lx1, ly1), QPointF(lx2, ly1))
        p.drawLine(QPointF(lx1, ly2), QPointF(lx2, ly2))

    # 6) 封面上方白色书页露出（1px 细节）
    if detailed:
        py = by + bh * 0.11
        ph = bh * 0.13
        pw = bw * 0.58
        px = bx + bw - pw - sp_w * 0.0
        pg = QLinearGradient(px, py, px, py + ph)
        pg.setColorAt(0.0, QColor("#FFFFFF"))
        pg.setColorAt(1.0, QColor("#F5FAFF"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(pg))
        p.setClipRect(QRectF(bx + sp_w, by, bw - sp_w, bh))
        p.drawRoundedRect(QRectF(px, py, pw, ph), 2, 2)
        p.setClipping(False)

    # 7) 封面中央白字斜体 W（主视觉）
    text = "W"
    p.setPen(QPen(QColor("#FFFFFF")))
    p.setBrush(QBrush(QColor("#FFFFFF")))
    f = QFont()
    f.setFamily("Georgia")
    f.setStyleHint(QFont.StyleHint.Serif)
    f.setItalic(True)
    f.setBold(True)
    pt = max(8, int(s * 0.34))
    f.setPointSize(pt)
    p.setFont(f)
    # 居中画 W
    cx = bx + bw - (bw - sp_w) * 0.44  # 稍微偏右（考虑书脊）
    cy = by + bh * 0.60
    p.drawText(
        QRectF(cx - s * 0.3, cy - s * 0.25, s * 0.6, s * 0.5),
        Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter,
        text,
    )

    p.end()
    return QIcon(pix)


# ===========================================================================
# 功能图标通用工具
# ===========================================================================
def _round_icon_pixmap(
    size: int,
    *,
    bg_color: QColor,
    border_color: QColor,
    pen_color: QColor,
) -> tuple[QPixmap, QPainter]:
    """返回带圆角白底/夜蓝底的 方形图标画布 + painter（供功能图标基使用）。"""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    rd = max(2, int(size * 0.22))
    p.setPen(QPen(border_color, max(1, int(size * 0.032))))
    p.setBrush(QBrush(bg_color))
    p.drawRoundedRect(
        QRectF(size * 0.055, size * 0.055, size * 0.89, size * 0.89), rd, rd
    )
    p.setPen(QPen(pen_color, max(1, int(size * 0.030))))
    return pix, p


# ===========================================================================
# 陆式功能操作图标（6 枚）
# ===========================================================================
def icon_login(size: int = 18, theme: str = "light") -> QIcon:
    """扫码登录：古书箱锁 + 锁孔 2×2 二维码点纹（铜锁+二维码）。"""
    c = _colors(theme)
    pix, p = _round_icon_pixmap(
        size, bg_color=c["card"], border_color=c["muted"], pen_color=c["ink"]
    )
    s = size
    p.setPen(QPen(c["ink"], max(1, int(s * 0.032))))
    p.setBrush(Qt.BrushStyle.NoBrush)

    # 锁身（矩形）
    lx, ly = s * 0.28, s * 0.40
    lw, lh = s * 0.44, s * 0.38
    p.drawRoundedRect(QRectF(lx, ly, lw, lh), s * 0.06, s * 0.06)

    # 锁拱（上方半圆）
    arch_r = s * 0.12
    p.setBrush(QBrush(QColor(c["card"])))
    p.drawArc(
        QRectF(lx + lw * 0.25, ly - arch_r * 0.8, arch_r * 2, arch_r * 2),
        0 * 16,
        180 * 16,
    )
    # 重画两根竖边连接
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawLine(QPointF(lx + lw * 0.25, ly), QPointF(lx + lw * 0.25, ly - arch_r * 0.8 + arch_r))
    p.drawLine(QPointF(lx + lw * 0.75, ly), QPointF(lx + lw * 0.75, ly - arch_r * 0.8 + arch_r))

    # 锁孔内 4 个二维码式小方点
    dot = max(2, int(s * 0.06))
    gap = s * 0.06
    ox = lx + lw / 2 - dot - gap / 2
    oy = ly + lh / 2 - dot - gap / 2
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(c["ink"]))
    # 左上、右下填充；右上、左下空心
    p.drawRect(QRectF(ox,       oy,       dot, dot))
    p.drawRect(QRectF(ox + dot + gap, oy + dot + gap, dot, dot))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(c["ink"], max(1, int(s * 0.022))))
    p.drawRect(QRectF(ox + dot + gap, oy,       dot, dot))
    p.drawRect(QRectF(ox,       oy + dot + gap, dot, dot))

    # 四角包金（微信金 4 角 L）
    p.setPen(QPen(c["gold"], max(1, int(s * 0.030))))
    a = s * 0.09
    p.drawLine(QPointF(s * 0.10, s * 0.14), QPointF(s * 0.10, s * 0.10 + a))
    p.drawLine(QPointF(s * 0.10, s * 0.10), QPointF(s * 0.10 + a, s * 0.10))
    p.drawLine(QPointF(s * 0.90, s * 0.14), QPointF(s * 0.90, s * 0.10 + a))
    p.drawLine(QPointF(s * 0.90, s * 0.10), QPointF(s * 0.90 - a, s * 0.10))

    p.end()
    return QIcon(pix)


def icon_shelf(size: int = 18, theme: str = "light") -> QIcon:
    """查看书架：三本书并排书脊 + 中间琥珀书签带。"""
    c = _colors(theme)
    pix, p = _round_icon_pixmap(
        size, bg_color=c["card"], border_color=c["muted"], pen_color=c["ink"]
    )
    s = size
    ink = c["ink"]
    p.setPen(QPen(ink, max(1, int(s * 0.026))))

    book_colors = [
        QColor("#072B60"),     # 左：墨蓝书脊（微信系）
        QColor("#083C82"),     # 中：深靛
        QColor("#1664FF"),     # 右：品牌亮蓝
    ]
    titles = [c["gold"], c["white"], c["gold"]]

    x0 = s * 0.24
    y0 = s * 0.24
    bw = (s - 2 * x0) / 3 * 0.92
    bh = s * 0.56
    gap = (s - 2 * x0) / 3 * 0.08

    for i, col in enumerate(book_colors):
        bx = x0 + i * (bw + gap)
        p.setBrush(QBrush(col))
        p.drawRoundedRect(QRectF(bx, y0, bw, bh), s * 0.04, s * 0.04)
        # 书名线（烫金/白）
        p.setPen(QPen(titles[i], max(1, int(s * 0.024))))
        ty1 = y0 + bh * 0.35
        ty2 = y0 + bh * 0.52
        ty3 = y0 + bh * 0.69
        lx1 = bx + bw * 0.15
        lx2 = bx + bw * 0.85
        p.drawLine(QPointF(lx1, ty1), QPointF(lx2, ty1))
        p.drawLine(QPointF(lx1, ty2), QPointF(lx2, ty2))
        p.setPen(QPen(ink, max(1, int(s * 0.026))))
        p.drawLine(QPointF(lx1, ty3), QPointF(lx2 - bw * 0.15, ty3))

    # 中间琥珀书签带（从上插入）
    mid_x = x0 + 1 * (bw + gap)
    br = bw * 0.22
    tb_x1 = mid_x + bw / 2 - br * 0.7
    tb_x2 = mid_x + bw / 2 + br * 0.7
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(c["gold"]))
    ribbon = QPainterPath()
    ribbon.moveTo(QPointF(tb_x1, y0 - s * 0.04))
    ribbon.lineTo(QPointF(tb_x2, y0 - s * 0.04))
    ribbon.lineTo(QPointF(tb_x2, y0 + bh * 0.45))
    ribbon.lineTo(QPointF((tb_x1 + tb_x2) / 2, y0 + bh * 0.38))
    ribbon.lineTo(QPointF(tb_x1, y0 + bh * 0.45))
    ribbon.closeSubpath()
    p.drawPath(ribbon)

    p.end()
    return QIcon(pix)


def icon_fetch(size: int = 18, theme: str = "light") -> QIcon:
    """获取数据：横卷轴两端木轴 + 斜放羽毛笔（主操作微信蓝渐变）。"""
    c = _colors(theme)
    s = size

    # 背景：使用微信读书蓝（=主按钮色，体现它是「主操作」按钮）
    pix = QPixmap(s, s)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    rd = max(2, int(s * 0.22))
    g = QLinearGradient(0, 0, 0, s)
    g.setColorAt(0.0, c["brand"])
    g.setColorAt(1.0, c["brand2"])
    p.setPen(QPen(c["brand2"], max(1, int(s * 0.032))))
    p.setBrush(QBrush(g))
    p.drawRoundedRect(
        QRectF(s * 0.055, s * 0.055, s * 0.89, s * 0.89), rd, rd
    )

    ink = c["white"]   # 在蓝底上画白色元素
    gold = c["gold"]

    # 卷轴纸身（长方形 · 纸白）
    px = s * 0.21
    py = s * 0.34
    pw = s * 0.58
    ph = s * 0.36
    p.setPen(QPen(ink, max(1, int(s * 0.020))))
    p.setBrush(QBrush(QColor(c["paper"].rgb() if theme == "light" else QColor("#B8C4DA").rgb())))
    p.drawRect(QRectF(px, py, pw, ph))

    # 左右木轴（金色圆柱头）
    cap_h = s * 0.16
    p.setBrush(QBrush(gold))
    p.setPen(QPen(QColor("#A06C0C"), max(1, int(s * 0.018))))
    p.drawRoundedRect(QRectF(px - s * 0.04, py - s * 0.05, s * 0.06, ph + s * 0.10), s * 0.02, s * 0.02)
    p.drawRoundedRect(QRectF(px + pw - s * 0.02, py - s * 0.05, s * 0.06, ph + s * 0.10), s * 0.02, s * 0.02)

    # 卷轴纸身 3 道文字行
    p.setPen(QPen(c["muted"] if theme == "dark" else c["ink"], max(1, int(s * 0.016))))
    for i in range(3):
        y = py + ph * (0.28 + i * 0.24)
        p.drawLine(QPointF(px + s * 0.06, y), QPointF(px + pw - s * 0.08, y))

    # 斜放羽毛笔（从左下到右上）
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(ink))
    path = QPainterPath()
    bx1, by1 = s * 0.23, s * 0.80
    tx, ty = s * 0.80, s * 0.20
    path.moveTo(QPointF(bx1, by1))
    path.cubicTo(
        QPointF(bx1 + s * 0.10, by1 - s * 0.12),
        QPointF(tx - s * 0.12, ty + s * 0.16),
        QPointF(tx, ty),
    )
    path.cubicTo(
        QPointF(tx - s * 0.04, ty + s * 0.06),
        QPointF(bx1 + s * 0.05, by1 - s * 0.02),
        QPointF(bx1 + s * 0.03, by1 + s * 0.03),
    )
    path.closeSubpath()
    p.drawPath(path)

    # 笔尖金点
    p.setBrush(QBrush(gold))
    p.drawEllipse(QPointF(tx, ty), max(1, s * 0.035), max(1, s * 0.035))

    p.end()
    return QIcon(pix)


def icon_refresh_stats(size: int = 18, theme: str = "light") -> QIcon:
    """刷新统计：账本封面 + 三条彩色柱图 + 琥珀趋势箭头 + 朱章（右下"章"点）。"""
    c = _colors(theme)
    pix, p = _round_icon_pixmap(
        size, bg_color=c["card"], border_color=c["muted"], pen_color=c["ink"]
    )
    s = size
    ink = c["ink"]

    # 账本封面（带两条装饰线）
    bx, by = s * 0.22, s * 0.18
    bw, bh = s * 0.56, s * 0.64
    p.setBrush(QBrush(c["card"]))
    p.setPen(QPen(ink, max(1, int(s * 0.03))))
    p.drawRoundedRect(QRectF(bx, by, bw, bh), s * 0.05, s * 0.05)
    # 书脊左竖
    p.drawLine(QPointF(bx + bw * 0.18, by), QPointF(bx + bw * 0.18, by + bh))
    # 封面上方烫金书名线
    p.setPen(QPen(c["gold"], max(1, int(s * 0.020))))
    p.drawLine(QPointF(bx + bw * 0.30, by + bh * 0.22), QPointF(bx + bw * 0.82, by + bh * 0.22))
    p.drawLine(QPointF(bx + bw * 0.30, by + bh * 0.30), QPointF(bx + bw * 0.70, by + bh * 0.30))

    # 三条柱图（左低中高右中 = 绿·蓝·金）
    gp = QRectF(bx + bw * 0.25, by + bh * 0.42, bw * 0.65, bh * 0.42)
    col_c = [c["ok"], c["brand"], c["gold"]]
    h_rel = [0.5, 1.0, 0.75]
    n = 3
    gap = gp.width() * 0.10
    bw_bar = (gp.width() - (n - 1) * gap) / n
    p.setPen(Qt.PenStyle.NoPen)
    for i in range(n):
        x = gp.x() + i * (bw_bar + gap)
        h = gp.height() * h_rel[i]
        y = gp.bottom() - h
        p.setBrush(QBrush(col_c[i]))
        p.drawRoundedRect(QRectF(x, y, bw_bar, h), s * 0.02, s * 0.02)

    # 琥珀趋势箭头
    p.setPen(QPen(c["gold"], max(2, int(s * 0.040)),
                  Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    ax1 = bx + bw * 0.28
    ay1 = by + bh * 0.92
    ax2 = bx + bw * 0.75
    ay2 = by + bh * 0.50
    p.drawLine(QPointF(ax1, ay1), QPointF(ax2, ay2))
    # 箭头头
    p.setBrush(QBrush(c["gold"]))
    ah = QPainterPath()
    ah.moveTo(QPointF(ax2, ay2 - s * 0.06))
    ah.lineTo(QPointF(ax2 + s * 0.06, ay2 + s * 0.02))
    ah.lineTo(QPointF(ax2 - s * 0.02, ay2 + s * 0.04))
    ah.closeSubpath()
    p.drawPath(ah)

    # 右下朱章
    p.setBrush(QBrush(c["danger"]))
    p.setPen(QPen(ink, max(1, int(s * 0.018))))
    cs = s * 0.14
    p.drawRoundedRect(QRectF(s * 0.70, s * 0.70, cs, cs), s * 0.02, s * 0.02)

    p.end()
    return QIcon(pix)


def icon_refresh_progress(size: int = 18, theme: str = "light") -> QIcon:
    """刷新进度：摊开的白书 + 朱砂丝带书签 + 琥珀高亮行 + 绿进度标签。"""
    c = _colors(theme)
    pix, p = _round_icon_pixmap(
        size, bg_color=c["card"], border_color=c["muted"], pen_color=c["ink"]
    )
    s = size
    ink = c["ink"]
    p.setPen(QPen(ink, max(1, int(s * 0.026))))
    p.setBrush(QBrush(c["paper"] if theme == "light" else c["card"]))

    # 书本左右
    cy = s * 0.55
    w_h = s * 0.42
    w_w = s * 0.28
    # 左半本
    left = QPainterPath()
    left.moveTo(QPointF(s * 0.5 - s * 0.02, cy - w_h * 0.20))
    left.quadTo(QPointF(s * 0.5 - w_w * 0.55, cy - w_h * 0.30),
                QPointF(s * 0.5 - w_w, cy - w_h * 0.05))
    left.lineTo(QPointF(s * 0.5 - w_w, cy + w_h * 0.55))
    left.quadTo(QPointF(s * 0.5 - w_w * 0.5, cy + w_h * 0.50),
                QPointF(s * 0.5 - s * 0.02, cy + w_h * 0.78))
    left.closeSubpath()
    p.drawPath(left)

    # 右半本
    right = QPainterPath()
    right.moveTo(QPointF(s * 0.5 + s * 0.02, cy - w_h * 0.20))
    right.quadTo(QPointF(s * 0.5 + w_w * 0.55, cy - w_h * 0.30),
                 QPointF(s * 0.5 + w_w, cy - w_h * 0.05))
    right.lineTo(QPointF(s * 0.5 + w_w, cy + w_h * 0.55))
    right.quadTo(QPointF(s * 0.5 + w_w * 0.5, cy + w_h * 0.50),
                 QPointF(s * 0.5 + s * 0.02, cy + w_h * 0.78))
    right.closeSubpath()
    p.drawPath(right)

    # 中央书脊缝
    p.drawLine(QPointF(s * 0.5, cy - w_h * 0.20), QPointF(s * 0.5, cy + w_h * 0.78))

    # 文字行（4 条）
    p.setPen(QPen(c["muted"], max(1, int(s * 0.016))))
    for i in range(4):
        ly = cy - w_h * 0.02 + i * (w_h * 0.16)
        lx1 = s * 0.5 - w_w + s * 0.05
        lx2 = s * 0.5 - s * 0.06
        if i == 2:
            # 第三行琥珀高亮条
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(c["gold"]))
            p.drawRect(QRectF(lx1, ly - s * 0.018, lx2 - lx1, s * 0.045))
            # 右半对称高亮
            rx1 = s * 0.5 + s * 0.06
            rx2 = s * 0.5 + w_w - s * 0.05
            p.drawRect(QRectF(rx1, ly - s * 0.018, rx2 - rx1, s * 0.045))
            p.setPen(QPen(c["muted"], max(1, int(s * 0.016))))
            p.setBrush(Qt.BrushStyle.NoBrush)
        else:
            p.drawLine(QPointF(lx1, ly), QPointF(lx2, ly))
            p.drawLine(QPointF(s * 0.5 + s * 0.06, ly), QPointF(s * 0.5 + w_w - s * 0.05, ly))

    # 朱砂丝带书签（从右页插入）
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(c["danger"]))
    rb = QPainterPath()
    sx = s * 0.75
    rb.moveTo(QPointF(sx - s * 0.04, s * 0.22))
    rb.lineTo(QPointF(sx + s * 0.04, s * 0.22))
    rb.lineTo(QPointF(sx + s * 0.04, s * 0.60))
    rb.lineTo(QPointF(sx, s * 0.55))
    rb.lineTo(QPointF(sx - s * 0.04, s * 0.60))
    rb.closeSubpath()
    p.drawPath(rb)

    # 绿色进度小标签 "42%"
    p.setBrush(QBrush(c["ok"]))
    p.setPen(QPen(c["ok"].darker(120), max(1, int(s * 0.015))))
    tw, th = s * 0.22, s * 0.12
    p.drawRoundedRect(QRectF(s * 0.52, cy - w_h * 0.14, tw, th), s * 0.025, s * 0.025)
    # 标签文字用墨黑的 42% 白描
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(c["white"]))
    f = QFont()
    f.setBold(True)
    f.setPointSize(max(5, int(s * 0.11)))
    f.setFamily("JetBrains Mono")
    p.setFont(f)
    p.drawText(
        QRectF(s * 0.52, cy - w_h * 0.14, tw, th),
        Qt.AlignmentFlag.AlignCenter,
        "42%",
    )

    p.end()
    return QIcon(pix)


def icon_report(size: int = 18, theme: str = "light") -> QIcon:
    """查看报告：打开的卷轴纸（米黄纸 + 朱砂丝带 + 右上 3 根彩色数据柱）。"""
    c = _colors(theme)
    s = size
    pix, p = _round_icon_pixmap(
        size, bg_color=c["card"], border_color=c["muted"], pen_color=c["ink"]
    )
    paper = QColor(c["paper"])
    paper_line = QColor("#E5E5DF" if theme == "light" else "#444b5a")
    ink = c["ink"]

    # 卷轴纸：两横书脊（左右） + 纸面
    x0, y0 = s * 0.15, s * 0.22
    bw = s * 0.62
    bh = s * 0.56
    # 纸面
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(paper))
    p.drawRoundedRect(QRectF(x0, y0, bw, bh), s * 0.05, s * 0.05)
    # 纸边框
    p.setPen(QPen(paper_line, max(1, int(s * 0.022))))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(x0, y0, bw, bh), s * 0.05, s * 0.05)

    # 文字横线 3 条
    p.setPen(QPen(QColor(c["muted"]), max(1, int(s * 0.018))))
    for i in range(3):
        ty = y0 + bh * (0.25 + 0.2 * i)
        p.drawLine(QPointF(x0 + bw * 0.14, ty), QPointF(x0 + bw * 0.88, ty))

    # 朱砂红丝带：左侧竖丝带盖在纸边
    rx = x0
    ry1 = y0 - s * 0.04
    ry2 = y0 + bh + s * 0.04
    rw = s * 0.10
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor(c["danger"])))
    p.drawRoundedRect(QRectF(rx - rw * 0.5, ry1, rw, ry2 - ry1), s * 0.02, s * 0.02)

    # 右上 3 根数据柱
    bx0 = x0 + bw - s * 0.22
    by0 = y0 + s * 0.06
    bw_small = s * 0.045
    gap_small = s * 0.025
    colors = [QColor(c["ok"]), QColor(c["gold"]), QColor(c["danger"])]
    heights = [s * 0.10, s * 0.18, s * 0.26]
    for i, (col, h) in enumerate(zip(colors, heights)):
        x = bx0 + i * (bw_small + gap_small)
        y = by0 + (max(heights) - h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(col))
        p.drawRoundedRect(QRectF(x, y, bw_small, h), s * 0.015, s * 0.015)

    # 右下一颗装饰金珠点
    p.setBrush(QBrush(QColor(c["gold"])))
    p.drawEllipse(QPointF(x0 + bw - s * 0.08, y0 + bh - s * 0.08), s * 0.028, s * 0.028)
    p.end()
    return QIcon(pix)


def icon_config_center(size: int = 18, theme: str = "light") -> QIcon:
    """配置中心：六角墨水瓶 + 墨字小标 + 双钢笔（棕+绿）+ 齿轮螺丝钉边。"""
    c = _colors(theme)
    pix, p = _round_icon_pixmap(
        size, bg_color=c["card"], border_color=c["muted"], pen_color=c["ink"]
    )
    s = size
    ink = c["ink"]
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(ink, max(1, int(s * 0.028))))

    # 六角瓶身
    cx, cy = s * 0.45, s * 0.60
    r = s * 0.24
    hex_path = QPainterPath()
    for i in range(6):
        import math
        a = math.radians(30 + i * 60)
        x = cx + r * math.cos(a)
        y = cy + r * math.sin(a)
        if i == 0:
            hex_path.moveTo(x, y)
        else:
            hex_path.lineTo(x, y)
    hex_path.closeSubpath()
    # 墨水瓶填色
    p.setBrush(QBrush(c["brand"] if theme == "light" else c["aurora2" if False else "brand"]))
    p.drawPath(hex_path)

    # 墨水瓶瓶颈
    neck_x1 = cx - r * 0.45
    neck_x2 = cx + r * 0.45
    neck_y = cy - r * 0.95
    p.setPen(QPen(ink, max(1, int(s * 0.028))))
    p.setBrush(QBrush(c["muted"] if theme == "dark" else c["paper"]))
    p.drawRect(QRectF(neck_x1, neck_y, neck_x2 - neck_x1, s * 0.08))

    # 墨字标签（矩形盖在瓶身）
    lbl_w = r * 1.1
    lbl_h = r * 0.72
    p.setBrush(QBrush(c["paper"] if theme == "light" else QColor("#E6C36A")))
    p.setPen(QPen(ink, max(1, int(s * 0.022))))
    p.drawRoundedRect(QRectF(cx - lbl_w / 2, cy - lbl_h / 2 + s * 0.02, lbl_w, lbl_h), s * 0.015, s * 0.015)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(ink))
    f = QFont()
    f.setBold(True)
    f.setPointSize(max(5, int(s * 0.22)))
    p.setFont(f)
    p.drawText(
        QRectF(cx - lbl_w / 2, cy - lbl_h / 2 + s * 0.02, lbl_w, lbl_h),
        Qt.AlignmentFlag.AlignCenter,
        "墨",
    )

    # 两支斜钢笔（交叉）
    p.setPen(QPen(c["gold"], max(2, int(s * 0.032)), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(s * 0.70, s * 0.24), QPointF(s * 0.92, s * 0.80))
    p.setPen(QPen(c["ok"], max(2, int(s * 0.032)), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(s * 0.22, s * 0.12), QPointF(s * 0.50, s * 0.12))

    # 齿轮螺丝钉（右上）
    gx, gy = s * 0.78, s * 0.30
    gr = s * 0.07
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(c["ink"]))
    p.drawEllipse(QPointF(gx, gy), gr, gr)
    p.setBrush(QBrush(c["card"]))
    p.drawEllipse(QPointF(gx, gy), gr * 0.5, gr * 0.5)
    p.setPen(QPen(c["ink"], max(1, int(s * 0.018))))
    for i in range(6):
        import math
        a = math.radians(i * 60)
        x1 = gx + gr * 1.0 * math.cos(a)
        y1 = gy + gr * 1.0 * math.sin(a)
        x2 = gx + gr * 1.35 * math.cos(a)
        y2 = gy + gr * 1.35 * math.sin(a)
        p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    p.end()
    return QIcon(pix)


# ===========================================================================
# 控制按钮图标（3 枚：开始/暂停/停止）
# ===========================================================================
def icon_start(size: int = 18, theme: str = "light") -> QIcon:
    """控制·开始：木柄 + 四叶花印章"始"字。"""
    c = _colors(theme)
    s = size
    pix = QPixmap(s, s)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    # 成功绿渐变圆底
    g = QLinearGradient(0, 0, 0, s)
    g.setColorAt(0, QColor("#36b046") if theme != "dark" else QColor("#5ea077"))
    g.setColorAt(1, c["ok"])
    rd = int(s * 0.22)
    p.setPen(QPen(c["ok"].darker(120), max(1, int(s * 0.030))))
    p.setBrush(QBrush(g))
    p.drawRoundedRect(QRectF(s * 0.05, s * 0.05, s * 0.90, s * 0.90), rd, rd)

    # 中央 方印章（朱白）
    ix, iy = s * 0.28, s * 0.20
    iw, ih = s * 0.44, s * 0.56
    p.setBrush(QBrush(c["white"]))
    p.setPen(QPen(c["white"], max(1, int(s * 0.030))))
    p.drawRect(QRectF(ix, iy, iw, ih))

    # "始"字（墨笔）
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(c["ok"]))
    f = QFont()
    f.setBold(True)
    f.setPointSize(max(7, int(s * 0.48)))
    f.setFamily("Microsoft YaHei UI")
    p.setFont(f)
    p.drawText(QRectF(ix, iy, iw, ih), Qt.AlignmentFlag.AlignCenter, "始")

    # 右下方三角形木柄
    p.setPen(QPen(c["gold"].darker(120), max(1, int(s * 0.025))))
    p.setBrush(QBrush(c["gold"]))
    handle = QPainterPath()
    handle.moveTo(QPointF(s * 0.50, s * 0.80))
    handle.lineTo(QPointF(s * 0.90, s * 0.96))
    handle.lineTo(QPointF(s * 0.62, s * 0.92))
    handle.closeSubpath()
    p.drawPath(handle)

    # 四叶花（四角金色圆点装饰）
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(c["gold"]))
    dot_r = max(1, int(s * 0.045))
    for pt in [(0.16, 0.16), (0.84, 0.16), (0.16, 0.84)]:
        p.drawEllipse(QPointF(s * pt[0], s * pt[1]), dot_r, dot_r)

    p.end()
    return QIcon(pix)


def icon_pause(size: int = 18, theme: str = "light") -> QIcon:
    """控制·暂停：一对黄铜尺 + 铆钉 + 刻度。"""
    c = _colors(theme)
    s = size
    pix = QPixmap(s, s)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    g = QLinearGradient(0, 0, 0, s)
    g.setColorAt(0.0, QColor("#F0B648"))
    g.setColorAt(1.0, c["gold"])
    rd = int(s * 0.22)
    p.setPen(QPen(c["gold"].darker(140), max(1, int(s * 0.030))))
    p.setBrush(QBrush(g))
    p.drawRoundedRect(QRectF(s * 0.05, s * 0.05, s * 0.90, s * 0.90), rd, rd)

    # 左尺
    rw = s * 0.18
    rx1 = s * 0.28
    rx2 = s * 0.54
    ry = s * 0.18
    rh = s * 0.64
    for rx in (rx1, rx2):
        p.setPen(QPen(c["gold"].darker(160), max(1, int(s * 0.022))))
        p.setBrush(QBrush(c["gold"].lighter(110)))
        p.drawRoundedRect(QRectF(rx, ry, rw, rh), s * 0.05, s * 0.05)
        # 刻度（短-长-短-长）
        p.setPen(QPen(c["gold"].darker(160), max(1, int(s * 0.018))))
        n_tick = 7
        for i in range(1, n_tick):
            ty = ry + i * rh / n_tick
            long = (i % 2 == 0)
            tl = rw * (0.42 if long else 0.22)
            p.drawLine(QPointF(rx + rw * 0.08, ty), QPointF(rx + rw * 0.08 + tl, ty))
        # 铆钉
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(c["gold"].darker(160)))
        rr = max(1, s * 0.045)
        p.drawEllipse(QPointF(rx + rw / 2, ry + s * 0.08), rr, rr)
        p.drawEllipse(QPointF(rx + rw / 2, ry + rh - s * 0.08), rr, rr)

    p.end()
    return QIcon(pix)


def icon_stop(size: int = 18, theme: str = "light") -> QIcon:
    """控制·停止：梯形柄 + 方印角花"止"。"""
    c = _colors(theme)
    s = size
    pix = QPixmap(s, s)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    g = QLinearGradient(0, 0, 0, s)
    g.setColorAt(0.0, QColor("#e66f6b") if theme != "dark" else QColor("#b8573f"))
    g.setColorAt(1.0, c["danger"])
    rd = int(s * 0.22)
    p.setPen(QPen(c["danger"].darker(130), max(1, int(s * 0.030))))
    p.setBrush(QBrush(g))
    p.drawRoundedRect(QRectF(s * 0.05, s * 0.05, s * 0.90, s * 0.90), rd, rd)

    # 方印章（白底 + "止"）
    ix, iy = s * 0.24, s * 0.18
    iw, ih = s * 0.52, s * 0.56
    p.setBrush(QBrush(c["white"]))
    p.setPen(QPen(c["white"], max(1, int(s * 0.030))))
    p.drawRect(QRectF(ix, iy, iw, ih))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(c["danger"]))
    f = QFont()
    f.setBold(True)
    f.setPointSize(max(7, int(s * 0.50)))
    f.setFamily("Microsoft YaHei UI")
    p.setFont(f)
    p.drawText(QRectF(ix, iy, iw, ih), Qt.AlignmentFlag.AlignCenter, "止")

    # 四个角花（角上小黑方）
    p.setBrush(QBrush(c["danger"].darker(140)))
    cs = max(2, int(s * 0.07))
    for px, py in [(s * 0.22, s * 0.16), (s * 0.78 - cs, s * 0.16),
                    (s * 0.22, s * 0.74 - cs), (s * 0.78 - cs, s * 0.74 - cs)]:
        p.drawRect(QRectF(px, py, cs, cs))

    # 梯形柄（下方）
    p.setPen(QPen(c["danger"].darker(140), max(1, int(s * 0.024))))
    p.setBrush(QBrush(c["danger"].lighter(120)))
    han = QPainterPath()
    han.moveTo(QPointF(s * 0.40, s * 0.80))
    han.lineTo(QPointF(s * 0.60, s * 0.80))
    han.lineTo(QPointF(s * 0.68, s * 0.96))
    han.lineTo(QPointF(s * 0.32, s * 0.96))
    han.closeSubpath()
    p.drawPath(han)

    p.end()
    return QIcon(pix)


# ===========================================================================
# 附加：配置中心内部三枚（返回·保存·验证）—— 陆式同风格
# ===========================================================================
def icon_back_home(size: int = 18, theme: str = "light") -> QIcon:
    """返回/首页：斜向箭头 + 底部小屋檐。"""
    c = _colors(theme)
    s = size
    pix = QPixmap(s, s)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    # 斜向箭头（←↘ 风格）
    pw = max(1, int(s * 0.07))
    pen = QPen(c["ink"], pw, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    arrow = QPainterPath()
    # 尾
    arrow.moveTo(QPointF(s * 0.80, s * 0.50))
    arrow.lineTo(QPointF(s * 0.28, s * 0.50))
    # 箭头 V
    arrow.moveTo(QPointF(s * 0.48, s * 0.30))
    arrow.lineTo(QPointF(s * 0.28, s * 0.50))
    arrow.lineTo(QPointF(s * 0.48, s * 0.70))
    p.drawPath(arrow)

    # 底部小屋檐（象征「家」）
    p.setPen(QPen(c["muted"], pw, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    roof = QPainterPath()
    roof.moveTo(QPointF(s * 0.28, s * 0.82))
    roof.lineTo(QPointF(s * 0.50, s * 0.68))
    roof.lineTo(QPointF(s * 0.72, s * 0.82))
    p.drawPath(roof)
    # 墙
    p.drawLine(QPointF(s * 0.35, s * 0.82), QPointF(s * 0.35, s * 0.92))
    p.drawLine(QPointF(s * 0.65, s * 0.82), QPointF(s * 0.65, s * 0.92))
    p.drawLine(QPointF(s * 0.35, s * 0.92), QPointF(s * 0.65, s * 0.92))
    p.end()
    return QIcon(pix)


def icon_save(size: int = 18, theme: str = "light") -> QIcon:
    """保存：软盘外形 · 中央金色小圆点象征写入。"""
    c = _colors(theme)
    s = size
    pix = QPixmap(s, s)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    rd = int(s * 0.18)
    pw = max(1, int(s * 0.07))
    # 软盘外框（圆角方）
    g = QLinearGradient(0, 0, 0, s)
    g.setColorAt(0.0, c["card"])
    g.setColorAt(1.0, c["paper"])
    p.setBrush(QBrush(g))
    p.setPen(QPen(c["ink"], pw))
    p.drawRoundedRect(QRectF(s * 0.08, s * 0.08, s * 0.84, s * 0.84), rd, rd)

    # 上方滑盖区（深色矩形）
    g2 = QLinearGradient(0, s * 0.18, 0, s * 0.52)
    g2.setColorAt(0.0, c["muted"] if theme != "dark" else c["moon"])
    g2.setColorAt(1.0, c["ink"])
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(g2))
    p.drawRoundedRect(
        QRectF(s * 0.20, s * 0.10, s * 0.46, s * 0.34), max(1, rd // 2), max(1, rd // 2)
    )

    # 滑盖金属槽
    p.setPen(QPen(c["paper"], max(1, pw - 1)))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(
        QRectF(s * 0.28, s * 0.20, s * 0.28, s * 0.16), 2, 2
    )

    # 中央金色写入点
    r = max(2, s * 0.07)
    gold_g = QLinearGradient(s * 0.50 - r, s * 0.62 - r, s * 0.50 + r, s * 0.62 + r)
    gold_g.setColorAt(0.0, QColor("#FFE49A"))
    gold_g.setColorAt(1.0, c["gold"])
    p.setPen(QPen(c["gold"].darker(130), max(1, pw - 1)))
    p.setBrush(QBrush(gold_g))
    p.drawEllipse(QPointF(s * 0.50, s * 0.62), r, r)

    # 下方两条短纹（标签边）
    p.setPen(QPen(c["muted"], max(1, pw - 1)))
    p.drawLine(QPointF(s * 0.28, s * 0.82), QPointF(s * 0.42, s * 0.82))
    p.drawLine(QPointF(s * 0.58, s * 0.82), QPointF(s * 0.72, s * 0.82))
    p.end()
    return QIcon(pix)


def icon_verify(size: int = 18, theme: str = "light") -> QIcon:
    """验证/对勾：盾牌形底 + 中央金绿 √。"""
    c = _colors(theme)
    s = size
    pix = QPixmap(s, s)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    # 盾牌形（上宽下尖）
    pw = max(1, int(s * 0.06))
    g = QLinearGradient(0, 0, 0, s)
    if theme == "dark":
        g.setColorAt(0.0, QColor("#2A3D5E"))
        g.setColorAt(1.0, QColor("#162032"))
    else:
        g.setColorAt(0.0, QColor("#FFFFFF"))
        g.setColorAt(1.0, QColor("#F0EFEA"))
    shield = QPainterPath()
    shield.moveTo(QPointF(s * 0.10, s * 0.18))
    shield.lineTo(QPointF(s * 0.90, s * 0.18))
    shield.lineTo(QPointF(s * 0.90, s * 0.50))
    shield.quadTo(QPointF(s * 0.68, s * 0.76), QPointF(s * 0.50, s * 0.95))
    shield.quadTo(QPointF(s * 0.32, s * 0.76), QPointF(s * 0.10, s * 0.50))
    shield.closeSubpath()
    p.setBrush(QBrush(g))
    p.setPen(QPen(c["ink"], pw, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.drawPath(shield)

    # 盾牌内顶部金小圆（徽章扣）
    r = max(1.5, s * 0.05)
    gold_g = QLinearGradient(s * 0.50 - r, s * 0.30 - r, s * 0.50 + r, s * 0.30 + r)
    gold_g.setColorAt(0.0, QColor("#FFE49A"))
    gold_g.setColorAt(1.0, c["gold"])
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(gold_g))
    p.drawEllipse(QPointF(s * 0.50, s * 0.30), r, r)

    # 中央大对勾（金绿渐变）
    pw2 = max(2, int(s * 0.08))
    v_pen = QPen(c["ok"], pw2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    vg = QLinearGradient(s * 0.25, s * 0.40, s * 0.80, s * 0.80)
    vg.setColorAt(0.0, c["gold"])
    vg.setColorAt(1.0, c["ok"])
    # Qt 没有直接 gradient pen → 用渐变 brush 的 path 代替：勾是一粗路径
    check = QPainterPath()
    check.moveTo(QPointF(s * 0.25, s * 0.58))
    check.lineTo(QPointF(s * 0.44, s * 0.76))
    check.lineTo(QPointF(s * 0.78, s * 0.40))
    pen2 = QPen(vg, pw2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen2)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(check)
    p.end()
    return QIcon(pix)


# ===========================================================================
# 辅助：为一组控件尺寸重新设置图标（按按钮尺寸自适应图标大小）
# ===========================================================================
def icon_size_for_button(height_px: int) -> int:
    """根据按钮实际高度，返回合适的图标尺寸。"""
    if height_px >= 42:
        return 24
    if height_px >= 34:
        return 20
    return 18


def icon_dim_for_toolbar(height_px: int) -> QSize:
    s = icon_size_for_button(height_px)
    return QSize(s, s)
