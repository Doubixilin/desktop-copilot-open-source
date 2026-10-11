# -*- coding: utf-8 -*-
"""hud_api：总览/submit（scene 注入）/许可/取消/事件——假 controller+hub 全离线。"""
import json
from pathlib import Path

import pytest

from copilot import hud_api
from copilot import models
from copilot.cli_agents import AgentConfig  # noqa: F401  构造 hub 用
from copilot.task_hub import TaskHub  # noqa: F401


class FakeController:
    def __init__(self):
        self.conv = models.Conversation(
            conversation_id="conv-1", runtime_fingerprint="fp", dsh_home="h",
            cwd="D:/w", created_at=1.0, last_activity_at=2.0, title="工作",
        )
        self.run = models.Run(
            run_id="run-1", conversation_id="conv-1",
            state=models.RunState.RUNNING, submitted_at=1.0, last_event_at=1.0,
            user_text="帮我整理",
        )
        self.perm = models.PermissionRecord(
            permission_id="perm-1", run_id="run-1", conversation_id="conv-1",
            runtime_epoch=0, tool_call_id="tc-1", created_at=1.0,
            tool_title="写文件", tool_kind="write",
            options=[models.PermissionOptionView("allow", "允许", "allow_once"),
                     models.PermissionOptionView("deny", "拒绝", "reject")],
        )
        self.submitted = []
        self.answered = []
        self.cancelled = []

    def subscribe(self, cb):
        pass

    def snapshot(self):
        return {
            "conversations": [self.conv.to_public()],
            "runs": [self.run.to_public()],
            "active_run": self.run.to_public(),
            "pending_permissions": [self.perm.to_public()],
            "runtime": {"spawned": True, "epoch": 1, "executable": "x"},
            "store_healthy": True,
        }

    def events(self, conversation_id, after_seq=0):
        if conversation_id != "conv-1":
            return []
        return [
            models.EventRecord(seq=1, run_id="run-1", conversation_id="conv-1",
                               runtime_epoch=0, kind="user",
                               payload={"text": "帮我整理"}, observed_at=1.0),
            models.EventRecord(seq=2, run_id="run-1", conversation_id="conv-1",
                               runtime_epoch=0, kind="assistant",
                               payload={"text": "开始处理"}, observed_at=1.5),
        ]

    def create_conversation(self, workspace, title=""):
        return self.conv

    def submit(self, conversation_id, text, context_refs=None, run_id=None):
        self.submitted.append((conversation_id, text, context_refs))
        return self.run

    def answer_permission(self, permission_id, option_id):
        self.answered.append((permission_id, option_id))
        return True

    def cancel(self, run_id):
        self.cancelled.append(run_id)
        return True


class TestSnapshot:
    def test_full_shape(self):
        ctl = FakeController()
        out = hud_api.hud_snapshot(ctl, hub=None)
        assert out["available"] is True
        assert out["active_run"]["run_id"] == "run-1"
        assert len(out["conversations"]) == 1
        conv = out["conversations"][0]
        assert conv["conversation_id"] == "conv-1"
        kinds = [e["kind"] for e in conv["events"]]
        assert kinds == ["user", "assistant"]
        assert out["pending_permissions"][0]["permission_id"] == "perm-1"
        assert out["tasks"]["tasks"] == []  # hub=None 时任务区空但不缺键

    def test_unavailable_reason(self):
        out = hud_api.hud_snapshot(None, None)
        assert out["available"] is False and out["reason"]

    def test_event_clip(self):
        ctl = FakeController()
        ctl.events = lambda cid, after_seq=0: [
            models.EventRecord(seq=1, run_id="r", conversation_id=cid,
                               runtime_epoch=0, kind="assistant",
                               payload={"text": "x" * 5000}, observed_at=1.0),
        ]
        out = hud_api.hud_snapshot(ctl, None)
        text = out["conversations"][0]["events"][0]["payload"]["text"]
        assert len(text) < 5000 and text.endswith("…[截断]")


class TestSubmit:
    def _scene(self):
        return {
            "scenario": "ide", "coverage": "full", "note": "",
            "identity": {"pid": 1, "process": "Code.exe", "title": "a.py — VS Code",
                          "exe": "C:/Code.exe", "class": "C", "ts": 1.0},
            "materials": [{"kind": "selection", "source": "uia:text", "chars": 9,
                            "preview": "print(1)", "text": "print(1)", "ts": 1.0}],
        }

    def test_submit_with_scene_injects_block(self, tmp_path):
        ctl = FakeController()
        out = hud_api.submit_task(
            ctl, {"text": "解释这段", "conversation_id": "conv-1",
                  "scene": self._scene()},
            package_dir=tmp_path / "pkgs",
        )
        assert "error" not in out and out["conversation_id"] == "conv-1"
        conv_id, text, refs = ctl.submitted[0]
        assert "[现场包 pkg-" in text and "解释这段" in text and "print(1)" in text
        pkg_files = list((tmp_path / "pkgs").glob("pkg-*.json"))
        assert len(pkg_files) == 1  # 包落盘可追溯

    def test_submit_creates_conversation_when_missing(self):
        ctl = FakeController()
        out = hud_api.submit_task(ctl, {"text": "新任务"})
        assert out["conversation_id"] == "conv-1"  # create_conversation 被调

    def test_submit_empty_rejected(self):
        out = hud_api.submit_task(FakeController(), {"text": "   "})
        assert out["error"]["code"] == "empty_prompt"

    def test_submit_controller_error_shape(self):
        class Boom(FakeController):
            def submit(self, *a, **k):
                raise models.RunState and Exception("run_in_progress: 已有任务")

        class Err(Boom):
            def submit(self, *a, **k):
                e = Exception("已有任务在执行")
                e.code = "run_in_progress"
                raise e

        out = hud_api.submit_task(Err(), {"text": "x", "conversation_id": "conv-1"})
        assert out["error"]["code"] == "run_in_progress"


class DedupeController(FakeController):
    """模拟 controller.submit 的 run_id 幂等语义（重复 run_id 返回原记录）。"""

    def __init__(self):
        super().__init__()
        self._by_run = {}
        self.replays = 0

    def submit(self, conversation_id, text, context_refs=None, run_id=None):
        if run_id and run_id in self._by_run:
            self.replays += 1
            return self._by_run[run_id]
        run = models.Run(
            run_id=run_id or f"run-{len(self.submitted) + 1}",
            conversation_id=conversation_id,
            state=models.RunState.PREPARING,
            submitted_at=1.0, last_event_at=1.0, user_text=text,
        )
        self._by_run[run.run_id] = run
        self.submitted.append((conversation_id, text, context_refs))
        return run


class TestRequestIdDedup:
    """票 01 R09：同客户端同 request_id 同载荷 → 唯一受理与轮次。"""

    def setup_method(self):
        hud_api.reset_request_runs()

    def teardown_method(self):
        hud_api.reset_request_runs()

    def test_same_request_id_same_payload_replays_original(self):
        ctl = DedupeController()
        payload = {"text": "整理一下", "conversation_id": "conv-1",
                   "request_id": "req-abc"}
        first = hud_api.submit_task(ctl, dict(payload))
        assert "error" not in first
        second = hud_api.submit_task(ctl, dict(payload))
        assert "error" not in second
        assert second["run"]["run_id"] == first["run"]["run_id"], "重发必须命中原轮次"
        assert second["replayed"] is True
        assert second["conversation_id"] == first["conversation_id"]
        assert len(ctl.submitted) == 1, "只允许一次真实派单"
        assert ctl.replays == 1

    def test_same_request_id_different_payload_conflicts(self):
        ctl = DedupeController()
        hud_api.submit_task(ctl, {"text": "第一版内容", "request_id": "req-x"})
        out = hud_api.submit_task(ctl, {"text": "改过的内容", "request_id": "req-x"})
        assert out["error"]["code"] == "idempotency_conflict"

    def test_failed_first_attempt_does_not_map(self):
        """首投失败（如单活跃约束）不落映射：重试是全新受理，不是误命中。"""
        class Busy(DedupeController):
            def submit(self, *a, **k):
                if not k.get("run_id"):
                    e = Exception("已有任务在执行")
                    e.code = "run_in_progress"
                    raise e
                return super().submit(*a, **k)

        busy = Busy()
        out1 = hud_api.submit_task(busy, {"text": "排队任务", "request_id": "req-q"})
        assert out1["error"]["code"] == "run_in_progress"
        out2 = hud_api.submit_task(busy, {"text": "排队任务", "request_id": "req-q"})
        assert out2["error"]["code"] == "run_in_progress", "失败未受理不得静默成功"

    def test_request_map_bounded(self):
        ctl = DedupeController()
        for i in range(hud_api.MAX_REQUEST_MAP + 10):
            hud_api.submit_task(ctl, {"text": f"t{i}", "request_id": f"req-{i}"})
        assert len(hud_api._REQUEST_RUNS) == hud_api.MAX_REQUEST_MAP

    def test_context_refs_passed_to_controller(self):
        ctl = FakeController()
        out = hud_api.submit_task(
            ctl, {"text": "看这个文件", "conversation_id": "conv-1",
                  "context_refs": ["D:/t/a.txt"]})
        assert "error" not in out
        _cid, _text, refs = ctl.submitted[0]
        assert refs == ["D:/t/a.txt"], "结构化引用必须到达 controller"

    def test_response_echoes_request_id(self):
        ctl = DedupeController()
        out = hud_api.submit_task(ctl, {"text": "x", "request_id": "req-echo"})
        assert out["request_id"] == "req-echo"
        out2 = hud_api.submit_task(ctl, {"text": "无身份"})
        assert out2["request_id"] is None


class TestPermissionCancelEvents:
    def test_permission(self):
        ctl = FakeController()
        out = hud_api.answer_permission(
            ctl, {"permission_id": "perm-1", "option_id": "allow"})
        assert out == {"ok": True} and ctl.answered == [("perm-1", "allow")]

    def test_permission_bad(self):
        out = hud_api.answer_permission(FakeController(), {"permission_id": ""})
        assert out["error"]["code"] == "bad_request"

    def test_cancel(self):
        ctl = FakeController()
        assert hud_api.cancel_run(ctl, {"run_id": "run-1"}) == {"ok": True}

    def test_events_listing(self):
        out = hud_api.list_events(FakeController(), "conv-1")
        assert [e["kind"] for e in out["events"]] == ["user", "assistant"]
        out = hud_api.list_events(FakeController(), "")
        assert out["error"]["code"] == "bad_request"


class TestRequestIdentityFixes:
    """复核 F01/F02：新会话重试同指纹；并发同 ID 只受理一次。"""

    def setup_method(self):
        hud_api.reset_request_runs()

    def teardown_method(self):
        hud_api.reset_request_runs()

    def test_new_conversation_retry_replays_original(self):
        """F01：首投 conversation_id=None 创建会话后，原载荷重试不得误判冲突。"""
        ctl = DedupeController()
        p = {"text": "整理一下", "request_id": "req-new", "conversation_id": None}
        first = hud_api.submit_task(ctl, dict(p))
        assert "error" not in first
        retry = hud_api.submit_task(ctl, dict(p))
        assert "error" not in retry, "新会话首投与重试必须同指纹"
        assert retry["run"]["run_id"] == first["run"]["run_id"]
        assert retry.get("replayed") is True
        assert retry["conversation_id"] == first["conversation_id"]

    def test_retry_with_created_conversation_id_conflicts(self):
        """F01 边界：重试若带上首投生成的会话 ID，即同 ID 不同载荷 → 409。

        客户端正确做法是重发冻结的原载荷（F05 页面快照），而不是改写后重试。
        """
        ctl = DedupeController()
        first = hud_api.submit_task(ctl, {"text": "x", "request_id": "req-c"})
        assert "error" not in first
        retry = hud_api.submit_task(
            ctl, {"text": "x", "request_id": "req-c",
                  "conversation_id": first["conversation_id"]})
        assert retry["error"]["code"] == "idempotency_conflict"

    def test_concurrent_same_request_id_single_acceptance(self):
        """F02：并发同 ID 同载荷——一次真实派单，两条回复同轮次。"""
        import threading
        import time as _t

        class SlowDedupe(DedupeController):
            def submit(self, *a, **k):
                _t.sleep(0.3)  # 放宽窗口：无锁实现会让两线程同时越过查图
                return super().submit(*a, **k)

        ctl = SlowDedupe()
        p = {"text": "并发任务", "request_id": "req-race", "conversation_id": None}
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(hud_api.submit_task, ctl, dict(p))
                       for _ in range(2)]
            replies = [f.result(timeout=10) for f in futures]
        assert all("error" not in r for r in replies)
        ids = {r["run"]["run_id"] for r in replies}
        assert len(ids) == 1, "并发同 ID 必须收敛到同一条受理"
        assert len(ctl.submitted) == 1, "只允许一次真实派单"
        assert ctl.replays == 1
