"""ACP 适配层：官方 agent-client-protocol SDK 之上的薄封装。

设计约束（spec 第 7 节 / 01 票）：
- SDK 的 ``spawn_agent_process`` 不支持 Windows 子进程创建标志，本模块用
  ``asyncio.create_subprocess_exec``（CREATE_NO_WINDOW + 独立 stderr 管道）
  加官方 ``ClientSideConnection`` 组装，不自写 JSON-RPC。
- 所有运行时 I/O 只发生在控制器创建的单一 asyncio 线程内；Tk/HTTP 线程
  仅通过队列与快照交互。
- 本模块不声明 fs/terminal 客户端能力（未实现相应回调）。
"""
from __future__ import annotations

import asyncio
import subprocess as _subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional

if False:  # 类型提示用；运行时懒加载
    from acp.client.connection import ClientSideConnection
    from acp.schema import InitializeResponse

if hasattr(_subprocess, "CREATE_NO_WINDOW"):
    _CREATION_FLAGS = _subprocess.CREATE_NO_WINDOW
else:  # 非 Windows 平台无此标志
    _CREATION_FLAGS = 0


# ---- Windows Job Object：owned runtime 的异常退出兜底（08 票）----
# JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE：本进程（数据服务）死亡时内核关闭
# 句柄并终止整个 job —— owned DSH 子进程及其工具子树不遗留孤儿。
# 不设置 UI/优先级等限制，允许 DSH 正常派生工具子进程。
try:
    import ctypes

    _kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
except Exception:  # 非 Windows
    _kernel32 = None

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_CLASS = None
if _kernel32 is not None:
    import ctypes as _ctypes

    class _IOCounters(_ctypes.Structure):
        _fields_ = [(n, _ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimitInformation(_ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", _ctypes.c_longlong),
            ("PerJobUserTimeLimit", _ctypes.c_longlong),
            ("LimitFlags", _ctypes.c_uint32),
            ("MinimumWorkingSetSize", _ctypes.c_size_t),
            ("MaximumWorkingSetSize", _ctypes.c_size_t),
            ("ActiveProcessLimit", _ctypes.c_uint32),
            ("Affinity", _ctypes.POINTER(_ctypes.c_ulong)),
            ("PriorityClass", _ctypes.c_uint32),
            ("SchedulingClass", _ctypes.c_uint32),
        ]

    class _ExtendedLimitInformation(_ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IOCounters),
            ("ProcessMemoryLimit", _ctypes.c_size_t),
            ("JobMemoryLimit", _ctypes.c_size_t),
            ("PeakProcessMemoryUsed", _ctypes.c_size_t),
            ("PeakJobMemoryUsed", _ctypes.c_size_t),
        ]

    _JOB_CLASS = _ExtendedLimitInformation


def _create_kill_on_close_job():
    """创建 kill-on-close Job；失败返回 None（回退到常规关闭路径）。"""
    if _kernel32 is None:
        return None
    import ctypes

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", ctypes.c_uint32),
                    ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", ctypes.c_int)]

    sa = _SecurityAttributes(
        nLength=ctypes.sizeof(_SecurityAttributes),
        lpSecurityDescriptor=None,
        bInheritHandle=0,  # 不可继承：句柄只归本进程，死亡即触发 kill
    )
    handle = _kernel32.CreateJobObjectW(ctypes.byref(sa), None)
    if not handle:
        return None
    info = _JOB_CLASS()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _kernel32.SetInformationJobObject(
        handle, 9, ctypes.byref(info), ctypes.sizeof(info)
    ):  # JobObjectExtendedLimitInformation = 9
        _kernel32.CloseHandle(handle)
        return None
    return handle


def _assign_to_job(job_handle, pid: int) -> bool:
    if job_handle is None or _kernel32 is None:
        return False
    import ctypes

    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001
    h = _kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
    if not h:
        return False
    try:
        return bool(_kernel32.AssignProcessToJobObject(job_handle, h))
    finally:
        _kernel32.CloseHandle(h)


def terminate_job(job_handle) -> bool:
    """终止整个 job（owned 子树）；返回是否成功。"""
    if job_handle is None or _kernel32 is None:
        return False
    return bool(_kernel32.TerminateJobObject(job_handle, 1))


def acp_sdk_version() -> str:
    from importlib.metadata import version

    return version("agent-client-protocol")


@dataclass
class SpawnedAgent:
    """一次 DSH 子进程连接。initialize_response 在握手成功后填充。"""

    connection: Any
    process: asyncio.subprocess.Process
    executable: str
    args: tuple[str, ...] = ()
    initialize_response: Optional["InitializeResponse"] = None
    stderr_file: Any = None
    job_handle: Any = None          # Windows Job Object（kill-on-close）
    pid_create_time: Optional[float] = None  # 防 PID 重用误杀的核验值

    @property
    def pid(self) -> Optional[int]:
        return self.process.pid

    def ownership_matches(self) -> bool:
        """PID+创建时间核验：进程仍是当初 spawn 的那个（防 PID 重用）。"""
        if self.pid is None:
            return False
        try:
            import psutil

            proc = psutil.Process(self.pid)
            return (
                self.pid_create_time is not None
                and abs(proc.create_time() - self.pid_create_time) < 1.0
            )
        except Exception:
            return False

    def terminate_owned_subtree(self) -> bool:
        """强制结束 owned 子树（Job Terminate）。仅核验归属后执行。"""
        if self.job_handle is not None and self.ownership_matches():
            return terminate_job(self.job_handle)
        return False

    async def close(self, shutdown_timeout: float = 5.0) -> None:
        """graceful: 关 connection -> stdin -> 限时等待 -> terminate -> kill。

        connection/stdio/Job 在统一 finally 收尾且幂等（R6）：正常退出与
        terminate 分支不再提前 return 泄漏 Job 句柄；若 runtime 父进程已
        退出而工具子进程仍活着，这里关闭 Job 句柄同样触发内核 kill-on-close
        兜底，可重复调用。
        """
        proc = self.process
        try:
            try:
                await self.connection.close()
            except Exception:
                pass
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=shutdown_timeout)
            except asyncio.TimeoutError:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=shutdown_timeout)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        pass
        finally:
            if self.job_handle is not None:
                try:
                    import ctypes

                    ctypes.windll.kernel32.CloseHandle(self.job_handle)  # type: ignore[attr-defined]
                except Exception:
                    pass
                self.job_handle = None

    def close_stderr_file(self) -> None:
        if self.stderr_file is not None:
            try:
                self.stderr_file.close()
            finally:
                self.stderr_file = None


UpdateCallback = Callable[[str, Any], Awaitable[None]]
PermissionCallback = Callable[[str, Any, Any], Awaitable[Any]]
DisconnectCallback = Callable[[str], Awaitable[None]]


async def spawn_agent_subprocess(
    executable: str,
    args: tuple[str, ...] | list[str],
    *,
    env: Optional[Mapping[str, str]] = None,
    launch_cwd: Optional[str] = None,
    stderr_path: Optional[Path] = None,
    on_session_update: Optional[UpdateCallback] = None,
    on_request_permission: Optional[PermissionCallback] = None,
    on_disconnect: Optional[DisconnectCallback] = None,
    initialize_timeout: float = 30.0,
    client_name: str = "agent-deck-copilot",
    skip_initialize: bool = False,
) -> SpawnedAgent:
    """启动 DSH ACP 子进程并完成 initialize 握手。

    on_session_update / on_request_permission 在 asyncio 线程内被调用，
    实现方只做归一化/入队，不得阻塞。
    """
    from acp.client.connection import ClientSideConnection
    from acp.interfaces import ClientCapabilities, Implementation
    from acp.schema import DeniedOutcome, RequestPermissionResponse

    ACP_PROTOCOL_V1 = 1
    # 注意：本 SDK 中拒绝的鉴别字面量是 "cancelled"（DeniedOutcome），无 "denied"


    stderr_file = None
    stderr_arg = asyncio.subprocess.PIPE
    if stderr_path is not None:
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_file = open(stderr_path, "ab")
        stderr_arg = stderr_file

    try:
        process = await asyncio.create_subprocess_exec(
            str(executable),
            *[str(a) for a in args],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr_arg,
            env=dict(env) if env is not None else None,
            cwd=str(launch_cwd) if launch_cwd else None,
            creationflags=_CREATION_FLAGS,
        )
    except Exception:
        if stderr_file is not None:
            stderr_file.close()
        raise

    # owned 归属：kill-on-close Job + PID/创建时间核验值（08 票）
    job_handle = _create_kill_on_close_job()
    pid_create_time = None
    if job_handle is not None:
        if not _assign_to_job(job_handle, process.pid or 0):
            try:
                import ctypes

                ctypes.windll.kernel32.CloseHandle(job_handle)  # type: ignore[attr-defined]
            except Exception:
                pass
            job_handle = None
    try:
        import psutil

        pid_create_time = psutil.Process(process.pid).create_time()
    except Exception:
        pass

    class _Client:
        """回调面：只宣告本客户端真实支持的能力（无 fs/terminal）。"""

        async def session_update(self, session_id: str, update, **kwargs) -> None:
            if on_session_update is not None:
                await on_session_update(session_id, update)

        async def request_permission(self, session_id: str, tool_call, options, **kwargs):
            if on_request_permission is not None:
                return await on_request_permission(session_id, tool_call, options)
            # 无处理器时默认拒绝，绝不默认允许
            return RequestPermissionResponse(
                outcome=DeniedOutcome(outcome="cancelled")
            )

    connection: "ClientSideConnection" = ClientSideConnection(
        _Client(),
        process.stdin,
        process.stdout,
    )

    spawned = SpawnedAgent(
        connection=connection,
        process=process,
        executable=str(executable),
        args=tuple(str(a) for a in args),
        stderr_file=stderr_file,
        job_handle=job_handle,
        pid_create_time=pid_create_time,
    )

    if skip_initialize:
        return spawned
    try:
        init_resp = await asyncio.wait_for(
            connection.initialize(
                protocol_version=ACP_PROTOCOL_V1,
                client_capabilities=ClientCapabilities(fs=None, terminal=False),
                client_info=Implementation(
                    name=client_name, version=_client_version()
                ),
            ),
            timeout=initialize_timeout,
        )
    except Exception:
        await spawned.close()
        spawned.close_stderr_file()
        raise
    spawned.initialize_response = init_resp
    return spawned


def _client_version() -> str:
    import copilot

    return copilot.__version__
