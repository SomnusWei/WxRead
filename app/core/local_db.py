"""本地数据管理：书架 + 进度 + 统计 + 章节池持久化。

文件位置：%APPDATA%/WxReadAssistant/local_db.json 和 chapter_cache.json
所有操作线程安全（RLock），原子写（tmp + replace）。
"""
from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.core.config import get_app_dir


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


class LocalDB:
    """本地数据管理（书架 + 进度 + 统计 + 章节池）。

    数据模型参考 `WxReadAssistant-v2.0重构方案.md` 5.1 / 5.2。
    """

    def __init__(self, data_dir: str | os.PathLike[str] | None = None) -> None:
        base = Path(data_dir) if data_dir else get_app_dir()
        base.mkdir(parents=True, exist_ok=True)
        self._dir = base
        self._db_path = base / "local_db.json"
        self._cache_path = base / "chapter_cache.json"
        self._lock = threading.RLock()
        self._db: dict[str, Any] = {}
        self._cache: dict[str, dict] = {}
        self._load()

    # ---------- IO ----------
    def _load(self) -> None:
        """从磁盘加载数据，文件不存在或损坏时使用空骨架。"""
        with self._lock:
            self._db = self._read_json(self._db_path, self._empty_db())
            self._cache = self._read_json(self._cache_path, {})

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return deepcopy(default)
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, type(default)) and not (
                isinstance(data, dict) and isinstance(default, dict)
            ):
                return deepcopy(default)
            return data
        except (OSError, json.JSONDecodeError):
            return deepcopy(default)

    def _save_db(self) -> None:
        self._atomic_write(self._db_path, self._db)

    def _save_cache(self) -> None:
        self._atomic_write(self._cache_path, self._cache)

    def _atomic_write(self, path: Path, payload: Any) -> None:
        tmp = path.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            tmp.replace(path)
        except OSError:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            raise

    @staticmethod
    def _empty_db() -> dict[str, Any]:
        return {
            "last_sync_time": "",
            "shelf": {"books": [], "total_count": 0},
            "reading_stats": {
                "today_seconds": 0,
                "today_date": "",          # YYYY-MM-DD，跨天重置 today_seconds 用
                "weekly_seconds": 0,
                "monthly_seconds": 0,
                "total_seconds": 0,
                "updated_at": "",
            },
            # 永久跳过的书（套装 / 公众号等不支持自动阅读的 bookId）
            # 结构：{bookId: {"reason": str, "title": str, "marked_at": iso字符串}}
            "blacklist": {},
        }

    # ---------- 书架 ----------
    def get_shelf(self) -> list[dict]:
        """返回本地书架 books 列表（深拷贝，调用方可自由修改）。"""
        with self._lock:
            shelf = self._db.get("shelf", {})
            books = shelf.get("books", [])
            return deepcopy(books) if isinstance(books, list) else []

    def get_shelf_meta(self) -> dict[str, Any]:
        with self._lock:
            shelf = self._db.get("shelf", {})
            return {
                "total_count": shelf.get("total_count", 0),
                "last_sync_time": self._db.get("last_sync_time", ""),
            }

    def update_shelf(self, books: list[dict], total_count: int | None = None) -> None:
        """写入完整书架（Skill /shelf/sync 拉取后调用）。

        采用 bookId 合并策略：保留本地进度字段（progress/current_chapter_uid/
        current_chapter_title/record_reading_time/last_read_time），其他字段以
        Skill 数据覆盖。
        """
        with self._lock:
            existing = {b.get("bookId"): b for b in self.get_shelf() if b.get("bookId")}
            merged: list[dict] = []
            for book in books:
                bid = book.get("bookId")
                if not bid:
                    continue
                base = deepcopy(book)
                old = existing.get(bid)
                if old:
                    # 保留本地进度字段，仅当 Skill 提供新值时覆盖
                    for k in (
                        "progress",
                        "current_chapter_uid",
                        "current_chapter_title",
                        "record_reading_time",
                        "last_read_time",
                        "blacklisted",
                        "blacklist_reason",
                    ):
                        if k in old and k not in base:
                            base[k] = old[k]
                merged.append(base)
            # 顶层 blacklist 中的书，确保条目仍带黑名单标记（以黑名单 reason 为准）
            blacklist = self._db.get("blacklist", {})
            if not isinstance(blacklist, dict):
                blacklist = {}
            for base in merged:
                entry = blacklist.get(str(base.get("bookId")))
                if isinstance(entry, dict):
                    base["blacklisted"] = True
                    base["blacklist_reason"] = entry.get("reason", "")
            self._db["shelf"] = {
                "books": merged,
                "total_count": total_count if total_count is not None else len(merged),
            }
            self._db["last_sync_time"] = _now_iso()
            self._save_db()

    def is_book_blacklisted(self, book_id: str) -> bool:
        """该书是否已被永久跳过。"""
        with self._lock:
            blacklist = self._db.get("blacklist", {})
            if not isinstance(blacklist, dict):
                return False
            return str(book_id) in blacklist

    def mark_book_blacklisted(self, book_id: str, reason: str, title: str = "") -> None:
        """永久跳过该书：写入顶层 blacklist，并在书架对应条目上打
        blacklisted=True / blacklist_reason 字段，最后落盘。已存在则覆盖。"""
        bid = str(book_id or "").strip()
        if not bid or bid.lower() == "none":
            return
        with self._lock:
            blacklist = self._db.get("blacklist", {})
            if not isinstance(blacklist, dict):
                blacklist = self._db["blacklist"] = {}
            blacklist[bid] = {
                "reason": reason,
                "title": title,
                "marked_at": _now_iso(),
            }
            for book in self._db.get("shelf", {}).get("books", []):
                if str(book.get("bookId")) == bid:
                    book["blacklisted"] = True
                    book["blacklist_reason"] = reason
                    break
            self._save_db()

    def get_book(self, book_id: str) -> dict | None:
        with self._lock:
            for book in self._db.get("shelf", {}).get("books", []):
                if str(book.get("bookId")) == str(book_id):
                    return deepcopy(book)
        return None

    # ---------- 单本书进度 ----------
    def get_book_progress(self, book_id: str) -> int:
        book = self.get_book(book_id)
        if not book:
            return 0
        try:
            return int(book.get("progress", 0))
        except (TypeError, ValueError):
            return 0

    def update_book_progress(
        self,
        book_id: str,
        progress: int,
        chapter_uid: int | None = None,
        chapter_title: str | None = None,
    ) -> None:
        """更新单本书进度（本地累加或 Skill 覆盖）。

        chapter_uid / chapter_title 给定时一并更新当前章节信息。
        """
        with self._lock:
            books = self._db.get("shelf", {}).get("books", [])
            for book in books:
                if str(book.get("bookId")) == str(book_id):
                    try:
                        book["progress"] = max(0, min(100, int(progress)))
                    except (TypeError, ValueError):
                        book["progress"] = 0
                    if chapter_uid is not None:
                        book["current_chapter_uid"] = chapter_uid
                    if chapter_title is not None:
                        book["current_chapter_title"] = chapter_title
                    book["last_read_time"] = int(datetime.now().timestamp())
                    self._save_db()
                    return

    def increment_book_progress(self, book_id: str, delta: float) -> int:
        """本地累加进度，返回更新后的整数进度。

        用于单次阅读成功后按章节比例微调 progress。
        """
        with self._lock:
            books = self._db.get("shelf", {}).get("books", [])
            for book in books:
                if str(book.get("bookId")) == str(book_id):
                    try:
                        cur = float(book.get("progress", 0))
                    except (TypeError, ValueError):
                        cur = 0.0
                    new_val = max(0.0, min(100.0, cur + delta))
                    book["progress"] = int(round(new_val))
                    self._save_db()
                    return int(book["progress"])
            return 0

    # ---------- 选书选章 ----------
    def get_unread_books(self) -> list[dict]:
        """返回进度 < 100% 的书，按进度降序（优先读进度高的）。

        永久跳过（顶层 blacklist 或条目 blacklisted=True）的书不返回。
        """
        with self._lock:
            books = self.get_shelf()
            blacklist = self._db.get("blacklist", {})
            if not isinstance(blacklist, dict):
                blacklist = {}
            blacklist_ids = {str(bid) for bid in blacklist}
        unread = [
            b
            for b in books
            if self._safe_progress(b) < 100
            and str(b.get("bookId")) not in blacklist_ids
            and not b.get("blacklisted")
        ]
        unread.sort(key=lambda b: self._safe_progress(b), reverse=True)
        return unread

    def select_next_book(self) -> dict | None:
        """选下一本要读的书（进度最高的未读完的书）。"""
        unread = self.get_unread_books()
        return unread[0] if unread else None

    @staticmethod
    def _safe_progress(book: dict) -> int:
        try:
            return int(book.get("progress", 0))
        except (TypeError, ValueError):
            return 0

    # ---------- 阅读统计 ----------
    def get_reading_stats(self) -> dict[str, Any]:
        with self._lock:
            stats = self._db.get("reading_stats", {})
            return deepcopy(stats) if isinstance(stats, dict) else {}

    def update_reading_stats(self, stats: dict[str, Any]) -> None:
        with self._lock:
            cur = self._db.get("reading_stats", {})
            if not isinstance(cur, dict):
                cur = {}
            today_iso = date.today().isoformat()
            is_new_day = str(cur.get("today_date", "")) != today_iso
            if is_new_day:
                cur["today_date"] = today_iso
                cur["today_seconds"] = 0
            # 先拷贝一份再处理：绝对不能原地修改调用方传进来的
            # dict——scheduler/FetchThread 会在调用后再用硬下标读取
            # local_stats['today_seconds']，pop 会把 caller 字典掏坏
            # 造成 KeyError（参见 2026-08-27 复现的 stack）。
            incoming = dict(stats or {})
            if is_new_day:
                # 跨天时丢弃外部传入的 today_seconds，防止 Skill
                # 缓存把昨天的值写回本地
                incoming.pop("today_seconds", None)
            # today_seconds 以 Skill 服务端统计为权威基线：
            # 本地每次成功固定 +45s 只是估算，系统性高于服务端口径，
            # 用 max() 会让误差永远无法被纠正（官方 36m、本地 49m 时界面纹丝不动）。
            # 规则：Skill 值 > 0 → 覆盖为基线，之后 add_today_seconds 继续累加；
            # Skill 值为 0/缺失（该项请求失败或当天确实无统计）→ 保留本地值，
            # 避免延迟/故障把真实进度清零。
            if "today_seconds" in incoming:
                val = incoming.pop("today_seconds")
                try:
                    inc_sec = int(val)
                except (TypeError, ValueError):
                    inc_sec = 0
                if inc_sec > 0:
                    cur["today_seconds"] = inc_sec
            cur.update(incoming)
            cur["today_date"] = today_iso
            cur["updated_at"] = _now_iso()
            self._db["reading_stats"] = cur
            self._save_db()

    def get_today_seconds(self) -> int:
        today_iso = date.today().isoformat()
        with self._lock:
            cur = self._db.get("reading_stats", {})
            if not isinstance(cur, dict):
                return 0
            # 跨天重置：日期不匹配时 today_seconds 归零并持久化
            if str(cur.get("today_date", "")) != today_iso:
                cur["today_date"] = today_iso
                cur["today_seconds"] = 0
                cur["updated_at"] = _now_iso()
                self._db["reading_stats"] = cur
                self._save_db()
                return 0
            try:
                return int(cur.get("today_seconds", 0))
            except (TypeError, ValueError):
                return 0

    def reset_today_seconds(self) -> int:
        """手动重置今日已完成时长为 0。"""
        today_iso = date.today().isoformat()
        with self._lock:
            cur = self._db.get("reading_stats", {})
            if not isinstance(cur, dict):
                cur = {}
            cur["today_date"] = today_iso
            cur["today_seconds"] = 0
            cur["updated_at"] = _now_iso()
            self._db["reading_stats"] = cur
            self._save_db()
            return 0

    def add_today_seconds(self, delta_sec: int) -> int:
        """本地累加今日阅读秒数（read_once 成功后调用）。"""
        today_iso = date.today().isoformat()
        with self._lock:
            cur = self._db.get("reading_stats", {})
            if not isinstance(cur, dict):
                cur = {}
            # 跨天重置：日期不匹配时 today_seconds 归零
            if str(cur.get("today_date", "")) != today_iso:
                cur["today_date"] = today_iso
                cur["today_seconds"] = 0
            try:
                sec = int(cur.get("today_seconds", 0))
            except (TypeError, ValueError):
                sec = 0
            sec = max(0, sec + max(0, int(delta_sec)))
            cur["today_seconds"] = sec
            cur["today_date"] = today_iso
            cur["updated_at"] = _now_iso()
            self._db["reading_stats"] = cur
            self._save_db()
            return sec

    # ---------- 章节池 ----------
    def get_chapters(self, book_id: str) -> list[dict]:
        """返回指定书的章节列表（深拷贝）。"""
        with self._lock:
            entry = self._cache.get(str(book_id))
            if not isinstance(entry, dict):
                return []
            chapters = entry.get("chapters", [])
            return deepcopy(chapters) if isinstance(chapters, list) else []

    def update_chapters(self, book_id: str, title: str, chapters: list[dict]) -> None:
        """写入指定书的章节池（Skill /book/chapterinfo 拉取后调用）。"""
        with self._lock:
            self._cache[str(book_id)] = {
                "title": title,
                "chapters": deepcopy(chapters),
                "updated_at": _now_iso(),
            }
            self._save_cache()

    def get_chapter_by_uid(self, book_id: str, chapter_uid: Any) -> dict | None:
        """根据 chapterUid 查找章节。"""
        chapters = self.get_chapters(book_id)
        for ch in chapters:
            if str(ch.get("chapterUid")) == str(chapter_uid):
                return ch
        return None

    def get_next_chapter(self, book_id: str, current_uid: Any) -> dict | None:
        """返回 current_uid 之后的下一个章节；如果当前是最后一章或未找到，返回 None。"""
        chapters = self.get_chapters(book_id)
        found = False
        for ch in chapters:
            if found:
                return ch
            if str(ch.get("chapterUid")) == str(current_uid):
                found = True
        return None

    def get_first_chapter(self, book_id: str) -> dict | None:
        chapters = self.get_chapters(book_id)
        return chapters[0] if chapters else None

    def has_chapters(self, book_id: str) -> bool:
        with self._lock:
            entry = self._cache.get(str(book_id))
            if not isinstance(entry, dict):
                return False
            return bool(entry.get("chapters"))

    # ---------- 授权 ----------
    def get_license(self) -> dict[str, Any]:
        """返回授权段（深拷贝；不存在时返回空 dict）。"""
        with self._lock:
            lic = self._db.get("license")
            return deepcopy(lic) if isinstance(lic, dict) else {}

    def set_license(self, patch: dict[str, Any]) -> None:
        """合并写入授权段并落盘。"""
        with self._lock:
            lic = self._db.setdefault("license", {})
            if not isinstance(lic, dict):
                lic = self._db["license"] = {}
            lic.update(deepcopy(patch))
            self._save_db()

    # ---------- 维护 ----------
    def clear_all(self) -> None:
        """清空所有本地数据（清除 Cookie 时一并调用）。

        注意：license 段必须保留——清 Cookie 不应导致已激活状态丢失。
        """
        with self._lock:
            license_kept = self._db.get("license")
            self._db = self._empty_db()
            if isinstance(license_kept, dict) and license_kept:
                self._db["license"] = deepcopy(license_kept)
            self._cache = {}
            self._save_db()
            self._save_cache()

    def clear_chapters(self) -> None:
        with self._lock:
            self._cache = {}
            self._save_cache()
