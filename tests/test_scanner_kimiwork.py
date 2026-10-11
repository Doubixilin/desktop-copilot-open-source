"""kimi work 扫描器接缝测试：状态 map + 上下文用量文件，降级展示。"""
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

import agent_sessions as ag

KEY = "agent:main:main:conversation:{}"


def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class KimiWorkFixture:
    def __init__(self, tmp, now):
        self.root = Path(tmp)
        self.now = now
        self.statuses = {}
        self.usage = {}

    def add(self, cid, age, status="completed"):
        self.statuses[KEY.format(cid)] = status
        self.usage[KEY.format(cid)] = {"contextUsage": 0.1, "updatedAt": iso(self.now - age)}

    def write(self):
        (self.root / "conversation-statuses.json").write_text(
            json.dumps(self.statuses), encoding="utf-8"
        )
        (self.root / "conversation-context-usage.json").write_text(
            json.dumps(self.usage), encoding="utf-8"
        )

    def collect(self):
        self.write()
        return ag.collect_sessions({"kimiwork": self.root}, self.now)


class KimiWorkScannerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.fx = KimiWorkFixture(self._tmp.name, self.now)

    def tearDown(self):
        self._tmp.cleanup()

    def test_completed_recent_is_run_and_degraded(self):
        self.fx.add("c1", 30)
        (s,) = self.fx.collect()
        self.assertEqual(s["tool"], "kimiwork")
        self.assertEqual(s["id"], "c1")
        self.assertEqual(s["state"], "RUN")
        self.assertEqual(s["project"], "")
        self.assertIsNone(s["tasks_done"])

    def test_completed_old_is_done(self):
        self.fx.add("c1", 300)
        self.assertEqual(self.fx.collect()[0]["state"], "DONE")

    def test_unknown_status_is_idle(self):
        self.fx.add("c1", 300, status="weird-future-value")
        self.assertEqual(self.fx.collect()[0]["state"], "IDLE")

    def test_status_without_usage_excluded(self):
        self.fx.add("c1", 30)
        del self.fx.usage[KEY.format("c1")]
        self.assertEqual(self.fx.collect(), [])

    def test_usage_without_status_excluded(self):
        self.fx.add("c1", 30)
        del self.fx.statuses[KEY.format("c1")]
        self.assertEqual(self.fx.collect(), [])

    def test_beyond_active_window_excluded(self):
        self.fx.add("c1", 601)
        self.assertEqual(self.fx.collect(), [])

    def test_missing_files_skips_tool(self):
        empty = Path(self._tmp.name) / "nope"
        empty.mkdir()
        self.assertEqual(ag.collect_sessions({"kimiwork": empty}, self.now), [])

    def test_broken_usage_json_skips_tool(self):
        self.fx.add("c1", 30)
        self.fx.write()
        (self.fx.root / "conversation-context-usage.json").write_text("{ bad", encoding="utf-8")
        self.assertEqual(ag.collect_sessions({"kimiwork": self.fx.root}, self.now), [])


if __name__ == "__main__":
    unittest.main()
