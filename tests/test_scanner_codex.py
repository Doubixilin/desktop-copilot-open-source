"""codex 扫描器接缝测试：临时目录造 sessions/YYYY/MM/DD/rollout-*.jsonl 形态。"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import agent_sessions as ag

UUID = "01a0768a-9dc6-7d42-b53d-01ed8393c1ad"


def _meta_line(thread_source="user", sid=UUID, cwd="D:\\work\\eps"):
    return json.dumps({
        "timestamp": "2026-09-25T00:00:00.000Z", "ordinal": 0, "type": "session_meta",
        "payload": {"session_id": sid, "id": sid, "cwd": cwd, "thread_source": thread_source},
    }, ensure_ascii=False, separators=(",", ":"))


def _msg_line(role, text):
    return json.dumps({
        "timestamp": "2026-09-25T00:00:00.000Z", "ordinal": 1, "type": "response_item",
        "payload": {"type": "message", "id": "m1", "role": role,
                    "content": [{"type": "output_text" if role == "assistant" else "input_text", "text": text}]},
    }, ensure_ascii=False)


def _tool_line(name="shell"):
    return json.dumps({
        "timestamp": "2026-09-25T00:00:00.000Z", "ordinal": 2, "type": "response_item",
        "payload": {"type": "custom_tool_call", "id": "c1", "call_id": "call_1", "name": name,
                    "input": "{\"cmd\":[\"ls\"]}"},
    }, ensure_ascii=False)


def _token_count_line(total=(5021911, 2915072, 733, 54, 5022644)):
    inp, cached, out, reasoning, tot = total
    return json.dumps({
        "timestamp": "2026-09-25T00:00:00.000Z", "ordinal": 3, "type": "event_msg",
        "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": inp, "cached_input_tokens": cached,
                                  "cache_write_input_tokens": 0, "output_tokens": out,
                                  "reasoning_output_tokens": reasoning, "total_tokens": tot},
            "model_context_window": 258400}},
    })


class CodexFixture:
    def __init__(self, tmp, now):
        self.root = Path(tmp)
        self.now = now

    def add_session(self, name, age, lines, thread_source="user", rel="2026/09/25"):
        day = self.root / "sessions" / rel
        day.mkdir(parents=True, exist_ok=True)
        jf = day / f"{name}.jsonl"
        lines = list(lines)
        if thread_source is not None:
            lines.insert(0, _meta_line(thread_source))
        jf.write_text("\n".join(lines) + "\n", encoding="utf-8")
        t = self.now - age
        os.utime(jf, (t, t))
        return jf

    def collect(self):
        return ag.collect_sessions({"codex": self.root}, self.now)


class CodexScannerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.fx = CodexFixture(self._tmp.name, self.now)

    def tearDown(self):
        self._tmp.cleanup()

    def test_fresh_session_run_with_meta_fields(self):
        self.fx.add_session(f"rollout-2026-09-25T10-00-00-{UUID}", 30,
                            [_msg_line("assistant", "done"), _token_count_line()])
        (s,) = self.fx.collect()
        self.assertEqual(s["tool"], "codex")
        self.assertEqual(s["id"], UUID)
        self.assertEqual(s["state"], "RUN")
        self.assertEqual(s["project"], "eps")
        self.assertTrue(s["running"])

    def test_tokens_from_last_token_count(self):
        self.fx.add_session("rollout-x" + "0" * 28 + "-" + UUID, 30,
                            [_token_count_line((1, 0, 2, 3, 6)),
                             _token_count_line((10, 5, 20, 3, 33))])
        (s,) = self.fx.collect()
        self.assertEqual(s["tokens"], {"input": 10, "cached_input": 5, "output": 20,
                                       "reasoning": 3, "total": 33})

    def test_old_assistant_tail_done(self):
        self.fx.add_session("rollout-x-" + UUID, 300, [_msg_line("assistant", "done")])
        self.assertEqual(self.fx.collect()[0]["state"], "DONE")

    def test_user_tail_idle(self):
        self.fx.add_session("rollout-x-" + UUID, 300, [_msg_line("user", "hi")])
        self.assertEqual(self.fx.collect()[0]["state"], "IDLE")

    def test_tool_call_tail_confirm(self):
        self.fx.add_session("rollout-x-" + UUID, 300,
                            [_msg_line("assistant", ".."), _tool_line()])
        self.assertEqual(self.fx.collect()[0]["state"], "CONFIRM")

    def test_guardian_review_excluded(self):
        self.fx.add_session("rollout-guard-" + UUID, 30,
                            [_msg_line("assistant", "review")], thread_source="guardian_review")
        self.assertEqual(self.fx.collect(), [])

    def test_missing_thread_source_treated_as_user(self):
        self.fx.add_session("rollout-legacy-" + UUID, 30,
                            [_msg_line("assistant", "ok")], thread_source=None)
        self.assertEqual(len(self.fx.collect()), 1)

    def test_beyond_active_window_excluded(self):
        self.fx.add_session("rollout-old-" + UUID, 601, [_msg_line("assistant", "ok")])
        self.assertEqual(self.fx.collect(), [])

    def test_corrupt_file_yields_row_without_tokens(self):
        d = self.fx.root / "sessions" / "2026" / "09" / "25"
        d.mkdir(parents=True, exist_ok=True)
        jf = d / f"rollout-bad-{UUID}.jsonl"
        jf.write_bytes(b"\xff\xfe not json at all")
        t = self.now - 5
        os.utime(jf, (t, t))
        (s,) = self.fx.collect()
        self.assertEqual(s["tool"], "codex")
        self.assertIsNone(s["tokens"])
        self.assertEqual(s["state"], "RUN")

    def test_developer_role_ignored_for_state(self):
        self.fx.add_session("rollout-dev-" + UUID, 300,
                            [_msg_line("assistant", "done"), _msg_line("developer", "instr")])
        self.assertEqual(self.fx.collect()[0]["state"], "DONE")

    def test_missing_root_yields_empty(self):
        self.assertEqual(ag.collect_sessions(
            {"codex": Path(self._tmp.name) / "nope"}, self.now), [])


if __name__ == "__main__":
    unittest.main()
