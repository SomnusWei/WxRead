"""书架路由：分页/筛选/搜索，附加章数与黑名单标记。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.web.state import svc

router = APIRouter(prefix="/api/shelf", tags=["shelf"])


def _safe_progress(book: dict) -> int:
    try:
        p = int(book.get("progress", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, p))


@router.get("")
def shelf(
    keyword: str = Query(default=""),
    filter: str = Query(default="all"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    s = svc()
    books = s.db.get_shelf()
    kw = keyword.strip().lower()
    items = []
    for b in books:
        title = str(b.get("title") or "")
        author = str(b.get("author") or "")
        if kw and kw not in title.lower() and kw not in author.lower():
            continue
        p = _safe_progress(b)
        if filter == "unread" and p != 0:
            continue
        if filter == "reading" and not (0 < p < 100):
            continue
        if filter == "finished" and p < 100:
            continue
        bid = str(b.get("bookId") or "")
        items.append({
            "book_id": bid,
            "title": title,
            "author": author,
            "progress": p,
            "category": str(b.get("category") or b.get("categoryName") or ""),
            "has_chapters": s.db.has_chapters(bid),
            "chapter_count": len(s.db.get_chapters(bid)),
            "blacklisted": bool(b.get("blacklisted")),
            "blacklist_reason": str(b.get("blacklist_reason") or ""),
            "last_read_time": b.get("last_read_time") or "",
        })
    # 正常书在前（进度降序），黑名单沉底（拉黑时间/书名无关）
    items.sort(key=lambda x: (x["blacklisted"], -x["progress"], x["title"]))
    total = len(items)
    start = (page - 1) * page_size
    page_items = items[start:start + page_size]
    meta = s.db.get_shelf_meta()
    return {
        "ok": True,
        "data": page_items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "meta": meta,
    }
