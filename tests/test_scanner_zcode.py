"""zcode 扫描器接缝测试：临时 SQLite（session + todo），不碰真实数据。"""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import agent_sessions as ag

SCHEMA = """
CREATE TABLE session (
    id TEXT PRIMARY KEY,
    directory TEXT,
    title TEXT,
    time_created INTEGER,
    time_updated INTEGER,
    time_archived INTEGER
);
CREATE TABLE todo (
    session_id TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    content TEXT,
    status TEXT,
    position INTEGER,
    PRIMARY KEY (session_id, position)
);
"""


class ZcodeFixture:
    def __init__(self, tmp, now):
        self.root = Path(tmp)
        self.now = now
        (self.root / "cli" / "db").mkdir(parents=True)
        self.con = sqlite3.connect(self.root / "cli" / "db" / "db.sqlite")
        self.con.executescript(SCHEMA)

    def add(self, sid, age_ms, archived=False, directory="D:/work/delta"):
        upd = int((self.now - age_ms / 1000.0) * 1000)
        self.con.execute(
            "INSERT INTO session (id, directory, title, time_created, time_updated, time_archived)"
            " VALUES (?,?,?,?,?,?)",
            (sid, directory, "t-" + sid, upd - 100000, upd, 1 if archived else None),
        )
        self.con.commit()

    def add_todo(self, sid, position, status):
        self.con.execute(
            "INSERT INTO todo (session_id, content, status, position) VALUES (?,?,?,?)",
            (sid, "todo", status, position),
        )
        self.con.commit()

    def collect(self):
        return ag.collect_sessions({"zcode": self.root}, self.now)


class ZcodeScannerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.fx = ZcodeFixture(self._tmp.name, self.now)

    def tearDown(self):
        self.fx.con.close()
        self._tmp.cleanup()

    def test_recent_session_is_run_with_project(self):
        self.fx.add("sess_1", 30_000)
        (s,) = self.fx.collect()
        self.assertEqual(s["tool"], "zcode")
        self.assertEqual(s["id"], "sess_1")
        self.assertEqual(s["state"], "RUN")
        self.assertEqual(s["project"], "delta")
        self.assertEqual(s["age"], 30)

    def test_old_session_is_done(self):
        self.fx.add("sess_1", 300_000)
        self.assertEqual(self.fx.collect()[0]["state"], "DONE")

    def test_todo_counts(self):
        self.fx.add("sess_1", 30_000)
        self.fx.add_todo("sess_1", 1, "completed")
        self.fx.add_todo("sess_1", 2, "completed")
        self.fx.add_todo("sess_1", 3, "in_progress")
        self.fx.add_todo("sess_1", 4, "pending")
        (s,) = self.fx.collect()
        self.assertEqual((s["tasks_done"], s["tasks_total"]), (2, 4))

    def test_session_without_todos_is_zero(self):
        self.fx.add("sess_1", 30_000)
        (s,) = self.fx.collect()
        self.assertEqual((s["tasks_done"], s["tasks_total"]), (0, 0))

    def test_archived_excluded(self):
        self.fx.add("sess_1", 30_000, archived=True)
        self.assertEqual(self.fx.collect(), [])

    def test_beyond_active_window_excluded(self):
        self.fx.add("sess_1", 601_000)
        self.assertEqual(self.fx.collect(), [])

    def test_subagent_sessions_excluded(self):
        self.fx.add("sess_subagent_agent_1", 30_000)
        self.fx.add("sess_main", 40_000)
        sessions = self.fx.collect()
        self.assertEqual([s["id"] for s in sessions], ["sess_main"])

    def test_missing_db_skips_tool(self):
        empty = Path(self._tmp.name) / "nope"
        empty.mkdir()
        self.assertEqual(ag.collect_sessions({"zcode": empty}, self.now), [])

    def test_corrupt_db_skips_tool(self):
        (self.fx.root / "cli" / "db" / "db.sqlite").write_bytes(b"not a sqlite file at all")
        self.assertEqual(ag.collect_sessions({"zcode": self.fx.root}, self.now), [])

    def test_sorted_with_other_tools(self):
        self.fx.add("sess_old", 300_000)
        self.fx.add("sess_new", 10_000)
        ids = [s["id"] for s in self.fx.collect()]
        self.assertEqual(ids, ["sess_new", "sess_old"])


if __name__ == "__main__":
    unittest.main()
