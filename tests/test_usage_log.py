import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import usage_log as ul


class DetectStartsTest(unittest.TestCase):
    def test_new_pid_is_a_start(self):
        self.assertEqual(ul.detect_starts({1: "a.exe"}, {1: "a.exe", 2: "b.exe"}), ["b.exe"])

    def test_restart_with_new_pid_is_a_start(self):
        self.assertEqual(ul.detect_starts({1: "a.exe"}, {2: "a.exe"}), ["a.exe"])

    def test_second_concurrent_instance_is_a_start(self):
        self.assertEqual(ul.detect_starts({1: "a.exe"}, {1: "a.exe", 2: "a.exe"}), ["a.exe"])

    def test_exit_is_not_a_start(self):
        self.assertEqual(ul.detect_starts({1: "a.exe", 2: "b.exe"}, {1: "a.exe"}), [])

    def test_empty_to_empty(self):
        self.assertEqual(ul.detect_starts({}, {}), [])


class DetectFocusTest(unittest.TestCase):
    def test_switch_reports_new_focus(self):
        self.assertEqual(ul.detect_focus("a.exe", "b.exe"), "b.exe")

    def test_same_focus_reports_nothing(self):
        self.assertIsNone(ul.detect_focus("a.exe", "a.exe"))

    def test_none_foreground_reports_nothing(self):
        self.assertIsNone(ul.detect_focus("a.exe", None))


class AppendAndPruneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_record_has_exactly_ts_and_exe(self):
        ul.append(self.dir, "start", r"C:\p\a.exe", datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc))
        files = list(self.dir.glob("start-*.jsonl"))
        self.assertEqual(len(files), 1)
        record = json.loads(files[0].read_text(encoding="utf-8").strip())
        self.assertEqual(set(record), {"ts", "exe"})
        self.assertEqual(record["exe"], r"C:\p\a.exe")

    def test_focus_and_start_go_to_separate_files(self):
        ts = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
        ul.append(self.dir, "start", "a.exe", ts)
        ul.append(self.dir, "focus", "a.exe", ts)
        self.assertEqual(len(list(self.dir.glob("start-*.jsonl"))), 1)
        self.assertEqual(len(list(self.dir.glob("focus-*.jsonl"))), 1)

    def test_prune_deletes_only_old_days(self):
        now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
        old = now - timedelta(days=91)
        recent = now - timedelta(days=89)
        ul.append(self.dir, "start", "old.exe", old)
        ul.append(self.dir, "start", "new.exe", recent)
        ul.prune(self.dir, days=90, now=now)
        remaining = [json.loads(l)["exe"] for f in self.dir.glob("start-*.jsonl") for l in f.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(remaining, ["new.exe"])

    def test_append_failure_is_swallowed(self):
        blocker = self.dir / "blockfile"
        blocker.write_text("x", encoding="utf-8")
        # 目录位置被普通文件占用时 mkdir/open 必失败：不得抛错，服务不能因日志死掉
        ul.append(blocker / "sub", "start", "a.exe", datetime.now(timezone.utc))
        ul._append_failures = 0

    def test_append_failure_is_logged_throttled(self):
        import contextlib
        import io
        blocker = self.dir / "blockfile"
        blocker.write_text("x", encoding="utf-8")
        ul._append_failures = 0
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            for _ in range(2):
                ul.append(blocker / "sub", "start", "a.exe", datetime.now(timezone.utc))
        out = buf.getvalue()
        self.assertIn("usage-log: 写盘失败", out)      # 静默失败必须留痕
        self.assertEqual(out.count("写盘失败"), 1)      # 节流：第 2 次不重复打
        ul._append_failures = 0


class CollectorFilesTest(unittest.TestCase):
    """隐私断言要盯 Collector 真正产出的文件，而不只是 append 的单条记录。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self._real_exes = ul.running_pid_exes
        self._real_fg = ul.foreground_exe
        ul.running_pid_exes = lambda: {1: r"C:\p\a.exe", 2: r"C:\p\b.exe"}
        ul.foreground_exe = lambda: r"C:\p\a.exe"
        self.addCleanup(setattr, ul, "running_pid_exes", self._real_exes)
        self.addCleanup(setattr, ul, "foreground_exe", self._real_fg)

    def test_collector_files_contain_only_ts_and_exe(self):
        ts = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
        collector = ul.Collector(self.dir)
        collector.collect(now=ts)                      # 首轮只建基线
        ul.running_pid_exes = lambda: {1: r"C:\p\a.exe", 2: r"C:\p\b.exe", 3: r"C:\p\c.exe"}
        ul.foreground_exe = lambda: r"C:\p\c.exe"
        collector.collect(now=ts)
        records = []
        for path in self.dir.glob("*.jsonl"):
            records += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(records)
        for record in records:
            self.assertEqual(set(record), {"ts", "exe"})


class PrivacyGuardTest(unittest.TestCase):
    def test_no_module_in_the_repo_reads_window_titles(self):
        repo = Path(ul.__file__).resolve().parent
        for path in repo.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("GetWindowText", source, path.name)
            self.assertNotIn("window_title", source, path.name)
            self.assertNotIn("WindowTitle", source, path.name)


if __name__ == "__main__":
    unittest.main()
