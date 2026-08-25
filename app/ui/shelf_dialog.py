"""书架查看对话框（v2 虚拟化列表）。

特性：
  - QListView + QAbstractListModel 虚拟化，支持 365+ 本书籍流畅滚动
  - 显示：书名 / 作者 / 进度 / 章节数
  - 章节池就绪状态图标
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, QSize
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.local_db import LocalDB
from app.utils.logger import get_logger

log = get_logger(__name__)


class ShelfModel(QAbstractListModel):
    """书架数据模型（虚拟化，按需查询 LocalDB）。"""

    def __init__(self, db: LocalDB, parent: QObject | None = None) -> None:  # noqa: F821
        super().__init__(parent)
        self._db = db
        self._books: list[dict] = []

    def refresh(self) -> None:
        """从 LocalDB 重新加载书架。"""
        self.beginResetModel()
        self._books = self._db.get_shelf()
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return len(self._books) if not parent.isValid() else 0

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if not index.isValid() or not (0 <= index.row() < len(self._books)):
            return None
        book = self._books[index.row()]
        bid = str(book.get("bookId", ""))
        if role == Qt.ItemDataRole.DisplayRole:
            title = str(book.get("title", "未知"))
            author = str(book.get("author", ""))
            progress = self._safe_progress(book)
            has_chapters = self._db.has_chapters(bid)
            ch_tag = "📖" if has_chapters else "⭕"
            return f"{ch_tag}  {title}  -  {author}  [{progress}%]"
        if role == Qt.ItemDataRole.ToolTipRole:
            title = str(book.get("title", "未知"))
            bid = str(book.get("bookId", ""))
            progress = self._safe_progress(book)
            chapters = self._db.get_chapters(bid)
            ch_count = len(chapters)
            return (
                f"书名：{title}\n"
                f"bookId：{bid}\n"
                f"进度：{progress}%\n"
                f"章节数：{ch_count}\n"
                f"当前章节：{book.get('current_chapter_title', '-')}"
            )
        if role == Qt.ItemDataRole.ForegroundRole:
            progress = self._safe_progress(book)
            if progress >= 100:
                from PySide6.QtGui import QColor
                return QColor("#2d9d3c")  # 已读完绿色
            if progress > 0:
                from PySide6.QtGui import QColor
                return QColor("#2f3b52")  # 在读黑色
            from PySide6.QtGui import QColor
            return QColor("#93a0b8")  # 未读灰色
        return None

    @staticmethod
    def _safe_progress(book: dict) -> int:
        try:
            return int(book.get("progress", 0))
        except (TypeError, ValueError):
            return 0


class ShelfDialog(QDialog):
    """书架查看对话框（虚拟化列表）。"""

    def __init__(self, db: LocalDB, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db = db
        self._model = ShelfModel(db, self)
        self._build_ui()
        self._model.refresh()
        self._update_count()

    def _build_ui(self) -> None:
        self.setWindowTitle("📚 书架")
        self.resize(800, 600)
        self.setMinimumSize(600, 400)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        # 顶部信息
        header = QHBoxLayout()
        title = QLabel("📚 我的书架")
        title.setStyleSheet("font-size:16px;font-weight:700;color:#2f3b52;")
        header.addWidget(title)
        header.addStretch(1)
        self._lbl_count = QLabel("共 0 本")
        self._lbl_count.setStyleSheet("color:#5f6c85;font-size:13px;")
        header.addWidget(self._lbl_count)
        root.addLayout(header)

        # 列表
        self._list = QListView()
        self._list.setModel(self._model)
        self._list.setStyleSheet(
            "QListView{background:#ffffff;border:1px solid #e3e8f1;border-radius:8px;"
            "padding:4px;font-size:13px;}"
            "QListView::item{padding:8px;border-bottom:1px solid #f4f7fc;}"
            "QListView::item:hover{background:#f4f7fc;}"
            "QListView::item:selected{background:#e8f0ff;color:#2d6cdf;}"
        )
        root.addWidget(self._list, 1)

        # 图例
        legend = QLabel("📖 章节池已就绪   ⭕ 章节池未获取   绿色=已读完   灰色=未读")
        legend.setStyleSheet("color:#93a0b8;font-size:11px;")
        legend.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(legend)

        # 按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._btn_refresh = QPushButton("🔄 刷新")
        self._btn_refresh.setStyleSheet(
            "QPushButton{background:#eef2f8;color:#2f3b52;border:none;border-radius:8px;"
            "padding:8px 16px;font-size:13px;}"
            "QPushButton:hover{background:#e2e8f4;}"
        )
        self._btn_refresh.clicked.connect(self._on_refresh)
        self._btn_close = QPushButton("关闭")
        self._btn_close.setStyleSheet(
            "QPushButton{background:#2d6cdf;color:#fff;border:none;border-radius:8px;"
            "padding:8px 16px;font-size:13px;font-weight:600;}"
            "QPushButton:hover{background:#265bc0;}"
        )
        self._btn_close.clicked.connect(self.accept)
        btn_row.addWidget(self._btn_refresh)
        btn_row.addWidget(self._btn_close)
        root.addLayout(btn_row)

    def _update_count(self) -> None:
        count = self._model.rowCount()
        meta = self._db.get_shelf_meta()
        self._lbl_count.setText(
            f"共 {count} 本（本地 {count} / 总 {meta.get('total_count', 0)}）"
        )

    def _on_refresh(self) -> None:
        self._model.refresh()
        self._update_count()
