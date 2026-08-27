"""📘 个人阅读报告对话框。

设计理念：
  - 对齐「米白纸 Paper Studio」视觉：paper #F9F9F5 底 + 卡片 #FFFFFF + 边框 #E5E5DF + 墨黑文字
  - 装饰 ❦ 花押 + 朱砂金点缀 + kami 长报告叙事分区
  - 所有图表用纯 QWidget + QPainter 手绘，无需 ECharts 依赖
  - 顶层 QTool 标志：主窗最小化时，报告窗口独立存在，不会一起被最小化

数据契约：见 app/core/report_aggregator.py（兼容 yao-weread-skill weread-report-data.json）
"""
from __future__ import annotations

import math
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt, QSize
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPen,
    QPainterPath,
    QPixmap,
)
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.report_aggregator import ReportAggregator
from app.utils.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# 颜色 & 排版常量
# ---------------------------------------------------------------------------
C_PAPER = "#F9F9F5"
C_CARD = "#FFFFFF"
C_BORDER = "#E5E5DF"
C_INK = "#1F2937"
C_INK_SOFT = "#374151"
C_MUTED = "#6B7280"
C_OK = "#2D9D3C"
C_GOLD = "#D99B2A"
C_DANGER = "#D14343"
C_BUFF = "#FBF3DE"
C_BUFF_BD = "#E6D7B0"
C_INK_SOLID = "#1F2937"

# 图表调色板（12 色循环）
CHART_PALETTE = [
    "#3B82F6",  # 蓝
    "#2D9D3C",  # 绿
    "#D99B2A",  # 金
    "#D14343",  # 朱
    "#8B5CF6",  # 紫
    "#EC4899",  # 粉
    "#14B8A6",  # 青
    "#F97316",  # 橙
    "#0EA5E9",  # 天蓝
    "#65A30D",  # 草绿
    "#A855F7",  # 罗兰
    "#DC2626",  # 赤
]


# =========================================================================
# 工具函数
# =========================================================================
def _fmt_sec(s: Any) -> str:
    try:
        sec = int(s or 0)
    except (TypeError, ValueError):
        sec = 0
    h, r = divmod(max(0, sec), 3600)
    m = r // 60
    if h == 0:
        return f"{m}分钟"
    return f"{h}h{m:02d}"


def _deco_title(text: str, note: str = "") -> QLabel:
    """分节标题：左❦装饰线 + 大字 + 小字说明。"""
    wrap = QWidget()
    lay = QHBoxLayout(wrap)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(10)

    fleur = QLabel("❦")
    fleur.setStyleSheet(
        f"font-size:22px;color:{C_GOLD};font-family:Georgia,serif;padding-top:2px;"
    )
    lay.addWidget(fleur)

    mid = QVBoxLayout()
    mid.setSpacing(2)
    title = QLabel(text)
    title.setStyleSheet(
        f"font-size:17px;font-weight:700;color:{C_INK};"
        "font-family:'Microsoft YaHei UI','PingFang SC',sans-serif;letter-spacing:0.5px;"
    )
    mid.addWidget(title)
    if note:
        sub = QLabel(note)
        sub.setStyleSheet(f"color:{C_MUTED};font-size:12px;")
        mid.addWidget(sub)
    lay.addLayout(mid, 1)

    dash = QFrame()
    dash.setFrameShape(QFrame.Shape.HLine)
    dash.setStyleSheet(f"color:{C_BORDER};background:{C_BORDER};height:1px;max-height:1px;")
    lay.addWidget(dash, 1)
    wrap.setMinimumHeight(40)
    return wrap


def _card() -> QFrame:
    c = QFrame()
    c.setStyleSheet(
        f"background:{C_CARD};border:1px solid {C_BORDER};border-radius:14px;"
    )
    return c


def _section() -> QFrame:
    s = QFrame()
    s.setStyleSheet(
        f"background:{C_PAPER};"
        f"border-top:1px dashed {C_BORDER};"
        "padding-top:14px;padding-bottom:14px;"
    )
    return s


# =========================================================================
# 图表 Widget 基类（paintEvent 渲染）
# =========================================================================
class ChartWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)


# =========================================================================
# 1. 月度阅读热力图（竖列 24 个月 × 行是天数？简化成 24 根方块条，深浅按天数）
# =========================================================================
class MonthlyHeatmapChart(ChartWidget):
    def __init__(self, monthly: list[dict], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows = monthly[-24:]
        self.setMinimumHeight(140)

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()
        pad_l, pad_r, pad_t, pad_b = 46, 12, 18, 28
        cw = (w - pad_l - pad_r)
        ch = h - pad_t - pad_b

        max_sec = max((r.get("seconds", 0) for r in self._rows), default=1)

        # 绘制 Y 轴提示："最多月 / 最少月"
        p.setPen(QColor(C_MUTED))
        p.setFont(QFont("JetBrains Mono", 9))
        p.drawText(QRectF(0, pad_t, pad_l - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, _fmt_sec(max_sec))
        p.drawText(QRectF(0, pad_t + ch - 16, pad_l - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "0")

        # 柱体
        n = max(1, len(self._rows))
        bar_w = cw / n * 0.72
        gap = cw / n * 0.28
        for i, r in enumerate(self._rows):
            x = pad_l + i * (bar_w + gap)
            sec = r.get("seconds", 0)
            ratio = min(1.0, (sec / max_sec) if max_sec > 0 else 0)
            bh = max(3, ch * ratio)
            y = pad_t + ch - bh

            # 上色：按 ratio 渐变青苔绿→墨黑
            t = int(235 - int(ratio * 130))
            if ratio > 0.66:
                fill = QColor(C_OK)
            elif ratio > 0.33:
                fill = QColor(C_GOLD)
            else:
                fill = QColor(t, t - 8, t - 20)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(fill))
            p.drawRoundedRect(QRectF(x, y, bar_w, bh), 4, 4)

            # 月份标签（每隔 3 个月显示一次）
            if i % 3 == 0 or i == n - 1:
                ym = r.get("ym", "")
                m_label = ym[5:7] + "月" if len(ym) >= 7 else ym
                p.setPen(QColor(C_MUTED))
                p.setFont(QFont("JetBrains Mono", 8))
                p.drawText(
                    QRectF(x - 6, h - pad_b + 6, bar_w + 12, 16),
                    Qt.AlignmentFlag.AlignHCenter,
                    m_label,
                )

        p.end()


# =========================================================================
# 2. 星期节律水平条形
# =========================================================================
class WeeklyBarsChart(ChartWidget):
    def __init__(self, rhythm: list[dict], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows = rhythm or []
        self.setMinimumHeight(230)

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()
        pad_l, pad_r, pad_t, pad_b = 72, 30, 12, 14
        row_h = 22
        row_gap = 8
        bar_max_w = w - pad_l - pad_r

        max_sec = max((r.get("seconds", 0) for r in self._rows), default=1)

        for i, r in enumerate(self._rows):
            y = pad_t + i * (row_h + row_gap)
            # 左边周几文字
            p.setPen(QColor(C_INK_SOFT))
            p.setFont(QFont("Microsoft YaHei UI", 11))
            p.drawText(
                QRectF(0, y, pad_l - 8, row_h),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                r.get("weekday_zh", ""),
            )
            # 背景灰色条
            p.setPen(QPen(QColor(C_BORDER), 0))
            p.setBrush(QBrush(QColor(C_PAPER)))
            p.drawRoundedRect(QRectF(pad_l, y, bar_max_w, row_h), 6, 6)
            # 实际值
            sec = r.get("seconds", 0)
            ratio = min(1.0, sec / max_sec) if max_sec > 0 else 0
            bw = max(6, bar_max_w * ratio)
            color = QColor(CHART_PALETTE[i % len(CHART_PALETTE)])
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(color))
            p.drawRoundedRect(QRectF(pad_l, y, bw, row_h), 6, 6)
            # 右边文字：时长 + 天数
            p.setPen(QColor(C_INK))
            p.setFont(QFont("JetBrains Mono", 9))
            text = f"{_fmt_sec(sec)}  ·  {r.get('day_count', 0)}d"
            p.drawText(
                QRectF(pad_l + bw + 8, y, pad_r, row_h),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                text,
            )
        p.end()


# =========================================================================
# 3. 小时节律曲线图（区域面积）
# =========================================================================
class HourlyRhythmChart(ChartWidget):
    def __init__(self, rhythm: list[dict], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows = sorted(rhythm or [], key=lambda r: r.get("hour", 0))
        self.setMinimumHeight(180)

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()
        pad_l, pad_r, pad_t, pad_b = 32, 12, 14, 28
        cw = w - pad_l - pad_r
        ch = h - pad_t - pad_b

        vals = [r.get("seconds", 0) for r in self._rows] or [0]
        max_v = max(vals) or 1
        n = len(vals)
        step_x = cw / max(1, n - 1)

        # 面积填充渐变
        g = QLinearGradient(0, pad_t, 0, pad_t + ch)
        g.setColorAt(0.0, QColor(209, 67, 67, 60))
        g.setColorAt(1.0, QColor(209, 67, 67, 0))

        path_area = QPainterPath()
        path_line = QPainterPath()
        first_x = pad_l
        first_y = pad_t + ch
        path_area.moveTo(first_x, first_y)
        for i, v in enumerate(vals):
            x = pad_l + i * step_x
            y = pad_t + ch - (v / max_v) * ch
            if i == 0:
                path_line.moveTo(x, y)
            else:
                path_line.lineTo(x, y)
            path_area.lineTo(x, y)
        path_area.lineTo(pad_l + cw, first_y)
        path_area.closeSubpath()

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawPath(path_area)

        # 描线
        pen = QPen(QColor(C_DANGER), 2)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path_line)

        # X 轴：每 3 小时
        p.setPen(QColor(C_MUTED))
        p.setFont(QFont("JetBrains Mono", 8))
        for i, r in enumerate(self._rows):
            hour = r.get("hour", 0)
            if hour % 3 != 0:
                continue
            x = pad_l + i * step_x
            p.drawText(
                QRectF(x - 16, h - pad_b + 4, 32, 14),
                Qt.AlignmentFlag.AlignHCenter,
                f"{hour:02d}h",
            )
        p.end()


# =========================================================================
# 4. Top 条形图（作者 / 出版社 / 读得最久的书 / NoteStats）
# =========================================================================
class HorizontalBarList(ChartWidget):
    def __init__(
        self,
        items: list[dict],
        *,
        label_key: str,
        value_key: str = "seconds",
        value_fmt=_fmt_sec,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._items = items[:10]
        self._lkey = label_key
        self._vkey = value_key
        self._fmt = value_fmt
        self.setMinimumHeight(60 + max(4, len(self._items)) * 28)

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        pad_l, pad_r, pad_t, pad_b = 130, 90, 10, 10
        bar_h = 20
        gap = 8
        cw = max(1, w - pad_l - pad_r)
        vals = [it.get(self._vkey, 0) for it in self._items]
        max_v = max(vals) if vals else 1
        if max_v <= 0:
            max_v = 1

        for i, it in enumerate(self._items):
            y = pad_t + i * (bar_h + gap)
            # 左侧标题
            label = str(it.get(self._lkey, ""))
            if len(label) > 16:
                label = label[:15] + "…"
            p.setPen(QColor(C_INK_SOFT))
            p.setFont(QFont("Microsoft YaHei UI", 10))
            p.drawText(
                QRectF(0, y, pad_l - 8, bar_h),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                label,
            )
            # 背景条
            p.setPen(QPen(QColor(C_BORDER), 0))
            p.setBrush(QBrush(QColor(C_PAPER)))
            p.drawRoundedRect(QRectF(pad_l, y, cw, bar_h), 5, 5)
            # 值条
            v = vals[i]
            ratio = min(1.0, v / max_v)
            bw = max(6, int(cw * ratio))
            color = QColor(CHART_PALETTE[i % len(CHART_PALETTE)])
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(color))
            p.drawRoundedRect(QRectF(pad_l, y, bw, bar_h), 5, 5)
            # 右侧数值
            p.setPen(QColor(C_INK))
            p.setFont(QFont("JetBrains Mono", 9))
            p.drawText(
                QRectF(pad_l + cw + 8, y, pad_r - 8, bar_h),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                self._fmt(v),
            )
        p.end()


# =========================================================================
# 5. 分类雷达图（5 轴）
# =========================================================================
class RadarChart(ChartWidget):
    def __init__(self, categories: list[dict], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cats = categories[:5]
        self.setMinimumHeight(320)
        self.setMinimumWidth(320)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()
        cx, cy = w / 2, h / 2 + 6
        r = min(w, h) / 2 - 40
        n = max(5, len(self._cats))  # 保持 5 轴
        cats = self._cats[:5] or [{"name": "商业", "seconds": 1}] * 5
        max_v = max((c.get("seconds", 0) for c in cats), default=1)
        if max_v <= 0:
            max_v = 1

        # 网格（5 环）
        pen_grid = QPen(QColor(C_BORDER))
        pen_grid.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen_grid)
        for k in range(1, 6):
            rr = r * (k / 5)
            path = QPainterPath()
            for i in range(n):
                angle = -math.pi / 2 + i * 2 * math.pi / n
                x = cx + rr * math.cos(angle)
                y = cy + rr * math.sin(angle)
                if i == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
            path.closeSubpath()
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)

        # 轴
        pen_axis = QPen(QColor(C_BORDER))
        p.setPen(pen_axis)
        for i in range(n):
            angle = -math.pi / 2 + i * 2 * math.pi / n
            x2 = cx + r * math.cos(angle)
            y2 = cy + r * math.sin(angle)
            p.drawLine(QPointF(cx, cy), QPointF(x2, y2))

        # 填充多边形
        poly = QPainterPath()
        for i, c in enumerate(cats):
            v = c.get("seconds", 0) / max_v
            angle = -math.pi / 2 + i * 2 * math.pi / n
            rr = r * min(1.0, v)
            x = cx + rr * math.cos(angle)
            y = cy + rr * math.sin(angle)
            if i == 0:
                poly.moveTo(x, y)
            else:
                poly.lineTo(x, y)
        poly.closeSubpath()
        p.setPen(QPen(QColor(C_OK), 2))
        p.setBrush(QBrush(QColor(45, 157, 60, 80)))
        p.drawPath(poly)

        # 顶点圆点
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(C_OK)))
        for i, c in enumerate(cats):
            v = c.get("seconds", 0) / max_v
            angle = -math.pi / 2 + i * 2 * math.pi / n
            rr = r * min(1.0, v)
            x = cx + rr * math.cos(angle)
            y = cy + rr * math.sin(angle)
            p.drawEllipse(QPointF(x, y), 3.5, 3.5)

        # 分类标签
        p.setPen(QColor(C_INK_SOFT))
        p.setFont(QFont("Microsoft YaHei UI", 11, QFont.Weight.Bold))
        for i, c in enumerate(cats):
            angle = -math.pi / 2 + i * 2 * math.pi / n
            lx = cx + (r + 22) * math.cos(angle)
            ly = cy + (r + 22) * math.sin(angle)
            rect = QRectF(lx - 60, ly - 12, 120, 24)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, c.get("name", ""))
        p.end()


# =========================================================================
# 6. 书架饼环（环形）
# =========================================================================
class DonutChart(ChartWidget):
    """书架构成·环形图：
    - 主环 = 进度维度（已完成/在读/未读），3 项 合计 = 藏书总数
    - 中心文字 = 单一真值 total_books（Aggregator 传 _shelf_total，
      杜绝因双维度混入而出现「合计 = 藏书 × 2」的 Bug）
    - 右侧图例 = 两组并列：进度 3 行 · 可见性 2 行，一目了然
    """

    # 仅这 3 类会进入环形分段；可见性只在图例+底部条呈现，绝不分走弧度
    _STATUS_LABELS = {"已完成", "在读", "未读"}
    _VIS_LABELS = {"私密书", "公开书"}

    def __init__(
        self,
        pie: list[dict],
        visibility: list[dict] | None = None,
        total_books: Any = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # 防御：pie 里若混进私密书/公开书，必须剔除（双维度正交，绝不一起画环）
        clean: list[dict] = []
        for r in (pie or []):
            if isinstance(r, dict) and str(r.get("label", "")) in self._STATUS_LABELS:
                clean.append(r)
        self._rows = clean
        self._vis = [r for r in (visibility or []) if isinstance(r, dict)
                     and str(r.get("label", "")) in self._VIS_LABELS]
        # 单一真值藏书总数：优先调用方传；否则退回 clean 求和；最后兜底 1（避免除0）
        if isinstance(total_books, int) and total_books >= 0:
            self._total = total_books
        else:
            s = sum(int(r.get("count", 0) or 0) for r in self._rows)
            self._total = s if s > 0 else 1
        self.setMinimumHeight(340)
        self.setMinimumWidth(320)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()

        # 图例区右 + 两分组头预留：进度 3 行 + 分隔 1 行 + 可见性 2 行 + 两组头 2 行 = 8 行
        legend_w = 180
        chart_h = h
        side = min(w - legend_w, chart_h) - 24
        side = max(140, side)
        cx = side / 2 + 12
        cy = chart_h / 2
        R_o = side / 2
        R_i = R_o * 0.60

        # 颜色映射（按标签语义，保证跨刷新/跨机器稳定）
        palette_map = {
            "已完成": C_OK,
            "在读": C_GOLD,
            "未读": C_MUTED,
            "私密书": C_DANGER,
            "公开书": "#0EA5E9",
        }

        # ============== 环形 = 只用 进度 3 项 画弧 ==============
        ring_total = sum(int(r.get("count", 0) or 0) for r in self._rows)
        ring_total = ring_total or self._total or 1
        start_a = -90 * 16  # QPainter 角度用 1/16 度制
        for i, r in enumerate(self._rows):
            cnt = int(r.get("count", 0) or 0)
            if cnt <= 0:
                continue
            sweep = int(360 * 16 * cnt / ring_total)
            label = r.get("label", "")
            color = QColor(palette_map.get(label, CHART_PALETTE[i % len(CHART_PALETTE)]))
            path = QPainterPath()
            rect_o = QRectF(cx - R_o, cy - R_o, 2 * R_o, 2 * R_o)
            path.moveTo(cx, cy)
            path.arcTo(rect_o, start_a / 16, sweep / 16)
            path.closeSubpath()
            rect_i = QRectF(cx - R_i, cy - R_i, 2 * R_i, 2 * R_i)
            inner = QPainterPath()
            inner.moveTo(cx, cy)
            inner.arcTo(rect_i, start_a / 16, sweep / 16)
            inner.closeSubpath()
            ring = path.subtracted(inner)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(color))
            p.drawPath(ring)
            start_a += sweep

        # ============== 中心文字 = 单一真值藏书数 ==============
        p.setPen(QColor(C_INK))
        f_title = QFont("JetBrains Mono", 10)
        p.setFont(f_title)
        p.drawText(QRectF(cx - R_i, cy - R_i, 2 * R_i, 2 * R_i * 0.4),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
                   "合计藏书")
        f_val = QFont("JetBrains Mono", 20, QFont.Weight.Bold)
        p.setFont(f_val)
        p.drawText(QRectF(cx - R_i, cy - 6, 2 * R_i, 30),
                   Qt.AlignmentFlag.AlignCenter, f"{self._total}")

        # ============== 右侧图例：两组（进度 / 可见性）并列 ==============
        lx = side + 24
        ly = cy - 3.2 * 24  # 居中：约 6.4 行（进度组头 1 + 3 条目 + 分隔 0.6 + 可见组头 1 + 2 条目）
        row_h = 24

        def _group_header(text: str, y: int) -> int:
            p.setFont(QFont("Microsoft YaHei UI", 9, QFont.Weight.DemiBold))
            p.setPen(QColor(C_MUTED))
            p.drawText(QRectF(lx, y, legend_w - 12, row_h),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
            return y + row_h

        def _swatch_row(r: dict, y: int) -> int:
            nonlocal palette_map
            label = str(r.get("label", ""))
            cnt = int(r.get("count", 0) or 0)
            color = QColor(palette_map.get(label, C_MUTED))
            # 色块
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(color))
            p.drawRoundedRect(QRectF(lx, y + 5, 12, 12), 3, 3)
            # label 左
            p.setFont(QFont("Microsoft YaHei UI", 10))
            p.setPen(QColor(C_INK_SOFT))
            p.drawText(QRectF(lx + 20, y, 70, row_h),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
            # count 右（等宽字体，对齐）
            p.setFont(QFont("JetBrains Mono", 9))
            p.setPen(QColor(C_INK))
            p.drawText(QRectF(lx + 92, y, legend_w - 104, row_h),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       f"{cnt}")
            return y + row_h

        # 进度组
        y = _group_header("进度（构成藏书总数）", ly)
        for r in self._rows:
            y = _swatch_row(r, y)

        # 可见性组（进度组的正交维度，同样合计 = 藏书总数）
        y += 4
        y = _group_header("可见性（与总数一致）", y)
        for r in self._vis:
            y = _swatch_row(r, y)
        p.end()


# =========================================================================
# 7. 进度分布直方图
# =========================================================================
class ProgressHistChart(ChartWidget):
    def __init__(self, hist: list[dict], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows = hist or []
        self.setMinimumHeight(200)

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()
        pad_l, pad_r, pad_t, pad_b = 36, 12, 14, 30
        cw = w - pad_l - pad_r
        ch = h - pad_t - pad_b
        n = max(1, len(self._rows))
        bar_w = cw / n * 0.70
        gap = cw / n * 0.30
        max_v = max((r.get("count", 0) for r in self._rows), default=1)
        for i, r in enumerate(self._rows):
            v = r.get("count", 0)
            bh = max(4, int(ch * v / max(max_v, 1)))
            x = pad_l + i * (bar_w + gap)
            y = pad_t + ch - bh
            # 最后一个桶（100）用绿
            if r.get("bucket") == "100":
                fill = QColor(C_OK)
            elif i >= len(self._rows) - 2:
                fill = QColor(C_GOLD)
            else:
                fill = QColor(C_INK_SOFT)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(fill))
            p.drawRoundedRect(QRectF(x, y, bar_w, bh), 6, 6)

            p.setPen(QColor(C_INK))
            p.setFont(QFont("JetBrains Mono", 9))
            p.drawText(QRectF(x, y - 18, bar_w, 14),
                       Qt.AlignmentFlag.AlignHCenter, f"{v}")
            p.setPen(QColor(C_MUTED))
            p.drawText(QRectF(x - 6, h - pad_b + 4, bar_w + 12, 16),
                       Qt.AlignmentFlag.AlignHCenter, r.get("bucket", "") + "%")
        p.end()


# =========================================================================
# 8. 进度-章节数散点（气泡）
# =========================================================================
class ProgressScatterChart(ChartWidget):
    def __init__(self, scatter: list[dict], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows = scatter or []
        self.setMinimumHeight(240)

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()
        pad_l, pad_r, pad_t, pad_b = 34, 12, 14, 26
        cw = w - pad_l - pad_r
        ch = h - pad_t - pad_b

        max_ch = max((r.get("chapter_count", 0) for r in self._rows), default=1)
        if max_ch <= 0:
            max_ch = 100
        # 轴
        p.setPen(QPen(QColor(C_BORDER)))
        p.drawLine(QPointF(pad_l, pad_t), QPointF(pad_l, pad_t + ch))
        p.drawLine(QPointF(pad_l, pad_t + ch), QPointF(pad_l + cw, pad_t + ch))
        # 点
        for i, r in enumerate(self._rows):
            pr = min(100, max(0, int(r.get("progress", 0) or 0)))
            cc = max(1, min(max_ch, int(r.get("chapter_count", 0) or 1)))
            x = pad_l + cw * pr / 100
            y = pad_t + ch - ch * cc / max_ch
            color = QColor(CHART_PALETTE[i % len(CHART_PALETTE)])
            color.setAlpha(170)
            radius = max(3, min(8, int(cc / max_ch * 12) + 3))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(color))
            p.drawEllipse(QPointF(x, y), radius, radius)
        # 轴标签
        p.setPen(QColor(C_MUTED))
        p.setFont(QFont("JetBrains Mono", 8))
        p.drawText(QRectF(pad_l - 34, pad_t + ch + 4, 30, 16),
                   Qt.AlignmentFlag.AlignRight, "0%")
        p.drawText(QRectF(pad_l + cw - 20, pad_t + ch + 4, 30, 16),
                   Qt.AlignmentFlag.AlignLeft, "100%")
        p.drawText(QRectF(0, pad_t - 6, pad_l - 6, 14),
                   Qt.AlignmentFlag.AlignRight, f"{max_ch}ch")
        p.drawText(QRectF(0, pad_t + ch - 8, pad_l - 6, 14),
                   Qt.AlignmentFlag.AlignRight, "1ch")
        p.end()


# =========================================================================
# 9. 笔记时间线（近 90 天横向小柱）
# =========================================================================
class NotesTimelineChart(ChartWidget):
    def __init__(self, timeline: list[dict], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows = timeline or []
        self.setMinimumHeight(140)

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()
        pad_l, pad_r, pad_t, pad_b = 36, 12, 14, 28
        cw = w - pad_l - pad_r
        ch = h - pad_t - pad_b
        n = max(1, len(self._rows))
        max_v = max((r.get("count", 0) for r in self._rows), default=1)
        bar_w = cw / n
        for i, r in enumerate(self._rows):
            v = r.get("count", 0)
            bh = max(2, int(ch * v / max(max_v, 1)))
            x = pad_l + i * bar_w
            y = pad_t + ch - bh
            if v >= 3:
                fill = QColor(C_DANGER)
            elif v >= 1:
                fill = QColor(C_GOLD)
            else:
                fill = QColor(C_BORDER)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(fill))
            # 小柱 1 像素间隙
            bw = max(1, bar_w - 1)
            p.drawRect(QRectF(x, y, bw, bh))

        # 轴标签：每月 1 号
        p.setPen(QColor(C_MUTED))
        p.setFont(QFont("JetBrains Mono", 8))
        last_month = ""
        for i, r in enumerate(self._rows):
            d = r.get("date", "")
            m_label = d[5:7] if len(d) >= 7 else ""
            if "-01" in d and m_label != last_month:
                last_month = m_label
                x = pad_l + i * bar_w
                p.drawText(QRectF(x - 20, h - pad_b + 6, 40, 14),
                           Qt.AlignmentFlag.AlignHCenter, m_label + "月")
        p.end()


# =========================================================================
# 10. 词云（按权字缩放 + 颜色，流式排布）
# =========================================================================
class WordCloudChart(ChartWidget):
    def __init__(self, words: list[dict], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._words = words or []
        self.setMinimumHeight(240)

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        w = self.width()
        h = self.height()
        weights = [r.get("weight", 1) for r in self._words] or [1]
        max_w = max(weights) or 1
        min_w = min(weights)
        placed: list[tuple[QRectF, str]] = []
        # 先从权重大到小排
        items = sorted(self._words, key=lambda r: -r.get("weight", 1))
        pad = 8
        # 螺旋布局
        cx, cy = w / 2, h / 2
        t = 0.0
        for i, item in enumerate(items):
            weight = item.get("weight", 1)
            ratio = 0.2 + 0.8 * ((weight - min_w) / max((max_w - min_w), 1))
            size = int(12 + ratio * 30)
            f = QFont("Microsoft YaHei UI", size, QFont.Weight.Bold if ratio > 0.55 else QFont.Weight.Normal)
            p.setFont(f)
            word = str(item.get("word", ""))
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(word) + 4
            th = fm.height() + 4
            color = QColor(CHART_PALETTE[i % len(CHART_PALETTE)])
            if ratio < 0.3:
                color = QColor(C_MUTED)
            color.setAlpha(int(180 + ratio * 55))

            # 螺旋搜索空位（简单实现，超出 400 次就跳过）
            placed_ok = False
            for step in range(400):
                r = step * 0.9
                theta = t + step * 0.28
                x = cx + r * math.cos(theta) - tw / 2
                y = cy + r * math.sin(theta) - th / 2
                rect = QRectF(x, y, tw, th)
                if rect.x() < pad or rect.right() > w - pad \
                   or rect.y() < pad or rect.bottom() > h - pad:
                    continue
                # 重叠检测
                overlap = False
                for r2, _ in placed:
                    if rect.intersects(r2):
                        overlap = True
                        break
                if overlap:
                    continue
                placed.append((rect, word))
                p.setPen(QPen(color))
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, word)
                placed_ok = True
                break
            if not placed_ok:
                pass
        p.end()


# =========================================================================
# 主对话框
# =========================================================================
class ReportDialog(QDialog):
    """阅读报告对话框。

    窗口 Flag 使用 `Tool`：Windows 下不会因父窗口最小化而一起最小化。
    """

    def __init__(self, aggregator: ReportAggregator, parent: QWidget | None = None) -> None:
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint,
        )
        self._agg = aggregator
        self.setWindowTitle("📘 个人阅读报告 · WxReadAssistant")
        self.resize(1160, 820)
        self.setMinimumSize(QSize(980, 640))

        # 背景
        self.setStyleSheet(f"QDialog{{background:{C_PAPER};}}")

        # 数据
        self._data = aggregator.build()

        self._build_ui()

    # ------------------------------------------------------------------
    # UI 组装
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            f"QScrollArea{{background:{C_PAPER};border:none;}}"
            "QScrollBar:vertical{width:8px;background:transparent;}"
            "QScrollBar::handle:vertical{background:#C8CDD5;border-radius:4px;margin:2px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        )
        host = QWidget()
        host.setStyleSheet(f"background:{C_PAPER};")
        v = QVBoxLayout(host)
        v.setContentsMargins(30, 26, 30, 30)
        v.setSpacing(14)

        # 1. Banner
        v.addWidget(self._build_header())
        # 2. 读者画像
        v.addWidget(self._build_portrait_banner())
        # 3. 4 KPI 整板
        v.addWidget(self._build_kpi_board())
        # 分节装饰横
        v.addSpacing(6)

        # 分区 1. 时间节律
        s1 = _section()
        sl1 = QVBoxLayout(s1)
        sl1.setContentsMargins(8, 4, 8, 4)
        sl1.setSpacing(10)
        sl1.addWidget(_deco_title(
            "一、时间节律",
            "月度时长 · 星期偏好 · 时段分布（高峰通勤/午间/睡前）",
        ))
        s1_card = _card()
        g1 = QGridLayout(s1_card)
        g1.setContentsMargins(14, 12, 14, 14)
        g1.setHorizontalSpacing(14)
        g1.setVerticalSpacing(8)
        g1.setColumnStretch(0, 3)
        g1.setColumnStretch(1, 2)
        g1.setColumnStretch(2, 2)
        g1.addWidget(QLabel("📊 近 24 个月阅读时长"), 0, 0)
        g1.addWidget(QLabel("🗓 星期节律"), 0, 1)
        g1.addWidget(QLabel("⏱ 24 小时节律"), 0, 2)
        g1.addWidget(MonthlyHeatmapChart(self._data.get("monthly", [])), 1, 0)
        g1.addWidget(WeeklyBarsChart(self._data.get("weekly_rhythm", [])), 1, 1)
        g1.addWidget(HourlyRhythmChart(self._data.get("daily_rhythm", [])), 1, 2)
        sl1.addWidget(s1_card)
        v.addWidget(s1)

        # 分区 2. 阅读偏好
        s2 = _section()
        sl2 = QVBoxLayout(s2)
        sl2.setContentsMargins(8, 4, 8, 4)
        sl2.setSpacing(10)
        sl2.addWidget(_deco_title(
            "二、阅读偏好",
            "分类雷达 · Top 作者 / Top 出版社 · 读得最久的书",
        ))
        s2_card = _card()
        g2 = QGridLayout(s2_card)
        g2.setContentsMargins(14, 12, 14, 14)
        g2.setHorizontalSpacing(14)
        g2.setVerticalSpacing(8)
        g2.setColumnStretch(0, 1)
        g2.setColumnStretch(1, 1)
        g2.addWidget(QLabel("🕸 分类分布（Top 5）"), 0, 0)
        g2.addWidget(QLabel("✍ Top 10 作者"), 0, 1)
        g2.addWidget(RadarChart(self._data.get("categories", [])), 1, 0)
        g2.addWidget(
            HorizontalBarList(
                self._data.get("top_authors", []), label_key="name",
                value_key="books_count", value_fmt=lambda v: f"{v}本",
            ),
            1, 1,
        )
        g2.addWidget(QLabel("🏛 Top 10 出版社"), 2, 0)
        g2.addWidget(QLabel("🏆 读得最久的 10 本书"), 2, 1)
        g2.addWidget(
            HorizontalBarList(
                self._data.get("top_publishers", []), label_key="name",
                value_key="books_count", value_fmt=lambda v: f"{v}本",
            ),
            3, 0,
        )
        g2.addWidget(
            HorizontalBarList(
                self._data.get("read_longest", []), label_key="title",
                value_key="seconds", value_fmt=_fmt_sec,
            ),
            3, 1,
        )
        sl2.addWidget(s2_card)
        v.addWidget(s2)

        # 分区 3. 书架资产
        s3 = _section()
        sl3 = QVBoxLayout(s3)
        sl3.setContentsMargins(8, 4, 8, 4)
        sl3.setSpacing(10)
        sl3.addWidget(_deco_title(
            "三、书架资产",
            "进度分布 · 书架构成 · 进度-章节散点",
        ))
        s3_card = _card()
        g3 = QGridLayout(s3_card)
        g3.setContentsMargins(14, 12, 14, 14)
        g3.setHorizontalSpacing(14)
        g3.setVerticalSpacing(8)
        g3.setColumnStretch(0, 1)
        g3.setColumnStretch(1, 1)
        g3.setColumnStretch(2, 1)
        g3.addWidget(QLabel("📈 进度分布（6 档）"), 0, 0)
        g3.addWidget(QLabel("🍩 书架构成"), 0, 1)
        g3.addWidget(QLabel("🫧 进度 × 章节数 散点"), 0, 2)
        g3.addWidget(ProgressHistChart(self._data.get("progress_hist", [])), 1, 0)
        g3.addWidget(DonutChart(
            self._data.get("shelf_pie", []),
            visibility=self._data.get("shelf_visibility"),
            total_books=self._data.get("_shelf_total"),
        ), 1, 1)
        g3.addWidget(ProgressScatterChart(self._data.get("progress_scatter", [])), 1, 2)
        sl3.addWidget(s3_card)
        v.addWidget(s3)

        # 分区 4. 笔记与语义
        s4 = _section()
        sl4 = QVBoxLayout(s4)
        sl4.setContentsMargins(8, 4, 8, 4)
        sl4.setSpacing(10)
        sl4.addWidget(_deco_title(
            "四、笔记与语义",
            "笔记时间线 · 笔记 Top 10 书 · 词云",
        ))
        s4_card = _card()
        g4 = QGridLayout(s4_card)
        g4.setContentsMargins(14, 12, 14, 14)
        g4.setHorizontalSpacing(14)
        g4.setVerticalSpacing(8)
        g4.setColumnStretch(0, 3)
        g4.setColumnStretch(1, 2)
        g4.setColumnStretch(2, 2)
        g4.addWidget(QLabel("📜 近 90 天笔记数量"), 0, 0)
        g4.addWidget(QLabel("📝 Top 10 笔记书"), 0, 1)
        g4.addWidget(QLabel("☁ 词云 · 偏好关键词"), 0, 2)
        g4.addWidget(NotesTimelineChart(self._data.get("notes_timeline", [])), 1, 0)
        g4.addWidget(
            HorizontalBarList(
                self._data.get("note_stats", []), label_key="book_title",
                value_key="count", value_fmt=lambda v: f"{v}条",
            ),
            1, 1,
        )
        g4.addWidget(WordCloudChart(self._data.get("word_cloud", [])), 1, 2)
        sl4.addWidget(s4_card)
        v.addWidget(s4)

        # 分区 5. 读者画像 · 高价值划线清单
        s5 = _section()
        sl5 = QVBoxLayout(s5)
        sl5.setContentsMargins(8, 4, 8, 4)
        sl5.setSpacing(10)
        sl5.addWidget(_deco_title(
            "五、读者画像 · 20 条高价值划线",
            "以下句子都来自你自己的划线 — 他们是真正意义上的「你的书单」。",
        ))
        s5_card = _card()
        lay = QVBoxLayout(s5_card)
        lay.setContentsMargins(16, 14, 16, 16)
        lay.setSpacing(10)
        portrait = self._data.get("reader_portrait", {}) or {}
        highlights = portrait.get("highlights", []) or []
        for i, hl in enumerate(highlights[:20], start=1):
            lay.addWidget(self._build_highlights_row(i, hl))
        if not highlights:
            l = QLabel("暂无划线数据（可先完成几次阅读后再打开报告）")
            l.setStyleSheet(f"color:{C_MUTED};padding:20px;")
            lay.addWidget(l)
        sl5.addWidget(s5_card)
        v.addWidget(s5)

        # 页脚
        v.addSpacing(12)
        foot = QHBoxLayout()
        left = QLabel(f"❦   报告生成时间：{self._data.get('_generated_at', '')}")
        left.setStyleSheet(f"color:{C_MUTED};font-size:12px;")
        foot.addWidget(left)
        badge = QLabel(" Sample " if self._data.get("_is_sample") else " Powered by WxReadAssistant ")
        badge.setStyleSheet(
            f"font-family:'JetBrains Mono',Consolas,monospace;font-size:10px;"
            f"color:{C_GOLD if self._data.get('_is_sample') else C_INK_SOFT};"
            f"background:{'#FFF7E6' if self._data.get('_is_sample') else C_CARD};"
            f"border:1px solid {C_BUFF_BD if self._data.get('_is_sample') else C_BORDER};"
            "border-radius:999px;padding:2px 10px;letter-spacing:0.5px;"
        )
        foot.addStretch(1)
        foot.addWidget(badge)
        v.addLayout(foot)

        scroll.setWidget(host)
        root.addWidget(scroll)

    # --- 头部 Banner ---------------------------------------------------
    def _build_header(self) -> QWidget:
        wrap = QFrame()
        wrap.setStyleSheet(
            f"background:{C_CARD};border:1px solid {C_BORDER};border-radius:16px;"
        )
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(18)

        # 左：书脊色块装饰
        deco = QFrame()
        deco.setMinimumWidth(6)
        deco.setMaximumWidth(6)
        deco.setStyleSheet(
            "background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            f"stop:0 #30ADFF, stop:0.5 #D99B2A, stop:1 #D14343);"
            "border-radius:3px;"
        )
        lay.addWidget(deco)

        # 中：标题区
        mid = QVBoxLayout()
        mid.setSpacing(4)
        fleur = QLabel("❦  M Y   R E A D I N G   R E P O R T")
        fleur.setStyleSheet(
            f"color:{C_GOLD};font-family:Georgia,serif;font-size:12px;"
            "letter-spacing:2px;"
        )
        mid.addWidget(fleur)
        title = QLabel("📘  我的阅读报告")
        title.setStyleSheet(
            "font-family:'Microsoft YaHei UI','PingFang SC',sans-serif;"
            f"font-size:26px;font-weight:800;color:{C_INK};letter-spacing:1px;"
        )
        mid.addWidget(title)
        sub = QLabel(f"基于微信读书 Skill 聚合数据 · 报告周期 24 个月  ·  生成于 {self._data.get('_generated_at', '')}")
        sub.setStyleSheet(f"color:{C_MUTED};font-size:12px;")
        mid.addWidget(sub)
        mid.addStretch(1)
        lay.addLayout(mid, 1)

        # 右：徽章（采样/正式 + 生成按钮）
        right = QVBoxLayout()
        right.setSpacing(8)
        right.addStretch(1)

        sample_badge = QLabel()
        if self._data.get("_is_sample"):
            sample_badge.setText(" SAMPLE · 本地抽样 ")
            sample_badge.setStyleSheet(
                f"background:#FFF7E6;color:{C_GOLD};border:1px solid {C_BUFF_BD};"
                "border-radius:999px;padding:3px 10px;font-size:11px;"
                "font-family:'JetBrains Mono',Consolas,monospace;"
            )
        else:
            sample_badge.setText(" ✓ REAL DATA ")
            sample_badge.setStyleSheet(
                f"background:#EAF6EC;color:{C_OK};border:1px solid #B7DFBC;"
                "border-radius:999px;padding:3px 10px;font-size:11px;"
                "font-family:'JetBrains Mono',Consolas,monospace;"
            )
        right.addWidget(sample_badge, alignment=Qt.AlignmentFlag.AlignRight)
        close_btn = QPushButton(" 关 闭 报 告 ")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setMinimumHeight(36)
        # 注意：Qt 样式表里：子控件要用选择器前缀，且伪状态前面需要空格
        close_btn.setStyleSheet(
            "QPushButton{"
            f"background:{C_INK};color:#FFFFFF;border-radius:8px;"
            "padding:6px 18px;font-size:13px;font-weight:600;border:none;"
            "}"
            "QPushButton:hover{"
            "background:#111827;"
            "}"
            "QPushButton:pressed{"
            "background:#0B1220;"
            "}"
        )
        close_btn.clicked.connect(self.accept)
        right.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)
        right.addStretch(1)
        lay.addLayout(right)

        wrap.setMinimumHeight(120)
        return wrap

    # --- 读者画像 Banner ---------------------------------------------
    def _build_portrait_banner(self) -> QWidget:
        wrap = QFrame()
        # 彻底无卡片/无边框/无底色：米白纸背景上纯粹的文本
        wrap.setFrameShape(QFrame.Shape.NoFrame)
        wrap.setStyleSheet(
            "background:transparent;"
            "border:none;"
        )
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(22)

        portrait = self._data.get("reader_portrait", {}) or {}

        # 左：诗性总结（标签+大段叙事，无卡片）
        left = QVBoxLayout()
        left.setSpacing(10)
        mark = QLabel("『 你 』  —  读者画像总结")
        mark.setStyleSheet(
            f"color:{C_DANGER};font-weight:700;font-size:12px;letter-spacing:2px;"
            "background:transparent;border:none;"
        )
        left.addWidget(mark)
        summary = QLabel(portrait.get("summary", "数据尚未丰富，无法生成画像。"))
        summary.setWordWrap(True)
        summary.setStyleSheet(
            f"color:{C_INK};font-size:14px;line-height:1.9;"
            "font-family:'Microsoft YaHei UI','PingFang SC',serif;"
            "background:transparent;border:none;"
        )
        left.addWidget(summary, 1)
        lay.addLayout(left, 3)

        # 细分隔线（柔和的淡化虚线，不抢戏，不要厚重实色 VLine）
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet(
            "background:transparent;"
            "border:none;"
            "border-left:1px dashed #DCD9CC;"
        )
        lay.addWidget(sep)

        # 右：金句（无边框、左右大字引号上下夹住正文；整块无底色）
        motto = portrait.get("motto", "")
        motto_wrap = QWidget()
        motto_wrap.setStyleSheet("background:transparent;border:none;")
        m = QVBoxLayout(motto_wrap)
        m.setContentsMargins(0, 0, 0, 0)
        m.setSpacing(2)
        q = QLabel("“")
        q.setStyleSheet(
            f"color:{C_DANGER};font-family:Georgia,serif;font-size:30px;line-height:26px;"
            "background:transparent;border:none;"
        )
        m.addWidget(q)
        text = QLabel(motto or "再读几页，你就会把一句自己的话留在这里。")
        text.setWordWrap(True)
        text.setStyleSheet(
            f"color:{C_INK};font-size:14px;font-weight:500;line-height:1.9;"
            "font-family:'Microsoft YaHei UI','PingFang SC',serif;font-style:italic;"
            "padding-left:6px;padding-right:6px;"
            "background:transparent;border:none;"
        )
        m.addWidget(text, 1)
        uq = QLabel("”")
        uq.setStyleSheet(
            f"color:{C_GOLD};font-family:Georgia,serif;font-size:30px;line-height:26px;"
            "background:transparent;border:none;"
        )
        uq.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        m.addWidget(uq)
        motto_wrap.setMinimumHeight(150)
        lay.addWidget(motto_wrap, 2)
        wrap.setMinimumHeight(150)
        return wrap

    # --- 4 KPI 整板 ---------------------------------------------------
    def _build_kpi_board(self) -> QWidget:
        kpi_data = self._data.get("kpi", {}) or {}
        panel = QFrame()
        panel.setStyleSheet(
            f"background:{C_CARD};border:1px solid {C_BORDER};border-radius:14px;"
        )
        inner = QGridLayout(panel)
        inner.setContentsMargins(20, 18, 20, 14)
        inner.setHorizontalSpacing(18)
        for c in range(4):
            inner.setColumnStretch(c, 1)
        inner.setRowMinimumHeight(0, 28)
        inner.setRowMinimumHeight(1, 54)
        inner.setRowMinimumHeight(2, 18)
        inner.setRowMinimumHeight(3, 16)

        def _card(col: int, title: str, value: str, aux: str, tone: str) -> None:
            # 标题
            t = QLabel(title)
            t.setStyleSheet(
                f"color:{C_MUTED};font-size:12px;letter-spacing:1px;"
            )
            inner.addWidget(t, 0, col)
            # 值
            v = QLabel(value)
            v.setStyleSheet(
                f"color:{C_INK};font-size:32px;font-weight:800;"
                f"font-family:'JetBrains Mono',Consolas,monospace;"
                "letter-spacing:0.5px;"
            )
            inner.addWidget(v, 1, col)
            # 辅助
            a = QLabel(aux)
            a.setStyleSheet(f"color:{tone};font-size:12px;")
            inner.addWidget(a, 2, col)
            # 进度条
            bar = QProgressBar()
            bar.setMaximumHeight(6)
            bar.setTextVisible(False)
            bar.setStyleSheet(
                f"QProgressBar{{background:{C_BORDER};border-radius:3px;border:none;height:6px;}}"
                f"QProgressBar::chunk{{background:{tone};border-radius:3px;}}"
            )
            if tone == C_OK:
                bar.setValue(85)
            elif tone == C_GOLD:
                bar.setValue(55)
            elif tone == C_DANGER:
                bar.setValue(25)
            else:
                bar.setValue(40)
            inner.addWidget(bar, 3, col)

        total_sec_fmt = kpi_data.get("total_seconds_fmt", "0h")
        days = kpi_data.get("reading_days", 0)
        bc = kpi_data.get("book_count", 0)
        fc = kpi_data.get("finished_count", 0)
        avg = kpi_data.get("avg_daily_minutes", 0)
        notes = kpi_data.get("note_count", 0)
        w_fmt = kpi_data.get("weekly_seconds_fmt", "-")
        m_fmt = kpi_data.get("monthly_seconds_fmt", "-")

        _card(0, "📚 累计阅读时长", total_sec_fmt, f"本周 {w_fmt} · 本月 {m_fmt}", C_INK_SOLID)
        _card(1, "📅 阅读天数", f"{days}", f"日均约 {avg} 分钟", C_OK)
        _card(2, "🏛 藏书/完成", f"{bc}  /  {fc}", f"书架共 {bc} 本，读完 {fc}", C_GOLD)
        _card(3, "✒ 笔记 / 划线", f"{notes} 条", "含划线、想法、摘录", C_DANGER)
        return panel

    # --- 读者画像 · 划线行 -------------------------------------------
    def _build_highlights_row(self, idx: int, hl: dict) -> QWidget:
        row = QFrame()
        row.setStyleSheet(
            f"background:{C_CARD};border:1px solid {C_BORDER};border-radius:10px;"
        )
        lay = QHBoxLayout(row)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(14)

        # 编号
        n = QLabel(f"{idx:02d}")
        n.setMinimumWidth(32)
        n.setAlignment(Qt.AlignmentFlag.AlignCenter)
        n.setStyleSheet(
            f"background:{C_PAPER};color:{C_INK_SOFT};"
            "font-family:'JetBrains Mono',Consolas,monospace;font-weight:700;"
            "font-size:12px;border-radius:8px;padding:4px 6px;"
        )
        lay.addWidget(n)

        # 文本
        v = QVBoxLayout()
        v.setSpacing(4)
        quote = QLabel(f"“{hl.get('quote', '')}”")
        quote.setWordWrap(True)
        quote.setStyleSheet(
            f"color:{C_INK};font-size:13px;line-height:1.7;"
            "font-family:'Microsoft YaHei UI','PingFang SC',serif;"
        )
        v.addWidget(quote)
        src = QLabel(
            f"——  《{hl.get('book', '')}》 · {hl.get('author', '')}"
        )
        src.setStyleSheet(f"color:{C_MUTED};font-size:11px;")
        v.addWidget(src)
        lay.addLayout(v, 1)
        row.setMinimumHeight(60)
        return row
