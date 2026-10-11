# -*- coding: utf-8 -*-
"""TaskPool：优先级/未读沿/环形/孤儿对账/防抖合并写。"""
import json
import time
from unittest import mock

from copilot.tasks import TaskPool


def make_pool(tmp_path, **kw):
    return TaskPool(tmp_path / "task_pool.json", debounce_seconds=0.05, **kw)


class TestStatusSemantics:
    def test_priority_order(self, tmp_path):
        pool = make_pool(tmp_path)
        for i, status in enumerate(["running", "ready", "blocked", "needs_input", "unknown"]):
            pool.upsert("cli", f"r{i}", agent="a", title=f"t{i}", status=status)
        order = [t.status for t in pool.list_tasks()]
        assert order == ["needs_input", "blocked", "ready", "running", "unknown"]

    def test_unread_on_transition_edge(self, tmp_path):
        pool = make_pool(tmp_path)
        pool.upsert("cli", "r1", status="running", agent="a", title="t")
        t = pool.find("cli", "r1")
        assert t.unread is False
        pool.upsert("cli", "r1", status="ready")
        assert pool.find("cli", "r1").unread is True
        # needs_input 也触发
        pool.upsert("cli", "r2", status="running", agent="a", title="t")
        pool.upsert("cli", "r2", status="needs_input")
        assert pool.find("cli", "r2").unread is True
        # ready -> blocked 的转移不重复置（已经 terminal）
        pool.mark_seen(pool.find("cli", "r1").task_id)
        pool.upsert("cli", "r1", status="blocked", note="改判")
        assert pool.find("cli", "r1").unread is False

    def test_title_and_text_clipped(self, tmp_path):
        pool = make_pool(tmp_path)
        pool.upsert("cli", "r1", agent="a", title="字" * 100, last_text="x" * 500)
        t = pool.find("cli", "r1")
        assert len(t.title) <= 41  # 40 + 省略号
        assert len(t.last_text) <= 201

    def test_invalid_status_rejected(self, tmp_path):
        pool = make_pool(tmp_path)
        try:
            pool.upsert("cli", "r1", status="bogus")
        except ValueError:
            pass
        else:
            raise AssertionError("should reject unknown status")

    def test_stats(self, tmp_path):
        pool = make_pool(tmp_path)
        pool.upsert("cli", "a", status="running", agent="x", title="t")
        pool.upsert("cli", "b", status="ready", agent="x", title="t")
        pool.upsert("cli", "c", status="ready", agent="x", title="t")
        s = pool.stats()
        assert s["total"] == 3 and s["running"] == 1 and s["ready"] == 2
        assert s["unread"] == 2  # 两个 ready 均沿触发


class TestPersistence:
    def test_ring_trim(self, tmp_path):
        pool = make_pool(tmp_path, keep_tasks=3)
        for i in range(6):
            pool.upsert("cli", f"r{i}", agent="a", title=f"t{i}")
            time.sleep(0.01)
        pool.flush()
        data = json.loads((tmp_path / "task_pool.json").read_text(encoding="utf-8"))
        assert len(data["tasks"]) == 3
        refs = {t["ref_id"] for t in data["tasks"]}
        assert refs == {"r3", "r4", "r5"}  # 保留最新

    def test_orphan_reconcile_on_load(self, tmp_path):
        pool = make_pool(tmp_path)
        pool.upsert("cli", "live", status="running", agent="a", title="t")
        pool.upsert("cli", "hung", status="needs_input", agent="a", title="t")
        pool.upsert("cli", "done", status="ready", agent="a", title="t")
        pool.flush()
        pool2 = TaskPool(tmp_path / "task_pool.json")
        t = pool2.find("cli", "live")
        assert t.status == "blocked" and "宿主重启" in t.note and t.unread
        assert pool2.find("cli", "hung").status == "blocked"
        assert pool2.find("cli", "done").status == "ready"  # 终态不受影响

    def test_debounce_coalesces_writes(self, tmp_path):
        pool = TaskPool(tmp_path / "task_pool.json", debounce_seconds=0.3)
        with mock.patch.object(
            pool, "_write_file", wraps=pool._write_file
        ) as writer:
            for i in range(20):
                pool.upsert("cli", f"r{i}", agent="a", title=f"t{i}")
            # 20 次更新发生在同一防抖窗口内（远小于 0.3s）→ 合并为一次写
            time.sleep(0.8)
            assert writer.call_count <= 2
        assert pool._dirty is False
        data = json.loads((tmp_path / "task_pool.json").read_text(encoding="utf-8"))
        assert len(data["tasks"]) == 20

    def test_atomic_and_schema(self, tmp_path):
        pool = make_pool(tmp_path)
        pool.upsert("cli", "r1", agent="a", title="t")
        pool.flush()
        data = json.loads((tmp_path / "task_pool.json").read_text(encoding="utf-8"))
        assert data["schema"] == 1
        assert not list(tmp_path.glob("*.tmp"))


class TestSubscribe:
    def test_callbacks_and_clear_finished(self, tmp_path):
        events = []
        pool = make_pool(tmp_path)
        pool.subscribe(lambda reason: events.append(reason))
        pool.upsert("cli", "a", status="running", agent="x", title="t")
        pool.upsert("cli", "a", status="ready")  # 同 ref：update 而非 create
        assert events[:2] == ["created", "updated"]
        pool.upsert("cli", "b", status="running", agent="x", title="t")
        removed = pool.clear_finished()
        assert removed == 1
        assert pool.find("cli", "a") is None
        assert pool.find("cli", "b") is not None
