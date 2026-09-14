# -*- coding: utf-8 -*-
"""M1 冒烟测试（纯 Python，无需 Docker/Qt）。

运行：cd wxread_docekr && python tests/test_smoke.py
所有数据隔离在 tests/_tmp_data 下（APPDATA 重定向）。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = ROOT / "tests" / "_tmp_data"
if _TMP.exists():
    shutil.rmtree(_TMP)
os.environ["APPDATA"] = str(_TMP)
os.environ["WXREAD_RUNTIME"] = "pure"

from app.core.runtime import QObject, QThread, Signal  # noqa: E402
from app.core.config import ConfigStore  # noqa: E402
from app.core.local_db import LocalDB  # noqa: E402
from app.core.scheduler import Scheduler  # noqa: E402
from app.utils.logger import purge_old_logs  # noqa: E402


class _SignalWorker(QThread):
    tick = Signal(int)

    def __init__(self):
        super().__init__()
        self.n = 0
        self._go = threading.Event()

    def run(self):
        for i in range(3):
            self.n += 1
            self.tick.emit(self.n)
            time.sleep(0.02)
        self._go.set()


class TestRuntimeShim(unittest.TestCase):
    def test_signal_connect_emit_disconnect(self):
        class E(QObject):
            changed = Signal(str, int)
        got = []
        e = E()
        fn = lambda s, i: got.append((s, i))
        e.changed.connect(fn)
        e.changed.connect(fn)  # 幂等
        e.changed.emit("a", 1)
        e.changed.disconnect(fn)
        e.changed.emit("b", 2)
        self.assertEqual(got, [("a", 1)])

    def test_qthread_restart_same_instance(self):
        w = _SignalWorker()
        seen = []
        w.tick.connect(lambda n: seen.append(n))
        w.start()
        self.assertTrue(w.wait(5000))
        self.assertEqual((w.n, seen), (3, [1, 2, 3]))
        # 同实例再启动（对齐 QThread 语义）
        w.start()
        self.assertTrue(w.wait(5000))
        self.assertEqual(w.n, 6)
        self.assertFalse(w.isRunning())

    def test_request_interruption(self):
        class W(QThread):
            def __init__(self):
                super().__init__()
                self._stop_event = threading.Event()

            def run(self):
                while not self._stop_event.wait(0.05):
                    pass
        w = W()
        w.start()
        w.requestInterruption()
        self.assertTrue(w.wait(3000))


class _FakeApi(QObject):
    message = Signal(str)
    warning = Signal(str)
    error = Signal(str)
    cookie_invalid = Signal(str, str)

    def __init__(self, fail_kind: str = "network", ok: bool = False):
        super().__init__()
        self._fail_kind = fail_kind
        self._ok = ok
        self.last_read_kind = ""
        self.last_read_detail = ""
        self.session_calls = 0

    def read_once(self, book_id, chapter_uid):
        self.last_read_kind = "ok" if self._ok else self._fail_kind
        self.last_read_detail = "fake"
        return self._ok

    def check_session(self, *, timeout=8):
        return True

    def ensure_session(self):
        return True


class _FakeSkill:
    def fetch_chapters(self, book_id):
        return []

    def fetch_progress(self, book_id):
        return None

    def fetch_reading_stats(self):
        return {}

    def fetch_shelf(self):
        return []


class _FakeNotifier:
    def __init__(self, cfg):
        self._cfg = cfg
        self.auto_paused_calls = []

    def notify_auto_paused(self, fail_count, last_kind):
        self.auto_paused_calls.append((fail_count, last_kind))

    def notify_risk_control(self, *a, **k):
        return False

    def notify_daily_start(self, t):
        pass

    def notify_daily_done(self, a, b):
        pass

    def notify_cookie_fail(self, a, b):
        pass


def _make_scheduler(cfg, db, notifier, *, ok=False, fail_kind="network"):
    api = _FakeApi(fail_kind=fail_kind, ok=ok)
    sched = Scheduler(
        api=api, skill_api=_FakeSkill(), local_db=db,
        config=cfg, notifier=notifier,
    )
    return sched, api


class TestFailurePause(unittest.TestCase):
    def setUp(self):
        ConfigStore._instance = None
        self.cfg = ConfigStore()
        self.db = LocalDB()
        self.notifier = _FakeNotifier(self.cfg)
        self.db.update_shelf([{
            "bookId": "330000001", "title": "测试书", "progress": 10,
        }])
        self.db.update_chapters("330000001", "测试书", [
            {"chapterUid": 1, "title": "第一章", "wordCount": 3000},
            {"chapterUid": 2, "title": "第二章", "wordCount": 3000},
        ])

    def test_second_level_pause_and_manual_resume(self):
        self.cfg.update_dict("risk", {
            "fail_streak_threshold": 3, "fail_pause_threshold": 5,
        })
        sched, _ = _make_scheduler(self.cfg, self.db, self.notifier)
        self.assertTrue(sched._select_book_and_chapter())
        for i in range(4):
            self.assertFalse(sched._do_one_reading_step())
        # 第 4 次：仅第一级冷却，未暂停
        self.assertIsNone(sched._auto_paused_info)
        self.assertTrue(sched._pause_event.is_set())
        self.assertFalse(sched._do_one_reading_step())
        # 第 5 次：真正暂停
        self.assertIsNotNone(sched._auto_paused_info)
        self.assertFalse(sched._pause_event.is_set())
        persisted = self.cfg.get("scheduler.auto_paused")
        self.assertEqual(persisted["fail_count"], 5)
        self.assertEqual(len(self.notifier.auto_paused_calls), 1)
        # 手动恢复
        sched.manual_resume()
        self.assertTrue(sched._pause_event.is_set())
        self.assertIsNone(self.cfg.get("scheduler.auto_paused"))
        self.assertEqual(sched._fail_streak, 0)
        self.assertEqual(sched._risk_pause_until, 0.0)

    def test_disable_second_level(self):
        self.cfg.update_dict("risk", {
            "fail_streak_threshold": 100, "fail_pause_threshold": 0,
        })
        sched, _ = _make_scheduler(self.cfg, self.db, self.notifier)
        sched._select_book_and_chapter()
        for _ in range(30):
            self.assertFalse(sched._do_one_reading_step())
        self.assertIsNone(sched._auto_paused_info)
        self.assertTrue(sched._pause_event.is_set())

    def test_session_invalid_not_counted(self):
        self.cfg.update_dict("risk", {
            "fail_streak_threshold": 100, "fail_pause_threshold": 5,
        })
        sched, _ = _make_scheduler(
            self.cfg, self.db, self.notifier, fail_kind="session_invalid"
        )
        sched._select_book_and_chapter()
        for _ in range(10):
            sched._do_one_reading_step()
        self.assertIsNone(sched._auto_paused_info)
        self.assertEqual(sched._fail_streak, 0)


class TestBlacklistSelection(unittest.TestCase):
    def setUp(self):
        ConfigStore._instance = None
        self.cfg = ConfigStore()
        self.db = LocalDB()
        self.notifier = _FakeNotifier(self.cfg)
        self.db.update_shelf([
            {"bookId": "CB_taozhuang", "title": "某套装合集", "progress": 90},
            {"bookId": "MP_gongzhonghao", "title": "某公众号", "progress": 80},
            {"bookId": "330000002", "title": "正常书", "progress": 50},
        ])
        self.db.update_chapters("330000002", "正常书", [
            {"chapterUid": 1, "title": "序章正文", "wordCount": 2000},
        ])

    def test_series_blacklisted_and_numeric_selected(self):
        sched, _ = _make_scheduler(self.cfg, self.db, self.notifier)
        self.assertTrue(sched._select_book_and_chapter())
        self.assertEqual(str(sched._current_book["bookId"]), "330000002")
        bl = self.db._db["blacklist"]
        self.assertIn("CB_taozhuang", bl)
        self.assertIn("MP_gongzhonghao", bl)
        # 重建实例模拟重启：黑名单持久
        db2 = LocalDB()
        unread = db2.get_unread_books()
        self.assertEqual([b["bookId"] for b in unread], ["330000002"])

    def test_all_unsupported_returns_none(self):
        self.db.update_shelf([
            {"bookId": "CB_x", "title": "套装A", "progress": 90},
        ])
        sched, _ = _make_scheduler(self.cfg, self.db, self.notifier)
        self.assertFalse(sched._select_book_and_chapter())
        self.assertIn("CB_x", self.db._db["blacklist"])


class TestDailyPlan(unittest.TestCase):
    def setUp(self):
        ConfigStore._instance = None
        self.cfg = ConfigStore()
        self.db = LocalDB()

    def test_plan_in_range_and_persisted(self):
        self.cfg.update_dict("reading", {"min_hours": 1.5, "max_hours": 1.5})
        notifier = _FakeNotifier(self.cfg)
        sched, _ = _make_scheduler(self.cfg, self.db, notifier, ok=True)
        t1 = sched._ensure_daily_plan()
        t2 = sched._ensure_daily_plan()
        self.assertEqual(t1, 90)
        self.assertEqual(t1, t2)


class TestConfigDefaults(unittest.TestCase):
    def test_new_keys(self):
        ConfigStore._instance = None
        cfg = ConfigStore()
        self.assertEqual(cfg.get("risk.fail_pause_threshold"), 15)
        self.assertTrue(cfg.get("push.notify_auto_pause"))
        self.assertEqual(cfg.get("app.log_retention_days"), 7)
        self.assertIsNone(cfg.get("scheduler.auto_paused"))


class TestLogPurge(unittest.TestCase):
    def test_purge_keeps_today_plus_history(self):
        d = Path(tempfile.mkdtemp())
        try:
            from datetime import datetime, timedelta
            today = datetime.now().date()
            (d / "app.log").write_text("today", encoding="utf-8")
            for delta in range(1, 10):
                day = today - timedelta(days=delta)
                (d / f"app.log.{day:%Y-%m-%d}").write_text("x", encoding="utf-8")
            # 未来日期不误删
            future = today + timedelta(days=1)
            (d / f"app.log.{future:%Y-%m-%d}").write_text("x", encoding="utf-8")
            # 孤儿老文件（mtime 30 天前）
            old = d / "app.log.old"
            old.write_text("x", encoding="utf-8")
            old_ts = time.time() - 30 * 86400
            os.utime(old, (old_ts, old_ts))

            removed = purge_old_logs(d, retention_days=7)
            names = {p.name for p in d.iterdir()}
            self.assertIn("app.log", names)
            self.assertIn(f"app.log.{future:%Y-%m-%d}", names)
            for delta in range(1, 7):
                self.assertIn(f"app.log.{(today - timedelta(days=delta)):%Y-%m-%d}", names)
            for delta in range(7, 10):
                self.assertNotIn(f"app.log.{(today - timedelta(days=delta)):%Y-%m-%d}", names)
            self.assertNotIn("app.log.old", names)
            self.assertTrue(len(removed) >= 4)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestImportsNoQt(unittest.TestCase):
    def test_no_pyside_loaded(self):
        # 整个冒烟套件导入链路不应加载 PySide6
        self.assertNotIn("PySide6", sys.modules)


if __name__ == "__main__":
    unittest.main(verbosity=2)
