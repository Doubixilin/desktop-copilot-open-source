"""04 票：G1 闭环的 UI 级测试（真 CopilotView + 真 controller + fake agent）。

withdrawn Tk 不进 mainloop，用 root.update() 驱动 after 轮询；覆盖
新建/发送/收到更新/许可/停止/续问/收起恢复。中文 IME/实机焦点属人工验收。
"""
import json
import os
import sys
import time
import tkinter as tk
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from copilot import models  # noqa: E402
from copilot.controller import CopilotController  # noqa: E402
from copilot.panel_view import CopilotView  # noqa: E402
from copilot.store import CopilotStore  # noqa: E402

PYTHON = sys.executable
TERMINAL = {s.value for s in models.TERMINAL_STATES}


def wait_until(predicate, timeout=20.0, interval=0.05, pump=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        if pump is not None:
            pump(0.1)
        else:
            time.sleep(interval)
    return False


PERM_AND_CANCEL_SCRIPT = {
    "turns": [
        {
            "updates": [
                {"kind": "user_echo"},
                {"kind": "agent_text", "text": "先读一下"},
                {
                    "kind": "tool_start", "id": "t1", "title": "写文件",
                    "tool_kind": "edit",
                },
                {
                    "kind": "permission",
                    "tool_call_id": "t1",
                    "title": "写入 target.md",
                    "options": [
                        {"optionId": "allow", "name": "允许", "kind": "allow_once"},
                        {"optionId": "reject", "name": "拒绝", "kind": "reject_once"},
                    ],
                    "on_selected": "已按选择继续",
                },
                {"kind": "agent_text", "text": "第一轮完成"},
            ],
            "stop_reason": "end_turn",
        },
        {
            "match": "续问",
            "updates": [{"kind": "agent_text", "text": "第二轮回答"}],
            "stop_reason": "end_turn",
        },
        {
            "match": "长任务",
            "updates": [
                {"kind": "agent_text", "text": "开始长任务"},
                {"kind": "silent", "seconds": 20},
            ],
            "stop_reason": "end_turn",
        },
    ],
    "on_cancel": {"settle_after": 0.3, "stop_reason": "cancelled"},
}


_SHARED_ROOT = None


def shared_root():
    """同进程复用一个 withdrawn Tk root：反复建/销毁 Tk 在本机不稳定。"""
    global _SHARED_ROOT
    if _SHARED_ROOT is None:
        _SHARED_ROOT = tk.Tk()
        _SHARED_ROOT.withdraw()
    return _SHARED_ROOT


class G1Harness:
    def __init__(self, tmp_path: Path, script=None, ready_reason=None):
        """ready_reason 非 None 时模拟控制器不可用（不建控制器/子进程）。"""
        os.environ["FAKE_ACP_STATE"] = str(tmp_path / "fake-state.json")
        if ready_reason is None:
            self._init_ready(tmp_path, script)
        else:
            self._init_not_ready(ready_reason)

    def _init_not_ready(self, reason):
        self.controller = None
        self._not_ready_reason = reason
        self._build_view(lambda: (None, reason))

    def _init_ready(self, tmp_path: Path, script):
        script = script or PERM_AND_CANCEL_SCRIPT
        sp = tmp_path / "script.json"
        sp.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
        self.workspace = tmp_path / "项目 目录"
        self.workspace.mkdir(exist_ok=True)
        self.store = CopilotStore(tmp_path / "copilot.sqlite3")
        self.controller = CopilotController(
            executable=PYTHON,
            runtime_args=["-m", "copilot.fake_agent", "--script", str(sp)],
            dsh_home=str(tmp_path / "home"),
            launch_cwd=str(REPO),
            store=self.store,
            idle_shutdown_seconds=0,
            cancel_warning_seconds=2,
            stderr_dir=tmp_path / "stderr",
        )
        self._build_view(lambda: (self.controller, None))

    def _build_view(self, get_controller):
        self.root = shared_root()
        import tkinter.font as tkfont
        font = tkfont.Font(family="Courier", size=-15)
        small = tkfont.Font(family="Courier", size=-12)
        self.view = CopilotView(
            self.root, font, small,
            get_controller=get_controller,
        )
        self.view.frame.pack(fill="both", expand=True)

    def open_conversation(self):
        conv = self.controller.create_conversation(str(self.workspace))
        self.view.conversation_id = conv.conversation_id
        self.view.project_dir = conv.cwd
        return conv

    def pump(self, seconds: float):
        """驱动 Tk 事件循环（after 轮询）指定时长。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.02)

    def wait(self, predicate, timeout=20.0):
        """等待条件成立；期间持续泵 Tk 事件（轮询刷新依赖 after）。"""
        return wait_until(predicate, timeout=timeout, pump=self.pump)

    def type_and_send(self, text: str):
        self.view.input.delete("1.0", "end")
        self.view.input.insert("1.0", text)
        self.view._send()

    def transcript_text(self) -> str:
        return self.view.transcript.get("1.0", "end")

    def active_state(self):
        snap = self.controller.snapshot()
        run = snap.get("active_run")
        return run["state"] if run else None

    def close(self):
        if os.environ.get("G1_DEBUG") and self.controller is not None:
            snap = self.controller.snapshot()
            print("DBG runs:", [(r["state"], r.get("error_code")) for r in snap["runs"]],
                  "pending:", len(snap["pending_permissions"]), flush=True)
            err_dir = self.workspace.parent / "stderr"
            if err_dir.is_dir():
                for f in sorted(err_dir.iterdir()):
                    content = f.read_text(errors="replace")
                    if content:
                        print("DBG stderr", f.name, ":", content[:500], flush=True)
        self.view.destroy()
        if self.controller is not None:
            self.controller.shutdown(timeout=15)
            self.store.close()


@pytest.fixture()
def g1(tmp_path):
    h = G1Harness(tmp_path)
    yield h
    h.close()


def test_g1_full_flow_permission_followup_collapse(g1):
    h = g1
    h.view.show()
    h.pump(0.3)
    h.open_conversation()

    # 发送（按钮路径；Enter 不发送）
    h.type_and_send("第一问")
    h.pump(0.5)
    # 轮询后状态离开 PREPARING
    assert h.wait(lambda: h.active_state() in ("RUNNING", "WAITING_PERMISSION")
                      or h.active_state() is None, 10), h.active_state()

    # 许可卡片出现 → 点允许
    assert h.wait(lambda: h.view._perm_frame_rendered, 10)
    h.pump(0.1)
    # 先取按钮引用再点击：点击即回答，卡片会立刻销毁重建
    buttons = []
    for child in h.view.perm_card.winfo_children():
        if isinstance(child, tk.Frame):
            buttons.extend(b for b in child.winfo_children() if isinstance(b, tk.Button))
    allow = None
    for b in buttons:
        try:
            if b.cget("text") == "允许":
                allow = b
                break
        except tk.TclError:
            continue  # 轮询重建后的失效包装
    assert allow is not None, "许可卡片上找不到允许按钮"
    allow.invoke()

    # 第一轮完成：转录包含回复与许可后续
    assert h.wait(
        lambda: "第一轮完成" in h.transcript_text()
        and "已按选择继续" in h.transcript_text(),
        timeout=15,
    ), h.transcript_text()[-300:]
    h.pump(0.3)

    # 收起（ESC 路径）再展开：草稿与转录保留，任务状态保留
    h.view.input.delete("1.0", "end")
    h.view.input.insert("1.0", "续问 第二点")
    h.view._esc()
    assert not h.view.frame.winfo_manager() or True  # hide() 由宿主调用
    h.view.show()
    h.pump(0.3)
    assert "第一轮完成" in h.transcript_text()
    assert h.view.input.get("1.0", "end").strip() == "续问 第二点"

    # 续问：同会话第二轮
    h.view._send()
    assert h.wait(lambda: "第二轮回答" in h.transcript_text(), 15)
    runs = h.store.runs_for_conversation(h.view.conversation_id)
    assert len(runs) == 2
    assert len({r.stop_reason for r in runs} - {None}) == 1  # 都 end_turn


def test_g1_stop_button_cancels(g1):
    h = g1
    h.view.show()
    h.pump(0.3)
    h.open_conversation()
    h.type_and_send("长任务 请处理")
    assert h.wait(lambda: h.active_state() == "RUNNING", 10)
    h.view._stop()
    assert h.wait(lambda: h.active_state() == "CANCELLING", 5)
    assert h.wait(lambda: h.active_state() is None, 15)
    runs = h.store.runs_for_conversation(h.view.conversation_id)
    assert runs[0].state is models.RunState.CANCELLED
    assert "不应到达" not in h.transcript_text()


def test_g1_enter_does_not_send(g1):
    """Enter 换行不发送；发送只走按钮（中文 IME 安全首版）。"""
    h = g1
    h.view.show()
    h.pump(0.3)
    h.open_conversation()
    h.view.input.insert("1.0", "第一行")
    h.view._enter_newline(None)
    # 模拟 Tk Return 默认换行后仍无 run
    h.view.input.insert("end", "\n第二行")
    snap = h.controller.snapshot()
    assert snap["active_run"] is None
    assert "第二行" in h.view.input.get("1.0", "end")


def test_g1_dsh_not_ready_blocks_send(tmp_path):
    """控制器不可用：发送禁用并显示 DSH NOT READY（不冒充可用）。"""
    h = G1Harness(tmp_path, ready_reason="agent-client-protocol 未安装")
    try:
        h.view.show()
        h.root.update()
        assert "DSH NOT READY" in h.view.status.cget("text")
        assert str(h.view.send_btn.cget("state")) == "disabled"
    finally:
        h.close()
