"""hermes 扫描器接缝测试：临时 SQLite + 租约 JSON，不碰真实数据。"""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import agent_sessions as ag

SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    cwd TEXT,
    title TEXT,
    started_at REAL,
    ended_at REAL,
    end_reason TEXT,
    last_activity_at REAL,
    archived INTEGER DEFAULT 0
);
"""


class HermesFixture:
    def __init__(self, tmp, now):
        self.root = Path(tmp)
        self.now = now
        self.con = sqlite3.connect(self.root / "state.db")
        self.con.executescript(SCHEMA)

    def add(self, sid, age, ended=False, archived=0, cwd="D:/work/gamma", last_act=None):
        act = self.now - age if last_act is None else last_act
        self.con.execute(
            "INSERT INTO sessions (id, cwd, title, started_at, ended_at, end_reason, last_activity_at, archived)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                sid,
                cwd,
                "t-" + sid,
                act - 100,
                act + 1 if ended else None,
                "cli_close" if ended else None,
                act,
                archived,
            ),
        )
        self.con.commit()

    def set_leases(self, session_ids):
        runtime = self.root / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        entries = [
            {"lease_id": f"l{i}", "session_id": sid, "pid": 1000 + i, "updated_at": self.now}
            for i, sid in enumerate(session_ids)
        ]
        (runtime / "active_sessions.json").write_text(
            json.dumps({"entries": entries}), encoding="utf-8"
        )

    def collect(self):
        return ag.collect_sessions({"hermes": self.root}, self.now)


class HermesScannerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.fx = HermesFixture(self._tmp.name, self.now)

    def tearDown(self):
        self.fx.con.close()
        self._tmp.cleanup()

    def test_unended_recent_session_is_run(self):
        self.fx.add("s1", 30)
        (s,) = self.fx.collect()
        self.assertEqual(s["tool"], "hermes")
        self.assertEqual(s["id"], "s1")
        self.assertEqual(s["state"], "RUN")
        self.assertEqual(s["project"], "gamma")
        self.assertEqual(s["age"], 30)

    def test_lease_keeps_old_unended_session_running(self):
        self.fx.add("s1", 300)
        self.fx.set_leases(["s1"])
        (s,) = self.fx.collect()
        self.assertEqual(s["state"], "RUN")

    def test_old_unended_session_without_lease_is_idle(self):
        self.fx.add("s1", 300)
        self.assertEqual(self.fx.collect()[0]["state"], "IDLE")

    def test_ended_session_is_done(self):
        self.fx.add("s1", 300, ended=True)
        self.assertEqual(self.fx.collect()[0]["state"], "DONE")

    def test_recent_ended_session_not_run(self):
        self.fx.add("s1", 10, ended=True)
        self.assertEqual(self.fx.collect()[0]["state"], "DONE")

    def test_archived_excluded(self):
        self.fx.add("s1", 30, archived=1)
        self.assertEqual(self.fx.collect(), [])

    def test_beyond_active_window_excluded(self):
        self.fx.add("s1", 601)
        self.assertEqual(self.fx.collect(), [])

    def test_tasks_are_null(self):
        self.fx.add("s1", 30)
        (s,) = self.fx.collect()
        self.assertIsNone(s["tasks_done"])
        self.assertIsNone(s["tasks_total"])

    def test_missing_db_skips_tool(self):
        empty = Path(self._tmp.name) / "nope"
        empty.mkdir()
        self.assertEqual(ag.collect_sessions({"hermes": empty}, self.now), [])

    def test_broken_lease_file_still_scans(self):
        self.fx.add("s1", 300)
        runtime = self.fx.root / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "active_sessions.json").write_text("{ not json", encoding="utf-8")
        (s,) = self.fx.collect()
        self.assertEqual(s["state"], "IDLE")


if __name__ == "__main__":
    unittest.main()
