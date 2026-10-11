# -*- coding: utf-8 -*-
"""TaskHub：ACP 映射表 + 两源同池统一快照。"""
import sys
import time
from pathlib import Path

from copilot.cli_agents import AgentConfig
from copilot.generic_cli import GenericCliAdapter
from copilot.task_hub import AcpTaskSource, TaskHub
from copilot.tasks import TaskPool

MOCK = str(Path(__file__).parent / "fixtures" / "mock_cli_agent.py")


class FakeController:
    """只实现 AcpTaskSource 依赖的 subscribe/snapshot 两个面。"""

    def __init__(self):
        self.runs = []
        self.callbacks = []
        self.notify_count = 0

    def subscribe(self, cb):
        self.callbacks.append(cb)

    def snapshot(self):
        return {"runs": list(self.runs)}

    def set_runs(self, runs):
        self.runs = runs
        self.notify_count += 1
        for cb in list(self.callbacks):
            cb("runs_changed")


def run(state, run_id="run-1", user_text="帮我审查这段", stop_reason=None, error=None):
    return {
        "run_id": run_id,
        "conversation_id": "conv-1",
        "state": state,
        "user_text": user_text,
        "stop_reason": stop_reason,
        "error_code": error,
    }


def wait_for(predicate, timeout=15.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting: {what}")


class TestAcpMapping:
    def test_full_mapping_table(self, tmp_path):
        ctl = FakeController()
        pool = TaskPool(None)
        src = AcpTaskSource(ctl, pool)
        cases = {
            "PREPARING": "running", "SUBMITTING": "running", "RUNNING": "running",
            "CANCELLING": "running", "WAITING_PERMISSION": "needs_input",
            "SETTLED": "ready", "LIMITED": "ready",
            "FAILED": "blocked", "INTERRUPTED": "blocked", "CANCELLED": "blocked",
        }
        for i, (state, want) in enumerate(cases.items()):
            ctl.set_runs([run(state, run_id=f"r{i}")])
        src.sync()
        for i, (state, want) in enumerate(cases.items()):
            t = pool.find("acp", f"r{i}")
            assert t is not None, state
            assert t.status == want, f"{state} -> {t.status} (want {want})"

    def test_notification_driven_sync_and_unread(self, tmp_path):
        ctl = FakeController()
        pool = TaskPool(None)
        AcpTaskSource(ctl, pool)
        ctl.set_runs([run("RUNNING", run_id="a")])
        assert pool.find("acp", "a").status == "running"
        ctl.set_runs([run("WAITING_PERMISSION", run_id="a")])
        t = pool.find("acp", "a")
        assert t.status == "needs_input" and t.unread
        assert t.note == "待许可"

    def test_limited_and_interrupted_notes(self, tmp_path):
        ctl = FakeController()
        pool = TaskPool(None)
        src = AcpTaskSource(ctl, pool)
        ctl.set_runs([run("LIMITED", run_id="l", stop_reason="max_tokens")])
        ctl.set_runs([run("INTERRUPTED", run_id="i")])
        src.sync()
        assert pool.find("acp", "l").note == "已到本轮上限"
        assert "中断" in pool.find("acp", "i").note


class TestHub:
    def make_hub(self, tmp_path, ctl):
        cfg = AgentConfig(
            name="mock-ok", program=sys.executable,
            args=[MOCK, "--mode", "ok", "{prompt}"], display="Mock",
            json_lines=True,
        )
        agents_path = tmp_path / "copilot_agents.json"
        import json

        agents_path.write_text(
            json.dumps({"agents": [{
                "name": cfg.name, "display": cfg.display, "program": cfg.program,
                "args": cfg.args, "json_lines": True,
            }]}),
            encoding="utf-8",
        )
        return TaskHub(ctl, data_dir=tmp_path, agents_path=agents_path)

    def test_unified_snapshot_two_sources(self, tmp_path):
        ctl = FakeController()
        hub = self.make_hub(tmp_path, ctl)
        try:
            ctl.set_runs([run("WAITING_PERMISSION", run_id="acp1", user_text="ACP 任务")])
            cli_task = hub.submit_cli("mock-ok", "CLI 任务")
            wait_for(
                lambda: (hub.pool.find("cli", cli_task.ref_id) or cli_task).status == "ready",
                what="cli task ready",
            )
            snap = hub.snapshot()
            statuses = [(t["source"], t["status"]) for t in snap["tasks"]]
            # needs_input 的 ACP 任务排最前；ready 的 CLI 在后
            assert statuses[0] == ("acp", "needs_input")
            assert ("cli", "ready") in statuses
            assert snap["stats"]["total"] == 2
            assert snap["agents"] == ["mock-ok"]
        finally:
            hub.shutdown()

    def test_mark_seen_and_persisted(self, tmp_path):
        ctl = FakeController()
        hub = self.make_hub(tmp_path, ctl)
        try:
            ctl.set_runs([run("SETTLED", run_id="s1")])
            t = hub.pool.find("acp", "s1")
            # 新建 Task 默认 running，首投影 SETTLED 即发生 running->ready
            # 状态变化沿 → 置未读（"有结果没看过"）
            assert t.unread is True
            assert hub.mark_seen(t.task_id) is True
            assert hub.pool.find("acp", "s1").unread is False
        finally:
            hub.shutdown()
        # 关闭后持久化文件存在且可再载
        assert (tmp_path / "task_pool.json").exists()

    def test_cancel_acp_refused(self, tmp_path):
        ctl = FakeController()
        hub = self.make_hub(tmp_path, ctl)
        try:
            ctl.set_runs([run("RUNNING", run_id="acp1")])
            t = hub.pool.find("acp", "acp1")
            assert hub.cancel(t.task_id) is False  # ACP 取消走 controller，不经 hub
        finally:
            hub.shutdown()
