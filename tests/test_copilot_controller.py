"""02/03 票：控制器全链路测试（fake agent，无 key/网络）。

覆盖 acceptance C03/C04/C05/C08/C09/C10 及权限六行为（C06/C07 的自动部分）。
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from copilot import models  # noqa: E402
from copilot.controller import CopilotController, ControllerError  # noqa: E402
from copilot.store import CopilotStore  # noqa: E402

PYTHON = sys.executable


def wait_until(predicate, timeout=15.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class Harness:
    def __init__(self, tmp_path: Path, script: dict, **controller_kwargs):
        self.tmp = tmp_path
        self.script_path = tmp_path / "fake_script.json"
        self.script_path.write_text(
            json.dumps(script, ensure_ascii=False), encoding="utf-8"
        )
        self.dsh_home = tmp_path / "dsh-home"
        self.dsh_home.mkdir(exist_ok=True)
        self.workspace = tmp_path / "项目 目录"
        self.workspace.mkdir(exist_ok=True)
        os.environ["FAKE_ACP_STATE"] = str(tmp_path / "fake-agent-state.json")
        self.store = CopilotStore(tmp_path / "copilot.sqlite3")
        controller_kwargs.setdefault("idle_shutdown_seconds", 0)  # 测试不闲置回收
        controller_kwargs.setdefault("cancel_warning_seconds", 3)
        controller_kwargs.setdefault("stderr_dir", tmp_path / "stderr")
        self.controller = CopilotController(
            executable=PYTHON,
            runtime_args=["-m", "copilot.fake_agent", "--script", str(self.script_path)],
            dsh_home=str(self.dsh_home),
            launch_cwd=str(REPO_ROOT),
            store=self.store,
            **controller_kwargs,
        )

    def new_conversation(self):
        return self.controller.create_conversation(str(self.workspace))

    def submit(self, conv, text, **kw):
        return self.controller.submit(conv.conversation_id, text, **kw)

    def run_state(self, run_id):
        snap = self.controller.snapshot()
        for r in snap["runs"]:
            if r["run_id"] == run_id:
                return r["state"]
        return None

    def wait_terminal(self, run_id, timeout=15):
        assert wait_until(
            lambda: self.run_state(run_id) in {s.value for s in models.TERMINAL_STATES},
            timeout,
        ), f"run {run_id} 未到终态，当前 {self.run_state(run_id)} events=" + json.dumps(
            [
                {"kind": e.kind, "payload": e.payload}
                for r in self.controller.snapshot()["runs"]
                if r["run_id"] == run_id
                for e in self.events(r["conversation_id"])
            ],
            ensure_ascii=False,
        )[:800]
        return self.run_state(run_id)

    def events_of(self, conv_id, kind=None):
        return [
            e
            for e in self.controller.events(conv_id)
            if kind is None or e.kind == kind
        ]

    def close(self):
        self.controller.shutdown(timeout=15)
        self.store.close()


@pytest.fixture()
def harness(tmp_path):
    def make(script, **kw):
        return Harness(tmp_path, script, **kw)

    return make


# ---------------- 02 票：会话/轮次/回显 ----------------

TWO_TURN_SCRIPT = {
    "turns": [
        {
            "updates": [
                {"kind": "user_echo"},
                {"kind": "agent_text", "text": "第一轮回复"},
            ],
            "stop_reason": "end_turn",
        },
        {
            "match": "第二",
            "updates": [
                {"kind": "tool_start", "id": "t1", "title": "读文件", "tool_kind": "read"},
                {"kind": "tool_update", "id": "t1", "status": "completed",
                 "content_text": "文件内容摘要"},
                {"kind": "agent_text", "text": "第二轮回复"},
            ],
            "stop_reason": "end_turn",
        },
    ],
    "on_cancel": {"settle_after": 0.2, "stop_reason": "cancelled"},
}


def test_two_turns_share_session_and_echo(harness):
    h = harness(TWO_TURN_SCRIPT)
    try:
        conv = h.new_conversation()
        r1 = h.submit(conv, "第一问")
        assert h.wait_terminal(r1.run_id) == "SETTLED"
        r2 = h.submit(conv, "第二问 展开")
        assert h.wait_terminal(r2.run_id) == "SETTLED"
        snap = h.controller.snapshot()
        conv_pub = next(
            c for c in snap["conversations"] if c["conversation_id"] == conv.conversation_id
        )
        # 两轮共用同一 DSH session
        runs = h.store.runs_for_conversation(conv.conversation_id)
        assert len(runs) == 2
        texts = [e.payload.get("text", "") for e in h.events_of(conv.conversation_id, "assistant")]
        assert "第一轮回复" in "".join(texts)
        assert "第二轮回复" in "".join(texts)
        tools = h.events_of(conv.conversation_id, "tool")
        assert any(t.payload.get("title") == "读文件" for t in tools)
        assert any(t.payload.get("status") == "completed" for t in tools)
        # 本轮结束显示"已回复"，不声明任务完成
        assert r1.state_label if hasattr(r1, "state_label") else True
        assert models.STATE_LABELS[models.RunState.SETTLED] == "已回复"
    finally:
        h.close()


def test_different_cwd_new_session(harness):
    h = harness(TWO_TURN_SCRIPT)
    try:
        c1 = h.controller.create_conversation(str(h.workspace))
        r1 = h.submit(c1, "第一问")
        assert h.wait_terminal(r1.run_id) == "SETTLED"
        ws2 = h.tmp / "另一个 目录"
        ws2.mkdir(exist_ok=True)
        c2 = h.controller.create_conversation(str(ws2))
        r2 = h.submit(c2, "第一问")
        assert h.wait_terminal(r2.run_id) == "SETTLED"
        s1 = h.store.get_conversation(c1.conversation_id).dsh_session_id
        s2 = h.store.get_conversation(c2.conversation_id).dsh_session_id
        assert s1 and s2 and s1 != s2
    finally:
        h.close()


def test_duplicate_submit_single_prompt(harness):
    script = {
        "turns": [
            {"updates": [{"kind": "user_echo"}, {"kind": "agent_text", "text": "回复"}],
             "stop_reason": "end_turn"}
        ]
    }
    h = harness(script)
    try:
        conv = h.new_conversation()
        run = h.submit(conv, "只发一次")
        dup = h.submit(conv, "只发一次", run_id=run.run_id)
        assert dup.run_id == run.run_id
        assert h.wait_terminal(run.run_id) == "SETTLED"
        # 只有一个 prompt：user_echo 事件只出现一次
        echoes = h.events_of(conv.conversation_id, "user_echo")
        assert len(echoes) == 1
        # store 中该会话仅 1 个 run
        assert len(h.store.runs_for_conversation(conv.conversation_id)) == 1
    finally:
        h.close()


def test_stop_reason_matrix(harness):
    for reason, expected in [
        ("end_turn", "SETTLED"),
        ("max_tokens", "LIMITED"),
        ("max_turn_requests", "LIMITED"),
        ("cancelled", None),  # 单独取消测试覆盖
        ("refusal", "SETTLED"),
        # 真"未知"值过不了 SDK Literal 校验 → 明确协议错误 FAILED（同样不虚报成功）
        ("made_up_reason", "FAILED"),
    ]:
        script = {
            "turns": [
                {"updates": [{"kind": "agent_text", "text": "x"}],
                 "stop_reason": reason}
            ]
        }
        h = harness(script)
        try:
            conv = h.new_conversation()
            run = h.submit(conv, "问")
            final = h.wait_terminal(run.run_id)
            if expected:
                assert final == expected, f"{reason} -> {final}"
                if reason != "made_up_reason":  # 传输层非法值到不了 stop_reason
                    stored = h.store.get_run(run.run_id)
                    assert stored.stop_reason == reason  # 原始原因保留
        finally:
            h.close()


def test_prompt_error_is_failed(harness):
    script = {
        "turns": [
            {"updates": [], "stop_reason": "end_turn", "prompt_error": "模型路由缺失"}
        ]
    }
    h = harness(script)
    try:
        conv = h.new_conversation()
        run = h.submit(conv, "问")
        assert h.wait_terminal(run.run_id) == "FAILED"
        stored = h.store.get_run(run.run_id)
        assert stored.error_code == "prompt_failed"
    finally:
        h.close()


def test_long_silence_stays_running_then_settles(harness):
    """C05：无更新期间保持运行态（不超时转空闲/完成）。"""
    script = {
        "turns": [
            {
                "updates": [
                    {"kind": "agent_text", "text": "开始"},
                    {"kind": "silent", "seconds": 2.0},
                    {"kind": "agent_text", "text": "结束"},
                ],
                "stop_reason": "end_turn",
            }
        ]
    }
    h = harness(script)
    try:
        conv = h.new_conversation()
        run = h.submit(conv, "长任务")
        # 静默期间仍是运行态（此处以 2s 模拟长静默；产品无超时自动转移）
        assert wait_until(lambda: h.run_state(run.run_id) == "RUNNING", 10)
        time.sleep(1.2)
        assert h.run_state(run.run_id) == "RUNNING"
        assert h.wait_terminal(run.run_id, timeout=15) == "SETTLED"
    finally:
        h.close()


def test_pipe_break_marks_interrupted_not_failed(harness):
    """C10：断管/未知送达 → INTERRUPTED，不自动重发。"""
    script = {
        "turns": [
            {
                "updates": [
                    {"kind": "agent_text", "text": "部分"},
                    {"kind": "exit", "code": 1},
                ],
                "stop_reason": "end_turn",
            }
        ]
    }
    h = harness(script)
    try:
        conv = h.new_conversation()
        run = h.submit(conv, "断管")
        assert h.wait_terminal(run.run_id, timeout=20) == "INTERRUPTED"
        stored = h.store.get_run(run.run_id)
        assert stored.error_code == "connection_lost"
        # 不自动重发：只有一个 run
        assert len(h.store.runs_for_conversation(conv.conversation_id)) == 1
    finally:
        h.close()


def test_store_failure_blocks_dispatch(harness, monkeypatch):
    h = harness(TWO_TURN_SCRIPT)
    try:
        conv = h.new_conversation()

        def boom(run):
            raise RuntimeError("disk full")

        monkeypatch.setattr(h.store, "save_run", boom)
        with pytest.raises(ControllerError) as ei:
            h.submit(conv, "写不进")
        assert ei.value.code == "store_write_failed"
        # 未派单：无 user_echo，无新 run 留存于 store（save_run 已被替换）
        snap = h.controller.snapshot()
        assert snap["active_run"] is None
    finally:
        h.close()


# ---------------- 03 票：许可与取消 ----------------

PERM_SCRIPT = {
    "turns": [
        {
            "updates": [
                {"kind": "tool_start", "id": "t1", "title": "写文件", "tool_kind": "edit"},
                {
                    "kind": "permission",
                    "tool_call_id": "t1",
                    "title": "写入 config.toml",
                    "options": [
                        {"optionId": "allow", "name": "允许", "kind": "allow_once"},
                        {"optionId": "reject", "name": "拒绝", "kind": "reject_once"},
                    ],
                    "on_selected": "已按选择继续",
                },
                {"kind": "agent_text", "text": "完成"},
            ],
            "stop_reason": "end_turn",
        }
    ],
    "on_cancel": {"settle_after": 0.2, "stop_reason": "cancelled"},
}


def _pending_permission(h):
    snap = h.controller.snapshot()
    pending = snap["pending_permissions"]
    return pending[0] if pending else None


def test_permission_allow(harness):
    h = harness(PERM_SCRIPT)
    try:
        conv = h.new_conversation()
        run = h.submit(conv, "写一下")
        assert wait_until(lambda: h.run_state(run.run_id) == "WAITING_PERMISSION", 10)
        perm = _pending_permission(h)
        assert perm["tool_title"] == "写入 config.toml"
        assert [o["option_id"] for o in perm["options"]] == ["allow", "reject"]
        assert h.controller.answer_permission(perm["permission_id"], "allow")
        assert h.wait_terminal(run.run_id) == "SETTLED"
        texts = "".join(
            e.payload.get("text", "") for e in h.events_of(conv.conversation_id, "assistant")
        )
        assert "已按选择继续" in texts
    finally:
        h.close()


def test_permission_deny(harness):
    h = harness(PERM_SCRIPT)
    try:
        conv = h.new_conversation()
        run = h.submit(conv, "写一下")
        assert wait_until(lambda: h.run_state(run.run_id) == "WAITING_PERMISSION", 10)
        perm = _pending_permission(h)
        assert h.controller.answer_permission(perm["permission_id"], "reject")
        assert h.wait_terminal(run.run_id) == "SETTLED"
    finally:
        h.close()


def test_permission_double_answer_rejected(harness):
    h = harness(PERM_SCRIPT)
    try:
        conv = h.new_conversation()
        run = h.submit(conv, "写一下")
        assert wait_until(lambda: h.run_state(run.run_id) == "WAITING_PERMISSION", 10)
        perm = _pending_permission(h)
        assert h.controller.answer_permission(perm["permission_id"], "allow")
        assert not h.controller.answer_permission(perm["permission_id"], "allow")  # 只能一次
        assert not h.controller.answer_permission(perm["permission_id"], "reject")
        assert h.wait_terminal(run.run_id) == "SETTLED"
    finally:
        h.close()


def test_permission_wait_no_auto_answer(harness):
    """C07：等待期间不默认批准（等效"收起面板不批准"）。"""
    h = harness(PERM_SCRIPT)
    try:
        conv = h.new_conversation()
        run = h.submit(conv, "写一下")
        assert wait_until(lambda: h.run_state(run.run_id) == "WAITING_PERMISSION", 10)
        time.sleep(1.5)  # 无操作等待
        snap = h.controller.snapshot()
        assert snap["pending_permissions"], "等待不能自动过期或自动批准"
        assert h.run_state(run.run_id) == "WAITING_PERMISSION"
        perm = _pending_permission(h)
        h.controller.answer_permission(perm["permission_id"], "reject")
        assert h.wait_terminal(run.run_id) == "SETTLED"
    finally:
        h.close()


def test_cancel_during_permission(harness):
    """许可等待中取消：callback 回 cancelled，prompt 结算 cancelled。"""
    h = harness(PERM_SCRIPT)
    try:
        conv = h.new_conversation()
        run = h.submit(conv, "写一下")
        assert wait_until(lambda: h.run_state(run.run_id) == "WAITING_PERMISSION", 10)
        assert h.controller.cancel(run.run_id)
        assert h.wait_terminal(run.run_id, timeout=15) == "CANCELLED"
        snap = h.controller.snapshot()
        assert not snap["pending_permissions"]
        # 迟到回答无效
        perm_records = h.store.pending_permissions()
        assert not perm_records
    finally:
        h.close()


def test_cancel_before_session(harness):
    """PREPARING 段取消：不发 prompt；若会话已建则清理，不让迟到 prompt 入队。"""
    script = {
        "turns": [
            {"updates": [{"kind": "user_echo"}, {"kind": "agent_text", "text": "不应执行"}],
             "stop_reason": "end_turn"}
        ]
    }
    h = harness(script)
    try:
        conv = h.new_conversation()
        run = h.submit(conv, "立刻取消")
        assert h.controller.cancel(run.run_id)
        assert wait_until(lambda: h.run_state(run.run_id) == "CANCELLED", 10)
        time.sleep(0.8)  # 若竞态漏网，迟到 prompt 会产生 user_echo
        echoes = h.events_of(conv.conversation_id, "user_echo")
        assert not echoes, "PREPARING 段取消后不应有任何 prompt 执行痕迹"
    finally:
        h.close()


def test_cancel_during_execution(harness):
    """执行中取消：先 CANCELLING，结算后 CANCELLED（非点击即停）。"""
    script = {
        "turns": [
            {
                "updates": [
                    {"kind": "agent_text", "text": "开始"},
                    {"kind": "silent", "seconds": 20},
                    {"kind": "agent_text", "text": "不应到达"},
                ],
                "stop_reason": "end_turn",
            }
        ],
        "on_cancel": {"settle_after": 0.5, "stop_reason": "cancelled"},
    }
    h = harness(script)
    try:
        conv = h.new_conversation()
        run = h.submit(conv, "长任务")
        assert wait_until(lambda: h.run_state(run.run_id) == "RUNNING", 10)
        h.controller.cancel(run.run_id)
        assert wait_until(lambda: h.run_state(run.run_id) == "CANCELLING", 5)
        assert h.wait_terminal(run.run_id, timeout=20) == "CANCELLED"
        texts = "".join(
            e.payload.get("text", "") for e in h.events_of(conv.conversation_id, "assistant")
        )
        assert "不应到达" not in texts
    finally:
        h.close()


def test_cancel_after_terminal_is_noop(harness):
    h = harness(TWO_TURN_SCRIPT)
    try:
        conv = h.new_conversation()
        run = h.submit(conv, "第一问")
        assert h.wait_terminal(run.run_id) == "SETTLED"
        assert h.controller.cancel(run.run_id)  # 幂等
        assert h.run_state(run.run_id) == "SETTLED"
    finally:
        h.close()


def test_cancel_race_completion_wins(harness):
    """取消与完成竞争：按真实终态记录，不把已结算回复改成 CANCELLED。"""
    script = {
        "turns": [
            {
                "updates": [
                    {"kind": "agent_text", "text": "马上完成"},
                ],
                "stop_reason": "end_turn",
            }
        ],
        "on_cancel": {"settle_after": 5.0, "stop_reason": "cancelled"},
    }
    h = harness(script)
    try:
        conv = h.new_conversation()
        run = h.submit(conv, "快速任务")
        wait_until(lambda: h.run_state(run.run_id) == "RUNNING", 10)
        h.controller.cancel(run.run_id)
        final = h.wait_terminal(run.run_id, timeout=15)
        assert final == "SETTLED", "完成先到，保留真实终态"
        stored = h.store.get_run(run.run_id)
        assert stored.stop_reason == "end_turn"
    finally:
        h.close()


def test_single_active_run_gate(harness):
    h = harness(TWO_TURN_SCRIPT)
    try:
        conv = h.new_conversation()
        run = h.submit(conv, "第一问")
        with pytest.raises(ControllerError) as ei:
            h.submit(conv, "插队")
        assert ei.value.code == "run_in_progress"
        assert h.wait_terminal(run.run_id) == "SETTLED"
        # 终态后可继续
        run2 = h.submit(conv, "第二问")
        assert h.wait_terminal(run2.run_id) == "SETTLED"
    finally:
        h.close()


# ---------------- 恢复（08 票前置行为在 02 层的基线） ----------------

def test_new_controller_marks_unsettled_interrupted(tmp_path):
    script = {
        "turns": [
            {
                "updates": [
                    {"kind": "agent_text", "text": "开始"},
                    {"kind": "silent", "seconds": 30},
                ],
                "stop_reason": "end_turn",
            },
            {
                "match": "恢复",
                "updates": [{"kind": "agent_text", "text": "恢复后的回复"}],
                "stop_reason": "end_turn",
            },
        ]
    }
    h1 = Harness(tmp_path, script)
    conv = h1.new_conversation()
    run = h1.submit(conv, "会被中断的任务")
    assert wait_until(lambda: h1.run_state(run.run_id) == "RUNNING", 10)
    # 模拟主进程崩溃：不 shutdown 直接弃置（线程为 daemon）
    h1.store.close()

    # 新控制器（同库）启动：未结算 → INTERRUPTED，不自动重发
    h2 = Harness(tmp_path, script)
    try:
        snap = h2.controller.snapshot()
        found = [r for r in snap["runs"] if r["run_id"] == run.run_id]
        assert found and found[0]["state"] == "INTERRUPTED"
        assert not snap["active_run"]
        # 显式恢复：同 cwd 同 sessionId
        conv2 = h2.store.get_conversation(conv.conversation_id)
        assert conv2.dsh_session_id
        assert h2.controller.resume(conv.conversation_id)
        run2 = h2.submit(conv, "恢复后续问")
        assert h2.wait_terminal(run2.run_id) == "SETTLED"
        # 恢复续问仍用原 sessionId
        assert (
            h2.store.get_conversation(conv.conversation_id).dsh_session_id
            == conv2.dsh_session_id
        )
    finally:
        h2.close()


def test_resume_rejects_cwd_mismatch(harness):
    """恢复时 cwd 已不存在给具体错误（cwd_missing），不静默 new_session。

    复核 R7 后该拒绝前移到控制器身份校验（与发送自动恢复共用），不再
    依赖 runtime 侧拒绝。
    """
    h = harness(TWO_TURN_SCRIPT)
    try:
        conv = h.new_conversation()
        run = h.submit(conv, "第一问")
        assert h.wait_terminal(run.run_id) == "SETTLED"
        # 篡改 cwd 模拟目录变化
        h.controller._conversations[conv.conversation_id].cwd = str(h.tmp / "不存在的目录")
        with pytest.raises(ControllerError) as ei:
            h.controller.resume(conv.conversation_id)
        assert ei.value.code == "cwd_missing"
        assert not h.controller.snapshot()["active_run"]
    finally:
        h.close()


class TestSubmitGuards:
    """复核 F03/F04：幂等前移不丢生命周期/会话校验；并发同 run_id 收敛。"""

    def _ctl(self, tmp_path):
        st = CopilotStore(tmp_path / "copilot.sqlite3")
        ctl = CopilotController(
            executable=PYTHON, runtime_args=[], dsh_home=str(tmp_path),
            launch_cwd=str(REPO_ROOT), store=st, idle_shutdown_seconds=0)
        ctl.start = lambda: None  # 不拉起事件循环；本组只测同步受理路径
        return ctl, st

    @pytest.fixture()
    def no_dispatch(self, monkeypatch):
        import asyncio
        import concurrent.futures

        def ignore(coro, loop):
            coro.close()
            return concurrent.futures.Future()

        monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", ignore)

    def test_unknown_conversation_rejected_before_save(self, tmp_path, no_dispatch):
        ctl, st = self._ctl(tmp_path)
        with pytest.raises(ControllerError) as ei:
            ctl.submit("does-not-exist", "文本")
        assert ei.value.code == "unknown_conversation"
        assert st.all_runs() == [], "未知会话不得落库为受理"
        st.close()

    def test_shutting_down_rejected(self, tmp_path, no_dispatch):
        ctl, st = self._ctl(tmp_path)
        conv = ctl.create_conversation(str(tmp_path))
        ctl.shutdown()
        with pytest.raises(ControllerError) as ei:
            ctl.submit(conv.conversation_id, "文本")
        assert ei.value.code == "shutting_down"
        assert st.all_runs() == [], "关闭后的提交不得受理"
        st.close()

    def test_concurrent_same_run_id_converges(self, tmp_path, no_dispatch,
                                              monkeypatch):
        """F04：两线程同 run_id 并发受理——都返回同一条，不得误报 run_in_progress。"""
        import concurrent.futures

        ctl, st = self._ctl(tmp_path)
        conv = ctl.create_conversation(str(tmp_path))
        ref = tmp_path / "same-ref.txt"
        ref.write_text("x", encoding="utf-8")
        barrier = threading.Barrier(2)
        orig_exists = Path.exists

        def pause_exists(p):
            if getattr(p, "name", "") == "same-ref.txt":
                barrier.wait(timeout=5)  # 两线程同时越过首段查图后在此汇合
            return orig_exists(p)

        monkeypatch.setattr(Path, "exists", pause_exists)
        outcomes = []

        def go():
            try:
                r = ctl.submit(conv.conversation_id, "文本",
                               context_refs=[str(ref)], run_id="same-run")
                outcomes.append(r.run_id)
            except Exception as exc:
                outcomes.append(getattr(exc, "code", type(exc).__name__))

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            fs = [pool.submit(go) for _ in range(2)]
            [f.result(timeout=10) for f in fs]
        assert outcomes == ["same-run", "same-run"], \
            "并发同 run_id 必须幂等收敛，不得一条 run_in_progress"
        assert len({r.run_id for r in st.all_runs()}) == 1
        st.close()
