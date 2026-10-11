"""kimi code 扫描器接缝测试：临时目录造 sessions/wd_*/<sid>/ 形态。"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import agent_sessions as ag


class KimiCodeFixture:
    def __init__(self, tmp, now):
        self.root = Path(tmp)
        self.now = now

    def add_session(self, sid, age, work_dir="D:/work/eps", title="sess title", wire_age=None):
        d = self.root / "sessions" / "wd_an-w_hash" / sid
        (d / "agents" / "main").mkdir(parents=True)
        state = {
            "createdAt": "2026-01-01T00:00:00.000Z",
            "updatedAt": "2026-01-01T00:00:00.000Z",
            "title": title,
            "workDir": work_dir,
        }
        sf = d / "state.json"
        sf.write_text(json.dumps(state), encoding="utf-8")
        os.utime(sf, (self.now - age, self.now - age))
        wf = d / "agents" / "main" / "wire.jsonl"
        wa = wire_age if wire_age is not None else age
        wf.write_text('{"type":"config.update"}\n', encoding="utf-8")
        os.utime(wf, (self.now - wa, self.now - wa))
        return d

    def collect(self):
        return ag.collect_sessions({"kimicode": self.root}, self.now)


class KimiCodeScannerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.fx = KimiCodeFixture(self._tmp.name, self.now)

    def tearDown(self):
        self._tmp.cleanup()

    def test_recent_session_is_run_with_project(self):
        self.fx.add_session("session_a", 20)
        (s,) = self.fx.collect()
        self.assertEqual(s["tool"], "kimicode")
        self.assertEqual(s["id"], "session_a")
        self.assertEqual(s["state"], "RUN")
        self.assertEqual(s["project"], "eps")
        self.assertEqual(s["age"], 20)

    def test_wire_newer_than_state_drives_age(self):
        self.fx.add_session("session_a", 500, wire_age=30)
        (s,) = self.fx.collect()
        self.assertEqual(s["state"], "RUN")
        self.assertEqual(s["age"], 30)

    def test_old_session_is_done(self):
        self.fx.add_session("session_a", 300)
        (s,) = self.fx.collect()
        self.assertEqual(s["state"], "DONE")
        self.assertFalse(s["running"])

    def test_tasks_are_null(self):
        self.fx.add_session("session_a", 20)
        (s,) = self.fx.collect()
        self.assertIsNone(s["tasks_done"])
        self.assertIsNone(s["tasks_total"])

    def test_beyond_active_window_excluded(self):
        self.fx.add_session("session_a", 601)
        self.assertEqual(self.fx.collect(), [])

    def test_broken_state_json_skips_session_not_tool(self):
        d = self.fx.add_session("session_good", 20)
        bad = self.fx.root / "sessions" / "wd_x" / "session_bad"
        (bad / "agents" / "main").mkdir(parents=True)
        (bad / "state.json").write_text("{ nope", encoding="utf-8")
        (bad / "agents" / "main" / "wire.jsonl").write_text("", encoding="utf-8")
        sessions = self.fx.collect()
        self.assertEqual([s["id"] for s in sessions], ["session_good"])
        self.assertTrue(d.is_dir())

    def test_missing_sessions_dir_yields_empty(self):
        empty = Path(self._tmp.name) / "nope"
        empty.mkdir()
        self.assertEqual(ag.collect_sessions({"kimicode": empty}, self.now), [])


if __name__ == "__main__":
    unittest.main()
