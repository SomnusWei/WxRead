"""书籍黑名单（只读展示；拉黑由调度器自动执行）。"""
from __future__ import annotations

from fastapi import APIRouter

from app.web.state import svc

router = APIRouter(prefix="/api/books", tags=["books"])


@router.get("/blacklist")
def blacklist():
    s = svc()
    db_raw = s.db._db.get("blacklist", {})  # noqa: SLF001 - 只读展示
    items = []
    if isinstance(db_raw, dict):
        for bid, info in db_raw.items():
            if not isinstance(info, dict):
                continue
            items.append({
                "book_id": str(bid),
                "title": str(info.get("title") or "未知书名"),
                "reason": str(info.get("reason") or ""),
                "marked_at": str(info.get("marked_at") or ""),
            })
    items.sort(key=lambda x: x["marked_at"], reverse=True)
    return {"ok": True, "data": items, "count": len(items)}
