"""数据同步路由：书架（SSE 流式）/ 阅读统计 / 当前书进度。"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse

from app.utils.logger import get_logger
from app.web.state import has_valid_cookie, svc

log = get_logger(__name__)
router = APIRouter(prefix="/api/sync", tags=["sync"])

# 首次书架同步时并发拉章/进度的未读书上限（防 Skill 过载）
_INITIAL_BOOK_CAP = 10


def _sse(event: dict) -> bytes:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")


@router.post("/shelf")
def sync_shelf():
    """全量书架同步（POST + SSE）。

    事件流：starting → shelf_ok → chapter(每本) → done / error
    """
    s = svc()
    if not has_valid_cookie(s.cfg):
        return JSONResponse(status_code=409,
                            content={"ok": False, "code": "NOT_LOGGED_IN",
                                     "msg": "请先扫码登录"})

    def gen():
        t0 = time.time()
        yield _sse({"event": "starting", "msg": "开始同步书架…"})
        try:
            books = s.skill.fetch_shelf()
        except Exception as exc:  # noqa: BLE001
            log.exception("书架同步失败：%s", exc)
            yield _sse({"event": "error", "msg": f"书架获取失败：{exc}"})
            return
        if not books:
            yield _sse({"event": "error", "msg": "书架为空（Skill 返回 0 本），请检查 Key/网络"})
            return
        s.db.update_shelf(books)
        yield _sse({"event": "shelf_ok", "count": len(books),
                    "msg": f"书架 {len(books)} 本已写入"})

        # 首次同步：未读书前 N 本补章节目录+进度；已同步过（有章节）的书跳过
        targets = []
        for b in s.db.get_unread_books():
            bid = str(b.get("bookId") or "")
            if bid and not s.db.has_chapters(bid):
                targets.append(b)
            if len(targets) >= _INITIAL_BOOK_CAP:
                break
        ok_n = 0
        for idx, b in enumerate(targets, 1):
            bid = str(b.get("bookId") or "")
            title = str(b.get("title") or "未知")
            yield _sse({"event": "chapter", "idx": idx, "total": len(targets),
                        "title": title, "msg": f"拉取目录 {idx}/{len(targets)}：{title}"})
            try:
                chapters = s.skill.fetch_chapters(bid)
                if chapters:
                    s.db.update_chapters(bid, title, chapters)
                prog = s.skill.fetch_progress(bid)
                if prog is not None:
                    s.db.update_book_progress(bid, prog)
                ok_n += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("《%s》目录/进度拉取失败：%s", title, exc)
                yield _sse({"event": "warn", "title": title, "msg": f"《{title}》拉取失败：{exc}"})
            time.sleep(0.4)
        yield _sse({"event": "done", "shelf": len(books), "chapter_books": ok_n,
                    "elapsed_sec": round(time.time() - t0, 1),
                    "msg": f"同步完成：书架 {len(books)} 本，{ok_n} 本已补目录"})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/stats")
def sync_stats():
    s = svc()
    if not has_valid_cookie(s.cfg):
        return JSONResponse(status_code=409,
                            content={"ok": False, "code": "NOT_LOGGED_IN",
                                     "msg": "请先扫码登录"})
    try:
        data = s.skill.fetch_reading_stats()
    except Exception as exc:  # noqa: BLE001
        log.exception("统计同步失败：%s", exc)
        return JSONResponse(status_code=502,
                            content={"ok": False, "msg": f"Skill 统计拉取失败：{exc}"})
    s.db.update_reading_stats(data)
    return {"ok": True, "data": s.db.get_reading_stats()}


@router.post("/progress")
def sync_progress():
    """立即同步当前在读这本书的进度（以服务端为准）。"""
    s = svc()
    st = s.sched.get_status()
    book = st.get("book")
    if not book or not book.get("bookId"):
        return JSONResponse(
            status_code=409,
            content={"ok": False, "code": "NO_CURRENT_BOOK",
                     "msg": "当前没有进行中的书籍（启动阅读后会自动选择）"},
        )
    bid = str(book["bookId"])
    try:
        prog = s.skill.fetch_progress(bid)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=502,
                            content={"ok": False, "msg": f"进度拉取失败：{exc}"})
    if prog is None:
        return JSONResponse(status_code=502,
                            content={"ok": False, "msg": "服务端未返回进度"})
    percent = prog.get("percent")
    if percent is None:
        return JSONResponse(status_code=502,
                            content={"ok": False, "msg": f"进度数据缺 percent 字段：{prog}"})
    try:
        pct = int(float(percent))
    except (TypeError, ValueError):
        return JSONResponse(status_code=502,
                            content={"ok": False, "msg": f"进度值无法解析：{percent}"})
    s.db.update_book_progress(bid, pct)
    return {"ok": True, "data": {"book_id": bid, "progress": pct}}
