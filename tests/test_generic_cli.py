# -*- coding: utf-8 -*-
"""GenericCliAdapter：生命周期/NDJSON/排队/取消/启动失败/{prompt} 替换。"""
import sys
import time
from pathlib import Path

import pytest

from copilot.cli_agents import AgentConfig, parse_agent
from copilot.generic_cli import CliAdapterError, GenericCliAdapter
from copilot.tasks import TaskPool

MOCK = str(Path(__file__).parent / "fixtures" / "mock_cli_agent.py")


def mock_agent(mode, json_lines=True, patterns=(), args_extra=()):
    return AgentConfig(
        name=f"mock-{mode}",
        program=sys.executable,
        args=[MOCK, "--mode", mode, *args_extra, "{prompt}"],
        display=f"Mock {mode}",
        json_lines=json_lines,
        needs_input_patterns=list(patterns),
    )


def make_adapter(tmp_path, cfgs, max_concurrent=3):
    pool = TaskPool(None)  # 适配器测试不落盘
    adapter = GenericCliAdapter(
        {c.name: c for c in cfgs}, pool, tmp_path / "logs",
        max_concurrent=max_concurrent,
    )
    return adapter, pool


def wait_status(pool, ref, statuses, timeout=15.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        t = pool.find("cli", ref)
        if t is not None:
            last = t
            if t.status in statuses:
                return t
        time.sleep(0.05)
    raise AssertionError(f"ref={ref} 未到 {statuses}，当前 {last and (last.status, last.note)}")


@pytest.fixture(autouse=True)
def _shutdown_adapters(request):
    yield


class TestLifecycle:
    def test_ok_flow_ndjson(self, tmp_path):
        adapter, pool = make_adapter(tmp_path, [mock_agent("ok")])
        try:
            task = adapter.submit("mock-ok", "你好 世界")
            t = wait_status(pool, task.ref_id, {"ready"})
            assert t.exit_code == 0 and t.unread
            assert t.last_text == "最终结果：你好 世界"
            assert t.extra.get("session_id") == "mock-s1"
            assert t.extra.get("phase") == "working"
            assert t.extra.get("final") is True
            assert Path(t.log_path).exists()
            assert "final" in Path(t.log_path).read_text(encoding="utf-8")
        finally:
            adapter.shutdown()

    def test_fail_flow(self, tmp_path):
        adapter, pool = make_adapter(tmp_path, [mock_agent("fail")])
        try:
            task = adapter.submit("mock-fail", "x")
            t = wait_status(pool, task.ref_id, {"blocked"})
            assert t.exit_code == 3
            assert "退出码 3" in t.note
        finally:
            adapter.shutdown()

    def test_needs_input_is_transient(self, tmp_path):
        adapter, pool = make_adapter(
            tmp_path, [mock_agent("needs", patterns=["^CONFIRM:"])]
        )
        try:
            task = adapter.submit("mock-needs", "x")
            # 过程态出现过 needs_input
            wait_status(pool, task.ref_id, {"needs_input"})
            # 退出码最终覆盖为 ready
            t = wait_status(pool, task.ref_id, {"ready"}, timeout=15)
            assert "CONFIRM" in t.extra.get("needs_input_hint", "")
        finally:
            adapter.shutdown()

    def test_failed_to_start(self, tmp_path):
        cfg = AgentConfig(
            name="ghost", program="definitely-missing-program-xyz",
            args=["{prompt}"],
        )
        adapter, pool = make_adapter(tmp_path, [cfg])
        try:
            task = adapter.submit("ghost", "x")
            t = wait_status(pool, task.ref_id, {"blocked"})
            assert "启动失败" in t.note
        finally:
            adapter.shutdown()

    def test_prompt_replacement_no_shell(self, tmp_path):
        adapter, pool = make_adapter(tmp_path, [mock_agent("echo", json_lines=False)])
        try:
            task = adapter.submit("mock-echo", '带 空格 & "引号" |whoami')
            t = wait_status(pool, task.ref_id, {"ready"})
            assert t.last_text == 'ECHO|带 空格 & "引号" |whoami'
        finally:
            adapter.shutdown()


class TestQueueAndCancel:
    def test_queueing_respects_max_concurrent(self, tmp_path):
        adapter, pool = make_adapter(tmp_path, [mock_agent("ok")], max_concurrent=1)
        try:
            t1 = adapter.submit("mock-ok", "first")
            t2 = adapter.submit("mock-ok", "second")
            assert pool.find("cli", t2.ref_id).extra.get("queued") is True
            w1 = wait_status(pool, t1.ref_id, {"ready"})
            w2 = wait_status(pool, t2.ref_id, {"ready"}, timeout=20)
            # 先提交的先完成（串行）
            assert w1.updated_at <= w2.updated_at
        finally:
            adapter.shutdown()

    def test_cancel_running(self, tmp_path):
        adapter, pool = make_adapter(tmp_path, [mock_agent("sleep")])
        try:
            task = adapter.submit("mock-sleep", "x")
            wait_status(pool, task.ref_id, {"running"})
            time.sleep(0.5)  # 让子进程真正进入 sleep
            assert adapter.cancel(task.task_id) is True
            t = wait_status(pool, task.ref_id, {"blocked"})
            assert "已取消" in t.note
        finally:
            adapter.shutdown()

    def test_cancel_queued(self, tmp_path):
        cfg = mock_agent("sleep")
        adapter, pool = make_adapter(tmp_path, [cfg], max_concurrent=1)
        try:
            first = adapter.submit("mock-sleep", "holds-slot")
            second = adapter.submit("mock-sleep", "queued")
            time.sleep(0.3)
            assert pool.find("cli", second.ref_id).extra.get("queued") is True
            assert adapter.cancel(second.task_id) is True
            t = wait_status(pool, second.ref_id, {"blocked"})
            assert "排队" in t.note
            adapter.cancel(first.task_id)
            wait_status(pool, first.ref_id, {"blocked"})
        finally:
            adapter.shutdown()


class TestValidation:
    def test_unknown_agent_and_empty_prompt(self, tmp_path):
        adapter, pool = make_adapter(tmp_path, [mock_agent("ok")])
        try:
            with pytest.raises(CliAdapterError) as e:
                adapter.submit("nope", "x")
            assert e.value.code == "unknown_agent"
            with pytest.raises(CliAdapterError) as e:
                adapter.submit("mock-ok", "   ")
            assert e.value.code == "empty_prompt"
        finally:
            adapter.shutdown()


class TestConfig:
    def test_parse_agent_rules(self):
        ok = parse_agent(
            {"name": "a", "program": "p", "args": ["{prompt}"]}, 0
        )
        assert ok.name == "a" and ok.display == "a" and ok.json_lines is False
        assert ok.build_argv("hi there") == ["p", "hi there"]
        # args 无占位
        with pytest.raises(Exception):
            parse_agent({"name": "a", "program": "p", "args": ["x"]}, 0)
        # 两个占位
        with pytest.raises(Exception):
            parse_agent(
                {"name": "a", "program": "p", "args": ["{prompt}", "{prompt}"]}, 0
            )
        # 坏正则
        with pytest.raises(Exception):
            parse_agent(
                {
                    "name": "a", "program": "p", "args": ["{prompt}"],
                    "needs_input_patterns": ["("],
                },
                0,
            )
