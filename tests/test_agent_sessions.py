"""agent_sessions 接缝测试：给定 fixture 数据根与固定 now，断言会话列表外部行为。"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import agent_sessions as ag


def _rec(role, kind="text", cwd=None):
    """构造 jsonl 记录：kind ∈ text/tool/user。"""
    if role == "user":
        rec = {"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}
    elif kind == "tool":
        rec = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}
    else:
        rec = {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}
    if cwd:
        rec["cwd"] = cwd
    return rec


class QoderRootFixture:
    """在临时目录里造一个 .qoder-cn 形态的数据根。"""

    def __init__(self, tmp):
        self.root = Path(tmp)

    def add_session(self, sid, age, records, proj_dir="proj-a", cwd=None):
        d = self.root / "projects" / proj_dir
        d.mkdir(parents=True, exist_ok=True)
        jf = d / f"{sid}.jsonl"
        lines = [json.dumps(r, ensure_ascii=False) for r in records]
        jf.write_text("\n".join(lines) + "\n", encoding="utf-8")
        t = self.now - age
        os.utime(jf, (t, t))
        return jf

    def add_task(self, sid, task_id, status, subject="task"):
        d = self.root / "tasks" / sid
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{task_id}.json").write_text(
            json.dumps({"id": task_id, "subject": subject, "status": status}), encoding="utf-8"
        )


class SeamTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.fx = QoderRootFixture(self._tmp.name)
        self.fx.now = self.now

    def tearDown(self):
        self._tmp.cleanup()

    def collect(self, roots=None):
        if roots is None:
            roots = {"qoder": self.fx.root}
        return ag.collect_sessions(roots, self.now)

    # ---- 基本映射 ----

    def test_fresh_session_is_run_with_tool_field(self):
        self.fx.add_session("s1", 5, [_rec("assistant", cwd="D:/work/alpha")])
        (s,) = self.collect()
        self.assertEqual(s["tool"], "qoder")
        self.assertEqual(s["id"], "s1")
        self.assertEqual(s["state"], "RUN")
        self.assertTrue(s["running"])
        self.assertEqual(s["project"], "alpha")
        self.assertEqual(s["age"], 5)

    def test_project_falls_back_to_parent_dir_without_cwd(self):
        self.fx.add_session("s1", 5, [_rec("assistant")], proj_dir="myproj")
        (s,) = self.collect()
        self.assertEqual(s["project"], "myproj")

    # ---- 四态判定 ----

    def test_tool_use_tail_is_confirm_outside_running_window(self):
        self.fx.add_session("s1", 120, [_rec("assistant", "tool")])
        (s,) = self.collect()
        self.assertEqual(s["state"], "CONFIRM")
        self.assertFalse(s["running"])

    def test_assistant_text_tail_is_done(self):
        self.fx.add_session("s1", 120, [_rec("assistant", "text")])
        self.assertEqual(self.collect()[0]["state"], "DONE")

    def test_user_tail_is_idle(self):
        self.fx.add_session("s1", 120, [_rec("user")])
        self.assertEqual(self.collect()[0]["state"], "IDLE")

    def test_recent_wins_over_tail_kind(self):
        # 90 秒内即使最后动作是 tool_use 也算 RUN
        self.fx.add_session("s1", 30, [_rec("assistant", "tool")])
        self.assertEqual(self.collect()[0]["state"], "RUN")

    # ---- 时间窗边界 ----

    def test_running_window_boundary(self):
        jf = self.fx.add_session("s1", 90, [_rec("assistant", "text")])
        now = jf.stat().st_mtime + ag.RUNNING_WINDOW
        (s,) = ag.collect_sessions({"qoder": self.fx.root}, now)
        self.assertEqual(s["state"], "RUN")

    def test_active_window_boundary_included(self):
        jf = self.fx.add_session("s1", 600, [_rec("assistant", "text")])
        now = jf.stat().st_mtime + ag.ACTIVE_WINDOW
        sessions = ag.collect_sessions({"qoder": self.fx.root}, now)
        self.assertEqual(len(sessions), 1)

    def test_beyond_active_window_excluded(self):
        self.fx.add_session("s1", 601, [_rec("assistant", "text")])
        self.assertEqual(self.collect(), [])

    # ---- 任务进度 ----

    def test_task_stats(self):
        self.fx.add_session("s1", 5, [_rec("assistant")])
        self.fx.add_task("s1", "t1", "completed")
        self.fx.add_task("s1", "t2", "in_progress")
        self.fx.add_task("s1", "t3", "pending")
        (s,) = self.collect()
        self.assertEqual((s["tasks_done"], s["tasks_total"]), (1, 3))

    def test_no_tasks_dir_is_zero(self):
        self.fx.add_session("s1", 5, [_rec("assistant")])
        (s,) = self.collect()
        self.assertEqual((s["tasks_done"], s["tasks_total"]), (0, 0))

    # ---- 排序与多会话 ----

    def test_sorted_by_age(self):
        self.fx.add_session("old", 300, [_rec("assistant")])
        self.fx.add_session("new", 10, [_rec("assistant")])
        ids = [s["id"] for s in self.collect()]
        self.assertEqual(ids, ["new", "old"])

    # ---- roots 注入与容错 ----

    def test_missing_root_yields_empty(self):
        roots = {"qoder": Path(self._tmp.name) / "nonexistent"}
        self.assertEqual(self.collect(roots), [])

    def test_unknown_tool_key_ignored(self):
        self.fx.add_session("s1", 5, [_rec("assistant")])
        roots = {"qoder": self.fx.root, "nosuchtool": Path(self._tmp.name)}
        sessions = self.collect(roots)
        self.assertEqual([s["tool"] for s in sessions], ["qoder"])

    def test_failing_scanner_skipped_others_survive(self):
        self.fx.add_session("s1", 5, [_rec("assistant")])

        def boom(root, now):
            raise RuntimeError("locked")

        original = dict(ag.SCANNERS)
        ag.SCANNERS["broken"] = boom
        try:
            roots = {"broken": Path(self._tmp.name), "qoder": self.fx.root}
            sessions = self.collect(roots)
        finally:
            ag.SCANNERS.clear()
            ag.SCANNERS.update(original)
        self.assertEqual([s["tool"] for s in sessions], ["qoder"])

    def test_garbage_jsonl_does_not_crash(self):
        d = self.fx.root / "projects" / "p"
        d.mkdir(parents=True)
        jf = d / "bad.jsonl"
        jf.write_bytes(b"\xff\xfe not json at all")
        t = self.now - 5
        os.utime(jf, (t, t))
        (s,) = self.collect()
        self.assertEqual(s["id"], "bad")
        self.assertEqual(s["project"], "p")


if __name__ == "__main__":
    unittest.main()
