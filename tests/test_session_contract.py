"""/deck 会话项扩展契约测试：title / current_task / tasks / preview / preview_role。

聚焦会话详情与行内预览（qoder-deck spec 故事 3/4/5 的服务端支撑），
多工具会话 spec 的既有字段不动，全部为加法。
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import agent_sessions as ag


def _assistant_text(text, cwd=None):
    rec = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    if cwd:
        rec["cwd"] = cwd
    return rec


def _assistant_tool(name):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name}]}}


def _user_text(text):
    return {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}


class QoderContractFixture:
    def __init__(self, tmp, now):
        self.root = Path(tmp)
        self.now = now

    def add_session(self, sid, age, records, proj_dir="proj-a"):
        d = self.root / "projects" / proj_dir
        d.mkdir(parents=True, exist_ok=True)
        jf = d / f"{sid}.jsonl"
        jf.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8"
        )
        t = self.now - age
        os.utime(jf, (t, t))
        return jf

    def add_task(self, sid, task_id, status, subject):
        d = self.root / "tasks" / sid
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{task_id}.json").write_text(
            json.dumps({"id": task_id, "subject": subject, "status": status}), encoding="utf-8"
        )

    def collect(self):
        return ag.collect_sessions({"qoder": self.root}, self.now)


class PreviewTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.fx = QoderContractFixture(self._tmp.name, self.now)

    def tearDown(self):
        self._tmp.cleanup()

    def test_assistant_text_preview(self):
        self.fx.add_session("s1", 30, [_assistant_text("部署完成", cwd="D:/work/alpha")])
        (s,) = self.fx.collect()
        self.assertEqual(s["preview"], "部署完成")
        self.assertEqual(s["preview_role"], "assistant")

    def test_tool_use_falls_back_to_tool_name(self):
        self.fx.add_session("s1", 30, [_assistant_tool("Bash")])
        (s,) = self.fx.collect()
        self.assertEqual(s["preview"], "tool:Bash")
        self.assertEqual(s["preview_role"], "assistant")

    def test_text_block_wins_over_earlier_tool_use(self):
        rec = {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Read"}, {"type": "text", "text": "看完了"}]},
        }
        self.fx.add_session("s1", 30, [rec])
        (s,) = self.fx.collect()
        self.assertEqual(s["preview"], "看完了")

    def test_latest_record_wins(self):
        self.fx.add_session("s1", 30, [_assistant_text("旧的"), _user_text("跑一下测试")])
        (s,) = self.fx.collect()
        self.assertEqual(s["preview"], "跑一下测试")
        self.assertEqual(s["preview_role"], "user")

    def test_user_record_preview(self):
        self.fx.add_session("s1", 30, [_user_text("帮我修这个 bug")])
        (s,) = self.fx.collect()
        self.assertEqual(s["preview"], "帮我修这个 bug")
        self.assertEqual(s["preview_role"], "user")

    def test_long_preview_truncated_with_ellipsis(self):
        self.fx.add_session("s1", 30, [_assistant_text("字" * 250)])
        (s,) = self.fx.collect()
        self.assertEqual(len(s["preview"]), ag.PREVIEW_MAX + 1)
        self.assertTrue(s["preview"].endswith("…"))

    def test_empty_text_blocks_skipped(self):
        rec = {"type": "assistant", "message": {"content": [{"type": "text", "text": "  "}]}}
        self.fx.add_session("s1", 30, [rec])
        (s,) = self.fx.collect()
        self.assertIsNone(s["preview"])
        self.assertIsNone(s["preview_role"])

    def test_garbage_tail_yields_no_preview(self):
        d = self.fx.root / "projects" / "p"
        d.mkdir(parents=True)
        jf = d / "bad.jsonl"
        jf.write_bytes(b"\xff\xfe not json")
        t = self.now - 5
        os.utime(jf, (t, t))
        (s,) = self.fx.collect()
        self.assertIsNone(s["preview"])


class QoderTasksTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.fx = QoderContractFixture(self._tmp.name, self.now)

    def tearDown(self):
        self._tmp.cleanup()

    def test_current_task_is_first_in_progress(self):
        self.fx.add_session("s1", 30, [_assistant_text("ok")])
        self.fx.add_task("s1", "t1", "completed", "建脚手架")
        self.fx.add_task("s1", "t2", "in_progress", "写测试")
        (s,) = self.fx.collect()
        self.assertEqual(s["current_task"], "写测试")

    def test_tasks_list_subjects_and_status(self):
        self.fx.add_session("s1", 30, [_assistant_text("ok")])
        self.fx.add_task("s1", "t1", "completed", "建脚手架")
        self.fx.add_task("s1", "t2", "in_progress", "写测试")
        (s,) = self.fx.collect()
        subjects = [(t["subject"], t["status"]) for t in s["tasks"]]
        self.assertIn(("建脚手架", "completed"), subjects)
        self.assertIn(("写测试", "in_progress"), subjects)

    def test_no_tasks_dir_yields_none(self):
        self.fx.add_session("s1", 30, [_assistant_text("ok")])
        (s,) = self.fx.collect()
        self.assertIsNone(s["tasks"])
        self.assertIsNone(s["current_task"])


class KimiCodeContractTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _add(self, sid, age, state_title, wire_lines):
        d = self.root / "sessions" / "wd_hash" / sid
        (d / "agents" / "main").mkdir(parents=True)
        sf = d / "state.json"
        sf.write_text(json.dumps({"title": state_title, "workDir": "D:/work/eps"}), encoding="utf-8")
        t = self.now - age
        os.utime(sf, (t, t))
        wf = d / "agents" / "main" / "wire.jsonl"
        wf.write_text("\n".join(wire_lines) + "\n", encoding="utf-8")
        os.utime(wf, (t, t))

    def test_title_from_state_json(self):
        self._add("s1", 30, "重构登录模块", ['{"type":"config.update"}'])
        (s,) = ag.collect_sessions({"kimicode": self.root}, self.now)
        self.assertEqual(s["title"], "重构登录模块")

    def test_preview_from_compatible_wire_format(self):
        self._add(
            "s1",
            30,
            "t",
            [json.dumps(_assistant_text("编译过了"), ensure_ascii=False)],
        )
        (s,) = ag.collect_sessions({"kimicode": self.root}, self.now)
        self.assertEqual(s["preview"], "编译过了")
        self.assertEqual(s["preview_role"], "assistant")

    def test_incompatible_wire_format_degrades_to_no_preview(self):
        self._add("s1", 30, "t", ['{"event":"snapshot","payload":{"x":1}}'])
        (s,) = ag.collect_sessions({"kimicode": self.root}, self.now)
        self.assertIsNone(s["preview"])
        self.assertEqual(s["state"], "RUN")

    def test_garbage_wire_lines_skipped(self):
        self._add(
            "s1",
            30,
            "t",
            ["not json at all", json.dumps(_user_text("再来一轮"))],
        )
        (s,) = ag.collect_sessions({"kimicode": self.root}, self.now)
        self.assertEqual(s["preview"], "再来一轮")


class TitleFromDbTest(unittest.TestCase):
    """zcode / hermes 的 title 列接回（此前被扫描器忽略）。"""

    def test_zcode_title_and_tasks_list(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cli" / "db").mkdir(parents=True)
            con = sqlite3.connect(root / "cli" / "db" / "db.sqlite")
            con.executescript(
                """
                CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, title TEXT,
                    time_created INTEGER, time_updated INTEGER, time_archived INTEGER);
                CREATE TABLE todo (session_id TEXT, content TEXT, status TEXT, position INTEGER,
                    PRIMARY KEY (session_id, position));
                """
            )
            now = time.time()
            con.execute(
                "INSERT INTO session VALUES (?,?,?,?,?,?)",
                ("sess_1", "D:/work/delta", "修复看板", 0, int(now * 1000) - 30_000, None),
            )
            con.execute("INSERT INTO todo VALUES (?,?,?,?)", ("sess_1", "复现问题", "completed", 1))
            con.execute("INSERT INTO todo VALUES (?,?,?,?)", ("sess_1", "改代码", "in_progress", 2))
            con.commit()
            con.close()
            (s,) = ag.collect_sessions({"zcode": root}, now)
            self.assertEqual(s["title"], "修复看板")
            subjects = [(t["subject"], t["status"]) for t in s["tasks"]]
            self.assertEqual(subjects, [("复现问题", "completed"), ("改代码", "in_progress")])

    def test_hermes_title(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            con = sqlite3.connect(root / "state.db")
            con.executescript(
                """
                CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, title TEXT,
                    started_at REAL, ended_at REAL, end_reason TEXT,
                    last_activity_at REAL, archived INTEGER DEFAULT 0);
                """
            )
            now = time.time()
            con.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                ("h1", "D:/work/gamma", "数据迁移", now - 30, now, "done", now - 30, 0),
            )
            con.commit()
            con.close()
            (s,) = ag.collect_sessions({"hermes": root}, now)
            self.assertEqual(s["title"], "数据迁移")


class ShapeTest(unittest.TestCase):
    """全部五工具的会话项都带齐扩展键（值可为 None）。"""

    KEYS = {
        "tool", "id", "project", "title", "running", "age",
        "tasks_done", "tasks_total", "current_task", "tasks",
        "preview", "preview_role", "tokens", "state",
    }

    def test_qoder_session_has_full_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = root / "projects" / "p"
            d.mkdir(parents=True)
            jf = d / "s1.jsonl"
            jf.write_text(json.dumps(_assistant_text("ok")) + "\n", encoding="utf-8")
            (s,) = ag.collect_sessions({"qoder": root}, time.time())
            self.assertEqual(set(s.keys()), self.KEYS)


if __name__ == "__main__":
    unittest.main()
