"""DSH 扫描器接缝测试：临时目录造 sessions/<slug>/session-<uuid>/session.v3.jsonl.zstd。"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import agent_sessions as ag

try:
    import zstandard
except ImportError:
    zstandard = None

SID = "session-6d027840-8c31-481a-9141-90128ee0eb96"


def _line(rec):
    return json.dumps(rec, ensure_ascii=False)


def _assistant(text, tool=False):
    blocks = [{"type": "tool_call", "name": "shell"}] if tool else [
        {"type": "reasoning", "text": "thinking..."}, {"type": "text", "text": text}]
    return {"type": "assistant/message", "seq": 2, "time": 1790318547338,
            "data": {"turn": 1, "step": 1, "message": {"role": "assistant", "content": blocks}}}


def _user(text):
    return {"type": "user/message", "seq": 1, "time": 1790318541200,
            "data": {"content": [{"type": "text", "text": text}],
                     "source": {"kind": "user"}}}


def _meta(sid=SID, cwd="D:\\work\\eps", depth=0):
    return {"type": "session", "version": 3, "id": sid, "createdAt": 1790318479142,
            "cwd": cwd, "delegationDepth": depth, "agentPreset": "standard"}


@unittest.skipIf(zstandard is None, "zstandard 未安装（生产环境亦静默跳过）")
class DshFixture:
    def __init__(self, tmp, now):
        self.root = Path(tmp)
        self.now = now
        self._dctx = zstandard.ZstdDecompressor()

    def add_session(self, sid, age, records, slug="--D-work-eps--", depth=0):
        d = self.root / "sessions" / slug / sid
        d.mkdir(parents=True, exist_ok=True)
        payload = "\n".join(_line(r) for r in [_meta(sid=sid, depth=depth)] + records) + "\n"
        zf = d / "session.v3.jsonl.zstd"
        with open(zf, "wb") as fh:
            cctx = zstandard.ZstdCompressor()
            fh.write(cctx.compress(payload.encode("utf-8")))
        t = self.now - age
        os.utime(zf, (t, t))
        return zf

    def collect(self):
        return ag.collect_sessions({"dsh": self.root}, self.now)


@unittest.skipIf(zstandard is None, "zstandard 未安装")
class DshScannerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.fx = DshFixture(self._tmp.name, self.now)

    def tearDown(self):
        self._tmp.cleanup()

    def test_fresh_session_run_with_project(self):
        self.fx.add_session(SID, 30, [_user("hi"), _assistant("done")])
        (s,) = self.fx.collect()
        self.assertEqual(s["tool"], "dsh")
        self.assertEqual(s["id"], SID)
        self.assertEqual(s["state"], "RUN")
        self.assertEqual(s["project"], "eps")
        self.assertTrue(s["running"])

    def test_preview_from_last_text(self):
        self.fx.add_session(SID, 30, [_assistant("旧的"), _user("帮我评估")])
        (s,) = self.fx.collect()
        self.assertEqual(s["preview"], "帮我评估")
        self.assertEqual(s["preview_role"], "user")

    def test_reasoning_block_not_used_for_preview(self):
        self.fx.add_session(SID, 30, [_assistant("结论文字")])
        (s,) = self.fx.collect()
        self.assertEqual(s["preview"], "结论文字")

    def test_tool_call_tail_confirm(self):
        tool_rec = {"type": "tool/call", "seq": 3, "time": 1790318600000,
                    "data": {"tool": "shell", "args": {}}}
        self.fx.add_session(SID, 300, [_user("hi"), _assistant("ok"), tool_rec])
        self.assertEqual(self.fx.collect()[0]["state"], "CONFIRM")

    def test_old_assistant_done_user_idle(self):
        self.fx.add_session("session-a", 300, [_assistant("ok")])
        self.assertEqual(self.fx.collect()[0]["state"], "DONE")
        self.fx.add_session("session-b", 300, [_user("hi")])
        self.assertEqual([s["state"] for s in self.fx.collect()].count("IDLE"), 1)

    def test_delegation_depth_excluded(self):
        self.fx.add_session("session-sub", 30, [_assistant("x")], depth=2)
        self.assertEqual(self.fx.collect(), [])

    def test_project_falls_back_to_slug(self):
        # 首 line 解析失败时 project 取 workspace slug 的尾段
        d = self.fx.root / "sessions" / "--D-work-gamma--" / "session-raw"
        d.mkdir(parents=True)
        zf = d / "session.v3.jsonl.zstd"
        zf.write_bytes(zstandard.ZstdCompressor().compress(b"not json\n"))
        t = self.now - 5
        os.utime(zf, (t, t))
        (s,) = self.fx.collect()
        self.assertEqual(s["project"], "gamma")

    def test_beyond_active_window_excluded(self):
        self.fx.add_session("session-old", 601, [_assistant("ok")])
        self.assertEqual(self.fx.collect(), [])

    def test_oversized_file_bounded_decompress(self):
        # 解压上限收窄后：头部元数据仍可读（project 正常），不整读不崩溃
        self.fx.add_session(SID, 30, [_user("x" * 4096)] * 20)
        orig = ag._DSH_MAX_BYTES
        ag._DSH_MAX_BYTES = 2048
        try:
            (s,) = self.fx.collect()
        finally:
            ag._DSH_MAX_BYTES = orig
        self.assertEqual(s["id"], SID)
        self.assertEqual(s["project"], "eps")

    def test_missing_root_empty(self):
        self.assertEqual(ag.collect_sessions(
            {"dsh": Path(self._tmp.name) / "nope"}, self.now), [])


if __name__ == "__main__":
    unittest.main()
