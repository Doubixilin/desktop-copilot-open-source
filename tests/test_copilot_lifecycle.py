"""08 票：闲置释放、进程归属与恢复边界。

进程归属（C20）：Job Object 只包含 owned runtime；强制结束核验
PID+创建时间；外部进程不受影响。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from copilot import acp_adapter, models  # noqa: E402
from copilot.controller import CopilotController  # noqa: E402
from copilot.store import CopilotStore  # noqa: E402

sys.path.insert(0, str(REPO / "tests"))
from test_copilot_controller import Harness, wait_until  # noqa: E402

PYTHON = sys.executable
TERMINAL = {s.value for s in models.TERMINAL_STATES}


def test_no_dsh_process_until_first_use(tmp_path):
    """未使用副驾不启动 DSH 进程（runtime 仅在 submit/resume 时懒启动）。"""
    h = Harness(tmp_path, {"turns": []})
    try:
        h.controller.start()
        time.sleep(0.3)
        snap = h.controller.snapshot()
        assert snap["runtime"]["spawned"] is False
        conv = h.new_conversation()  # 新建对话也不启动
        snap = h.controller.snapshot()
        assert snap["runtime"]["spawned"] is False
    finally:
        h.close()


def test_idle_release_and_reuse(tmp_path):
    """闲置 1 秒回收 runtime；再次派单经 resume 复用原会话。"""
    script = {
        "turns": [
            {"updates": [{"kind": "agent_text", "text": "第一轮"}],
             "stop_reason": "end_turn"},
            {"match": "第二", "updates": [{"kind": "agent_text", "text": "第二轮"}],
             "stop_reason": "end_turn"},
        ]
    }
    h = Harness(tmp_path, script, idle_shutdown_seconds=1)
    try:
        conv = h.new_conversation()
        r1 = h.submit(conv, "第一问")
        assert h.wait_terminal(r1.run_id) == "SETTLED"
        assert wait_until(
            lambda: h.controller.snapshot()["runtime"]["spawned"] is False, 10
        ), "闲置未回收 runtime"
        sid = h.store.get_conversation(conv.conversation_id).dsh_session_id
        r2 = h.submit(conv, "第二问")  # 回收后再用：resume 同 sessionId
        assert h.wait_terminal(r2.run_id) == "SETTLED"
        sid2 = h.store.get_conversation(conv.conversation_id).dsh_session_id
        assert sid == sid2
    finally:
        h.close()


def test_force_stop_kills_only_owned_subtree(tmp_path):
    """强制结束只杀 owned 子树；外部同名进程不受影响（C20）。"""
    script = {
        "turns": [
            {
                "updates": [
                    {"kind": "agent_text", "text": "开始"},
                    {"kind": "silent", "seconds": 60},
                ],
                "stop_reason": "end_turn",
            }
        ],
        "on_cancel": {"settle_after": 30, "stop_reason": "cancelled"},
    }
    h = Harness(tmp_path, script)
    # 外部进程：独立启动的同可执行文件长驻进程（python -c sleep）
    external = subprocess.Popen(
        [PYTHON, "-c", "import time; time.sleep(120)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        conv = h.new_conversation()
        run = h.submit(conv, "长任务")
        assert wait_until(lambda: h.run_state(run.run_id) == "RUNNING", 10)
        snap = h.controller.snapshot()
        assert snap["runtime"]["spawned"] is True
        owned_pid = None
        spawned = h.controller._spawned
        owned_pid = spawned.pid
        assert spawned.ownership_matches() is True
        assert h.controller.force_stop(run.run_id) is True
        assert wait_until(lambda: h.run_state(run.run_id) == "INTERRUPTED", 10)
        # owned 子进程已消失
        assert wait_until(lambda: not _pid_alive(owned_pid), 10)
        # 外部进程不受影响
        assert external.poll() is None
        assert _pid_alive(external.pid)
    finally:
        external.terminate()
        external.wait(timeout=10)
        h.close()


def _pid_alive(pid) -> bool:
    import psutil

    try:
        psutil.Process(pid)
        return True
    except psutil.NoSuchProcess:
        return False


def test_ownership_mismatch_rejected(tmp_path):
    """PID 重用防护：create_time 不匹配时不得 Job Terminate。"""
    short = subprocess.Popen(
        [PYTHON, "-c", "print('x')"],
        stdout=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    short.wait(timeout=10)

    class FakeProc:
        pid = short.pid  # 已退出进程的 PID（大概率未被复用，若复用则 create_time 不同）

        def __init__(self):
            self.returncode = None

    spawned = acp_adapter.SpawnedAgent(
        connection=None, process=FakeProc(), executable="x",
        pid_create_time=1.0,  # 明显不同的创建时间
    )
    assert spawned.ownership_matches() is False
    assert spawned.terminate_owned_subtree() is False  # 核验失败不执行杀


def test_shutdown_releases_owned_runtime(tmp_path):
    """正常退出：合理期限内释放 owned runtime，无遗留。"""
    h = Harness(tmp_path, {
        "turns": [{"updates": [{"kind": "agent_text", "text": "ok"}],
                   "stop_reason": "end_turn"}]
    })
    c = h.new_conversation()
    r = h.submit(c, "hi")
    assert h.wait_terminal(r.run_id) == "SETTLED"
    pid = h.controller._spawned.pid if h.controller._spawned else None
    t0 = time.monotonic()
    h.controller.shutdown(timeout=20)
    elapsed = time.monotonic() - t0
    assert elapsed < 15, f"shutdown 用时 {elapsed:.1f}s"
    if pid is not None:
        assert wait_until(lambda: not _pid_alive(pid), 10), "owned runtime 未释放"
    h.store.close()


def test_job_object_created_on_windows():
    """Windows 下 spawn 应创建 kill-on-close Job（非 Windows 跳过）。"""
    if sys.platform != "win32":
        pytest.skip("Windows only")
    import asyncio

    async def body():
        return await acp_adapter.spawn_agent_subprocess(
            PYTHON, ["-c", "import time; time.sleep(30)"],
            initialize_timeout=10, skip_initialize=True,
        )

    loop = asyncio.new_event_loop()
    spawned = loop.run_until_complete(body())
    try:
        assert spawned.job_handle is not None, "Job Object 未创建"
        assert spawned.ownership_matches() is True
    finally:
        loop.run_until_complete(spawned.close(shutdown_timeout=3))
        loop.close()
