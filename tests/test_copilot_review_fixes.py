"""复核 2026-09-27（review-2026-09-27/report.md）R1–R9 修复的回归测试。

七个用例先以"复现缺陷"形态存在于复核目录，修复后纳入正式测试并保留
原断言（不删断言换绿）；另补 R1 真实事件派发（tab/输入/发送/许可按钮）、
R3 缺详情许可卡片、R8 Tk 心跳与 R9 服务退出路径的集成测试，以及第二轮
复核（recheck-02）S1–S3 的取消/重启边界回归。

已知环境现象：Windows 上同一 pytest 进程反复创建/销毁 Tk root 偶发
"couldn't read init.tcl"（复核报告同见）；失败项单独重跑即过，属环境
脆弱性，不判为产品缺陷。
"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from copilot import models  # noqa: E402
from copilot.controller import RECOVERY_EVENT_WINDOW, CopilotController  # noqa: E402
from copilot.store import CopilotStore  # noqa: E402


def make_controller(tmp_path, home="home"):
    store = CopilotStore(tmp_path / "review.sqlite3")
    ctrl = CopilotController(
        executable=sys.executable, runtime_args=[], dsh_home=str(tmp_path / home),
        launch_cwd=str(ROOT), store=store, idle_shutdown_seconds=0,
    )
    conv = ctrl.create_conversation(str(tmp_path))
    return ctrl, conv, store


def seed_run(ctrl, conv):
    run = models.Run(run_id="review-run", conversation_id=conv.conversation_id,
                     state=models.RunState.PREPARING,
                     submitted_at=models.now(), last_event_at=models.now(), user_text="synthetic")
    ctrl._runs[run.run_id] = run
    ctrl._store.save_run(run)
    return run


# ---------------- R2：准备阶段取消绝不发送 prompt ----------------

def test_cancel_while_applying_config_never_sends_prompt(tmp_path):
    ctrl, conv, store = make_controller(tmp_path)
    conv.selected_config = {"model": "synthetic-model"}
    run = seed_run(ctrl, conv)
    sent = []

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class Connection:
            async def new_session(self, **kw):
                return SimpleNamespace(session_id="review-session", config_options=[])

            async def set_config_option(self, **kw):
                entered.set()
                await release.wait()

            async def prompt(self, *args):
                sent.append("prompt")
                return SimpleNamespace(stop_reason="end_turn")

        async def runtime():
            return SimpleNamespace(connection=Connection())

        ctrl._ensure_runtime = runtime
        task = asyncio.create_task(ctrl._execute_run(run.run_id))
        await asyncio.wait_for(entered.wait(), 2)
        ctrl.cancel(run.run_id)
        assert run.state == models.RunState.CANCELLED
        release.set()
        await asyncio.wait_for(task, 2)

    try:
        asyncio.run(scenario())
        assert sent == [], f"cancelled run still sent {sent}, final state={run.state}"
        assert run.state == models.RunState.CANCELLED  # 终态不被迟到任务复活
    finally:
        store.close()


# ---------------- R3：许可关联前序工具详情 ----------------

def test_permission_joins_previous_tool_details(tmp_path):
    ctrl, conv, store = make_controller(tmp_path)
    run = seed_run(ctrl, conv)
    ctrl._session_to_run["review-session"] = run.run_id
    ctrl._process_update("review-session", SimpleNamespace(
        session_update="tool_call", tool_call_id="call-1", title="pwsh",
        kind="execute", status="in_progress", content=None,
        raw_input={"command": "synthetic operation"},
    ))

    async def scenario():
        task = asyncio.create_task(ctrl._on_request_permission(
            "review-session", SimpleNamespace(tool_call_id="call-1"),
            [SimpleNamespace(option_id="allow-once", name="Allow once", kind="allow_once")],
        ))
        await asyncio.sleep(0)
        perm = next(iter(ctrl._permissions.values()))
        next(iter(ctrl._perm_futures.values())).set_result(("cancelled", None))
        await task
        return perm

    try:
        perm = asyncio.run(scenario())
        assert (perm.tool_title, perm.raw_input) == ("pwsh", {"command": "synthetic operation"})
    finally:
        store.close()


def test_late_tool_update_fills_pending_permission_details(tmp_path):
    """许可先到、详情后到：pending 许可被补齐并通知。"""
    ctrl, conv, store = make_controller(tmp_path)
    run = seed_run(ctrl, conv)
    ctrl._session_to_run["review-session"] = run.run_id
    notified = []
    ctrl.subscribe(lambda kind: notified.append(kind))

    async def scenario():
        task = asyncio.create_task(ctrl._on_request_permission(
            "review-session", SimpleNamespace(tool_call_id="call-2"),
            [SimpleNamespace(option_id="allow-once", name="Allow once", kind="allow_once")],
        ))
        await asyncio.sleep(0)
        ctrl._process_update("review-session", SimpleNamespace(
            session_update="tool_call", tool_call_id="call-2", title="bash",
            kind="execute", status="in_progress", content=None,
            raw_input={"command": "late details"},
        ))
        perm = ctrl._permissions[next(iter(ctrl._permissions))]
        next(iter(ctrl._perm_futures.values())).set_result(("cancelled", None))
        await task
        return perm

    try:
        perm = asyncio.run(scenario())
        assert perm.tool_title == "bash"
        assert perm.raw_input == {"command": "late details"}
        assert "permission_details" in notified
    finally:
        store.close()


# ---------------- R5：重启后最新历史可见 ----------------

def test_restart_does_not_hide_latest_history(tmp_path):
    ctrl, conv, store = make_controller(tmp_path)
    run = seed_run(ctrl, conv)
    run.state = models.RunState.SETTLED
    store.save_run(run)
    for i in range(620):
        store.append_event(models.EventRecord(
            seq=0, run_id=run.run_id, conversation_id=conv.conversation_id,
            runtime_epoch=1, kind="assistant", payload={"text": f"saved-{i}"},
            observed_at=models.now(),
        ))
    try:
        restored = CopilotController(
            executable=sys.executable, runtime_args=[], dsh_home=str(tmp_path / "home"),
            launch_cwd=str(ROOT), store=store, idle_shutdown_seconds=0,
        )
        texts = [e.payload.get("text") for e in restored.events(conv.conversation_id)]
        assert "saved-619" in texts, f"recovered={len(texts)} last={texts[-1]}"
    finally:
        store.close()


def test_recovery_reports_truncation_and_restores_seq_watermark(tmp_path):
    """超出恢复窗口时以 note 明示截断；新事件 seq 接全局高水位不回退。"""
    ctrl, conv, store = make_controller(tmp_path)
    run = seed_run(ctrl, conv)
    run.state = models.RunState.SETTLED
    store.save_run(run)
    total = RECOVERY_EVENT_WINDOW + 50
    for i in range(total):
        store.append_event(models.EventRecord(
            seq=0, run_id=run.run_id, conversation_id=conv.conversation_id,
            runtime_epoch=1, kind="assistant", payload={"text": f"saved-{i}"},
            observed_at=models.now(),
        ))
    try:
        restored = CopilotController(
            executable=sys.executable, runtime_args=[], dsh_home=str(tmp_path / "home"),
            launch_cwd=str(ROOT), store=store, idle_shutdown_seconds=0,
        )
        ring = restored.events(conv.conversation_id)
        notes = [e for e in ring if e.kind == "note"]
        assert notes and "未载入" in notes[0].payload.get("text", "")
        texts = [e.payload.get("text") for e in ring]
        assert f"saved-{total - 1}" in texts
        assert restored._seq_counter == total  # 高水位：后续 seq 不与库内重复
        restored._append_event_public(run.run_id, "note", {"text": "after restart"})
        reloaded = store.events_for_conversation(conv.conversation_id, tail=True,
                                                 limit=total + 10)
        assert reloaded[-1].payload.get("text") == "after restart"
        assert reloaded[-1].seq > total
    finally:
        store.close()


# ---------------- R6：close() 统一收尾 Job 句柄 ----------------

def test_graceful_close_releases_job_handle():
    from copilot import acp_adapter
    handle = acp_adapter._create_kill_on_close_job()
    if handle is None:
        pytest.skip("Windows Job API unavailable")

    class Conn:
        async def close(self):
            pass

    class Proc:
        stdin = None

        async def wait(self):
            return 0

    spawned = acp_adapter.SpawnedAgent(connection=Conn(), process=Proc(),
                                       executable="synthetic", job_handle=handle)
    try:
        asyncio.run(spawned.close())
        assert spawned.job_handle is None, "normal-exit branch leaks the owned Job handle"
    finally:
        if spawned.job_handle is not None:
            acp_adapter._kernel32.CloseHandle(handle)


# ---------------- R7：发送自动恢复与显式 resume 共用身份校验 ----------------

def test_submit_path_checks_original_home_before_resume(tmp_path):
    ctrl, conv, store = make_controller(tmp_path, "new-home")
    conv.dsh_session_id = "prior-session"
    conv.dsh_home = str(tmp_path / "original-home")
    called = []

    class Conn:
        async def resume_session(self, **kw):
            called.append(kw)

    try:
        with pytest.raises(Exception):
            asyncio.run(ctrl._ensure_session(SimpleNamespace(connection=Conn()), conv))
        assert called == [], "submit auto-resume bypasses the home guard on explicit resume()"
    finally:
        store.close()


def test_submit_marks_run_failed_on_home_mismatch(tmp_path):
    """自动恢复被拒后轮次显式失败，不静默另建会话。"""
    ctrl, conv, store = make_controller(tmp_path)
    conv.dsh_session_id = "prior-session"
    conv.dsh_home = str(tmp_path / "another-home")
    started = []

    async def new_session(**kw):
        started.append(kw)
        return SimpleNamespace(session_id="sneaky-session", config_options=[])

    async def runtime():
        return SimpleNamespace(connection=SimpleNamespace(new_session=new_session))

    async def scenario():
        ctrl._ensure_runtime = runtime
        await ctrl._execute_run("review-run")

    run = seed_run(ctrl, conv)
    try:
        asyncio.run(scenario())
        assert run.state == models.RunState.FAILED
        assert run.error_code == "session_resume_failed"
        assert started == []  # 没有悄悄 new_session 另起一段对话
    finally:
        store.close()


# ---------------- R4：断线后有界清理 owned 进程 ----------------

def test_broken_connection_does_not_orphan_live_owned_process(tmp_path):
    from copilot import acp_adapter
    ctrl, conv, store = make_controller(tmp_path)
    run = seed_run(ctrl, conv)
    run.state = models.RunState.RUNNING
    run.prompt_written = True

    async def scenario():
        spawned = await acp_adapter.spawn_agent_subprocess(
            sys.executable, ["-c", "import os,time; os.close(1); time.sleep(30)"],
            skip_initialize=True, stderr_path=tmp_path / "synthetic-stderr.log",
        )
        ctrl._spawned = spawned
        try:
            await ctrl._handle_prompt_failure(run.run_id, BrokenPipeError("synthetic pipe loss"))
            assert ctrl._spawned is None  # 引用已清，新提交可另起 runtime
            assert run.state == models.RunState.INTERRUPTED  # 断线轮次不重发
            assert spawned.process.returncode is not None, (
                "connection error discarded controller ownership while the process stayed alive"
            )
        finally:
            await spawned.close(shutdown_timeout=0.1)
            spawned.close_stderr_file()
            if spawned.job_handle is not None:
                acp_adapter._kernel32.CloseHandle(spawned.job_handle)
                spawned.job_handle = None

    try:
        asyncio.run(scenario())
    finally:
        store.close()


# ---------------- S1/S2/S3：第二轮复核的取消/重启边界（recheck-02） ----------------

def test_cancel_during_new_session_can_send_next_turn(tmp_path):
    """S1：新建会话等待期间取消——会话被关闭且转非 live，下一轮 resume 成功。"""
    ctrl, conv, store = make_controller(tmp_path)
    first = seed_run(ctrl, conv)
    sent = []

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class Connection:
            live = False

            async def new_session(self, **kw):
                entered.set()
                await release.wait()
                self.live = True
                return SimpleNamespace(session_id="session", config_options=[])

            async def close_session(self, session_id):
                self.live = False

            async def resume_session(self, **kw):
                self.live = True

            async def prompt(self, *args):
                if not self.live:
                    raise RuntimeError("session is not active; resume required")
                sent.append(args[0])
                return SimpleNamespace(stop_reason="end_turn")

        connection = Connection()

        async def runtime():
            return SimpleNamespace(connection=connection)

        ctrl._ensure_runtime = runtime
        task = asyncio.create_task(ctrl._execute_run(first.run_id))
        await asyncio.wait_for(entered.wait(), 2)
        ctrl.cancel(first.run_id)
        release.set()
        await asyncio.wait_for(task, 2)
        assert first.state == models.RunState.CANCELLED
        assert sent == []
        assert conv.session_live is False, "取消清理未把刚建的会话转非 live"
        second = models.Run(
            run_id="second", conversation_id=conv.conversation_id,
            state=models.RunState.PREPARING, submitted_at=models.now(),
            last_event_at=models.now(), user_text="second synthetic task",
        )
        ctrl._runs[second.run_id] = second
        store.save_run(second)
        await ctrl._execute_run(second.run_id)
        assert second.state == models.RunState.SETTLED, (
            f"next turn={second.state}; session_live={conv.session_live}; "
            f"error={second.error_detail}"
        )
        assert sent == ["session"]

    try:
        asyncio.run(scenario())
    finally:
        store.close()


def test_late_config_failure_preserves_cancelled_terminal(tmp_path):
    """S2：配置 RPC 迟到报错不得把已取消的终态改成 FAILED。"""
    ctrl, conv, store = make_controller(tmp_path)
    run = seed_run(ctrl, conv)
    conv.selected_config = {"model": "synthetic"}

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class Connection:
            async def new_session(self, **kw):
                return SimpleNamespace(session_id="session", config_options=[])

            async def set_config_option(self, **kw):
                entered.set()
                await release.wait()
                raise RuntimeError("late configuration failure")

        async def runtime():
            return SimpleNamespace(connection=Connection())

        ctrl._ensure_runtime = runtime
        task = asyncio.create_task(ctrl._execute_run(run.run_id))
        await asyncio.wait_for(entered.wait(), 2)
        ctrl.cancel(run.run_id)
        assert run.state == models.RunState.CANCELLED
        release.set()
        await asyncio.wait_for(task, 2)
        assert run.state == models.RunState.CANCELLED, run.error_detail
        assert run.error_code is None

    try:
        asyncio.run(scenario())
    finally:
        store.close()


def test_replacing_exited_runtime_cleans_old_and_resumes_session(tmp_path, monkeypatch):
    """S3：旧 runtime 进程已退出——重建前回收旧资源并失效旧会话，随后 resume。"""
    from copilot import acp_adapter
    ctrl, conv, store = make_controller(tmp_path)
    conv.dsh_session_id = "saved-session"
    conv.session_live = True
    calls = []

    class OldRuntime:
        process = SimpleNamespace(returncode=1)

        async def close(self, **kw):
            calls.append("close_old")

    class NewConnection:
        async def resume_session(self, **kw):
            calls.append("resume")

    fresh = SimpleNamespace(process=SimpleNamespace(returncode=None),
                            connection=NewConnection())

    async def spawn(*args, **kw):
        calls.append("spawn")
        return fresh

    monkeypatch.setattr(acp_adapter, "spawn_agent_subprocess", spawn)
    ctrl._spawned = OldRuntime()

    async def scenario():
        ctrl._runtime_lock = asyncio.Lock()
        spawned = await ctrl._ensure_runtime()
        await ctrl._ensure_session(spawned, conv)
        assert "close_old" in calls and "resume" in calls, (
            f"operations={calls}; stale session_live={conv.session_live}"
        )
        assert conv.session_live is True  # resume 成功后才回到 live

    try:
        asyncio.run(scenario())
    finally:
        store.close()


def test_next_turn_after_runtime_exit_recovers_and_settles(tmp_path, monkeypatch):
    """S3 全链路：上一轮结束后 runtime 意外退出，下一轮同一对话照常完成。"""
    from copilot import acp_adapter
    ctrl, conv, store = make_controller(tmp_path)
    conv.dsh_session_id = "saved-session"
    conv.session_live = True
    calls = []

    class OldRuntime:
        process = SimpleNamespace(returncode=1)

        async def close(self, **kw):
            calls.append("close_old")

    class NewConnection:
        async def resume_session(self, **kw):
            calls.append("resume")

        async def prompt(self, session_id, blocks):
            calls.append("prompt")
            return SimpleNamespace(stop_reason="end_turn")

    async def spawn(*args, **kw):
        calls.append("spawn")
        return SimpleNamespace(process=SimpleNamespace(returncode=None),
                               connection=NewConnection())

    monkeypatch.setattr(acp_adapter, "spawn_agent_subprocess", spawn)
    ctrl._spawned = OldRuntime()
    run = seed_run(ctrl, conv)

    async def scenario():
        ctrl._runtime_lock = asyncio.Lock()
        await ctrl._execute_run(run.run_id)

    try:
        asyncio.run(scenario())
        assert run.state == models.RunState.SETTLED, (
            f"state={run.state} error={run.error_detail}"
        )
        assert calls == ["close_old", "spawn", "resume", "prompt"]
    finally:
        store.close()


# ---------------- R1 / R3 UI / R8：真实 Tk 事件派发 ----------------

class StubController:
    """面板层测试桩：覆盖 snapshot/events/submit/answer_permission 面。"""

    def __init__(self):
        self.submitted = []
        self.answered = []
        self.pending = []

    def snapshot(self):
        return {
            "conversations": [], "runs": [], "active_run": None,
            "pending_permissions": list(self.pending), "store_healthy": True,
            "runtime": {"spawned": False, "epoch": 0, "executable": ""},
        }

    def events(self, *a, **kw):
        return []

    def take_notifications(self):
        return []

    def submit(self, conversation_id, text, context_refs=None):
        self.submitted.append((conversation_id, text, list(context_refs or [])))
        return SimpleNamespace(run_id="run-stub-0001")

    def answer_permission(self, permission_id, option_id):
        self.answered.append((permission_id, option_id))
        self.pending = [p for p in self.pending
                        if p["permission_id"] != permission_id]
        return True


def _perm(permission_id, title, raw, options):
    return {
        "permission_id": permission_id, "run_id": "run-x",
        "conversation_id": "conv-x", "tool_call_id": "call-x",
        "tool_title": title, "tool_kind": "execute", "raw_input": raw,
        "options": options, "state": "pending", "resolved_option": None,
        "created_at": time.time(),
    }


def build_panel(monkeypatch, root, stub):
    """真实 run() 装配（含全部事件绑定），mainloop 打桩为直接返回。"""
    import tkinter as tk
    import search_panel as sp
    from copilot import runtime_host
    monkeypatch.setattr(runtime_host, "copilot_tab_available", lambda: (True, ""))
    monkeypatch.setattr(runtime_host, "panel_mode", lambda: "copilot")
    monkeypatch.setattr(runtime_host, "get_controller", lambda: (stub, None))
    monkeypatch.setattr(tk, "Tk", lambda: root)
    monkeypatch.setattr(root, "mainloop", lambda: None)
    app = sp.SearchPanelApp()
    app.run()
    root.geometry("600x650+30000+30000")  # 屏幕外合成窗口：可派发真实事件
    root.deiconify()
    root.update()
    return app


def _collect_buttons(container):
    import tkinter as tk
    out = []
    for child in container.winfo_children():
        if isinstance(child, tk.Button):
            out.append(child)
        else:
            out.extend(_collect_buttons(child))
    return out


@pytest.fixture()
def tk_root():
    import tkinter as tk
    root = tk.Tk()
    yield root
    root.destroy()


def test_real_click_opens_and_keeps_copilot_tab(monkeypatch, tk_root):
    """R1：点击 COPILOT tab 后，root 绑定不得把视图立刻切回文件搜索。

    点击与断言无条件执行（复核 T1：guard 包住主体会让首建视图前
    _copilot_view 为 None 的正常初态空跑整个测试）；仅 finally 清理判空。
    """
    stub = StubController()
    app = build_panel(monkeypatch, tk_root, stub)
    try:
        assert app._view == "idle"
        assert app._copilot_view is None  # 首次进入副驾前视图未创建
        app.copilot_head.event_generate("<Button-1>", x=3, y=3)
        tk_root.update_idletasks()
        assert app._view == "copilot", f"real Tk dispatch left view={app._view}"
        assert app._copilot_view is not None  # 点击真实创建了副驾视图
    finally:
        if app._copilot_view is not None:
            app._copilot_view.destroy()


def test_real_clicks_inside_copilot_do_not_exit(monkeypatch, tk_root):
    """R1：副驾视图内部点击（转录/输入）不冒泡为退出副驾；SEARCH tab 才退出。"""
    stub = StubController()
    app = build_panel(monkeypatch, tk_root, stub)
    try:
        app._open_copilot()
        tk_root.update()
        view = app._copilot_view
        assert app._view == "copilot"

        view.transcript.event_generate("<Button-1>", x=5, y=5)
        tk_root.update()
        assert app._view == "copilot"

        view.input.event_generate("<Button-1>", x=5, y=5)
        tk_root.update()
        assert app._view == "copilot"

        app.head.event_generate("<Button-1>", x=3, y=3)  # SEARCH tab：切回文件视图
        tk_root.update()
        assert app._view == "files"
    finally:
        if app._copilot_view is not None:
            app._copilot_view.destroy()


def test_real_send_button_click_submits_and_stays(monkeypatch, tk_root):
    """R1：真实点击发送按钮——提交生效且不退出副驾。

    Button-1 走真实事件派发（验证不冒泡退出副驾）；命令触发用 invoke()：
    Tk 的 ButtonRelease 命令派发会核对真实鼠标指针位置，合成事件无法
    伪造指针在控件内，故命令路径用官方程序化等价物。
    """
    stub = StubController()
    app = build_panel(monkeypatch, tk_root, stub)
    try:
        app._open_copilot()
        view = app._copilot_view
        view.conversation_id = "conv-x"
        view.input.insert("1.0", "你好副驾")
        tk_root.update()

        view.send_btn.event_generate("<Button-1>", x=3, y=3)
        tk_root.update()
        assert app._view == "copilot"  # 点击按钮本体不退出副驾
        view.send_btn.invoke()
        tk_root.update()

        assert stub.submitted == [("conv-x", "你好副驾", [])]
        assert app._view == "copilot"
        assert view.input.get("1.0", "end").strip() == ""  # 发送成功清草稿
    finally:
        if app._copilot_view is not None:
            app._copilot_view.destroy()


def test_real_permission_button_click_answers_and_stays(monkeypatch, tk_root):
    """R1/R3：真实点击许可按钮——回答生效且不退出副驾（命令经 invoke，见上）。"""
    stub = StubController()
    stub.pending = [_perm(
        "perm-1", "pwsh", {"command": "dir"},
        [{"option_id": "allow-once", "name": "允许一次", "kind": "allow_once"},
         {"option_id": "reject-once", "name": "拒绝", "kind": "reject_once"}],
    )]
    app = build_panel(monkeypatch, tk_root, stub)
    try:
        app._open_copilot()
        view = app._copilot_view
        view.refresh(full=True)
        tk_root.update()
        buttons = _collect_buttons(view.perm_card)
        texts = [b.cget("text") for b in buttons]
        assert "允许一次" in texts and "拒绝" in texts

        allow_btn = next(b for b in buttons if b.cget("text") == "允许一次")
        allow_btn.event_generate("<Button-1>", x=3, y=3)
        tk_root.update()
        assert app._view == "copilot"  # 点击许可按钮本体不退出副驾
        allow_btn.invoke()
        tk_root.update()

        assert stub.answered == [("perm-1", "allow-once")]
        assert app._view == "copilot"
    finally:
        if app._copilot_view is not None:
            app._copilot_view.destroy()


def test_permission_card_without_details_hides_allow(monkeypatch, tk_root):
    """R3：详情缺失的许可卡片不提供允许按钮，仅保留拒绝/停止。"""
    stub = StubController()
    stub.pending = [_perm(
        "perm-2", "", None,
        [{"option_id": "allow-once", "name": "允许一次", "kind": "allow_once"},
         {"option_id": "reject-once", "name": "拒绝", "kind": "reject_once"}],
    )]
    app = build_panel(monkeypatch, tk_root, stub)
    try:
        app._open_copilot()
        view = app._copilot_view
        view.refresh(full=True)
        tk_root.update()
        texts = [b.cget("text") for b in _collect_buttons(view.perm_card)]
        assert "允许一次" not in texts
        assert "拒绝" in texts
        assert stub.submitted == [] and stub.answered == []
    finally:
        if app._copilot_view is not None:
            app._copilot_view.destroy()


def test_resume_keeps_tk_event_loop_alive(monkeypatch, tk_root):
    """R8：慢恢复在工作线程执行；Tk 事件循环持续处理（心跳无长停顿）。"""
    from tkinter import font as tkfont
    from copilot.panel_view import CopilotView

    class SlowController(StubController):
        def resume(self, conv_id):
            time.sleep(1.0)  # 模拟慢启动/挂起的 runtime
            self.resumed = getattr(self, "resumed", []) + [conv_id]
            return True

    slow = SlowController()
    font = tkfont.Font(root=tk_root, family="Courier", size=-14)
    small = tkfont.Font(root=tk_root, family="Courier", size=-11)
    view = CopilotView(tk_root, font, small, get_controller=lambda: (slow, None))
    try:
        view.conversation_id = "conv-slow"
        view.show()
        beats = []

        def beat():
            beats.append(time.monotonic())
            tk_root.after(10, beat)

        tk_root.after(10, beat)
        view._resume_conversation()

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            tk_root.update()
            time.sleep(0.005)

        assert getattr(slow, "resumed", None) == ["conv-slow"]
        assert beats, "no heartbeats recorded"
        gaps = [b - a for a, b in zip(beats, beats[1:])]
        assert max(gaps) < 0.6, (
            f"Tk event loop stalled during resume: max gap {max(gaps):.2f}s"
        )
        assert view.status.cget("text") == "已恢复会话（不回放历史）"
    finally:
        view.destroy()


# ---------------- R9：服务退出接入副驾受控关闭 ----------------

def test_server_exit_closes_http_then_copilot(monkeypatch):
    import server as srv
    import copilot.runtime_host as runtime_host

    order = []

    class FakeServer:
        def __init__(self, addr, handler):
            pass

        def serve_forever(self):
            raise KeyboardInterrupt  # 模拟 Ctrl+C

        def server_close(self):
            order.append("http_close")

    monkeypatch.setattr(srv, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(srv, "PORT", 1)  # 单实例探测：连接被拒 → 继续启动
    monkeypatch.setattr(srv, "_configured_token", lambda: None)
    monkeypatch.setattr(srv, "_cpu_sampler", lambda: None)
    monkeypatch.setattr(srv, "_usage_collector", lambda: None)
    monkeypatch.setattr(srv, "_zone_watcher", lambda: None)
    monkeypatch.setattr(srv.search_panel, "start_thread", lambda: None)
    monkeypatch.setattr(runtime_host, "shutdown",
                        lambda: order.append("copilot_shutdown"))

    srv.main()  # 不抛 KeyboardInterrupt：退出路径吞掉并走 finally 收尾

    assert order == ["http_close", "copilot_shutdown"]
