"""副驾控制器：UI/服务与 ACP runtime 之间的唯一协调层。

线程模型（spec 第 7 节）：
- 单一 asyncio 线程拥有 ACP connection 与子进程；Tk/HTTP 线程只通过
  本类的方法提交命令、读取快照。
- 回调只归一化/入队；事件按序消费，终态等待前序事件落库后再通知。
- 通知通过线程安全队列暴露（UI 用 after 轮询），另支持订阅回调。

关键不变量：
- 全局同时最多一个非终态 Run；
- 同一 run_id 重复提交返回原记录（防双击重复派单）；
- PREPARING 段取消不发送 ACP cancel（prompt 未写入）；写入后必须走
  session/cancel 通知并等待真实结算；
- 断线/未知送达 → INTERRUPTED，绝不自动重发；
- runtime 每次重启 epoch+1，旧许可不作用于新运行时。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import models
from .models import (
    UNKNOWN_TOOL_DETAIL,
    Conversation,
    EventRecord,
    PermissionOptionView,
    PermissionRecord,
    Run,
    RunState,
)
from .store import CopilotStore

#: 恢复时载入的最近事件窗口（与内存 ring 上限同源；更早记录仍在库，
#: 以 note 标注截断，不静默丢弃最新历史）
EVENT_RING_MAX = 2000
RECOVERY_EVENT_WINDOW = EVENT_RING_MAX
#: 工具详情缓存上限（run_id, tool_call_id -> 详情）；超出丢最旧一半
TOOL_DETAIL_CACHE_MAX = 512


class ControllerError(Exception):
    """命令拒绝原因（面向用户可读）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def runtime_fingerprint(executable: str, args: list[str], dsh_home: str) -> str:
    raw = "|".join([executable, " ".join(args), dsh_home])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class CopilotController:
    """进程内单例；生命周期由调用方（server/UI）管理。"""

    def __init__(
        self,
        *,
        executable: str,
        runtime_args: list[str],
        dsh_home: str,
        launch_cwd: str,
        store: CopilotStore,
        idle_shutdown_seconds: int = 600,
        initialize_timeout: int = 30,
        cancel_warning_seconds: int = 10,
        stderr_dir: Optional[Path] = None,
        mcp_servers_provider: Optional[Callable[[], list]] = None,
    ):
        self._executable = executable
        self._runtime_args = list(runtime_args)
        self._dsh_home = dsh_home
        self._launch_cwd = launch_cwd
        self._store = store
        self._idle_shutdown_seconds = idle_shutdown_seconds
        self._initialize_timeout = initialize_timeout
        self._cancel_warning_seconds = cancel_warning_seconds
        self._stderr_dir = stderr_dir
        self._mcp_servers_provider = mcp_servers_provider or (lambda: [])

        # ---- 跨线程状态（_lock 保护；事件循环线程也遵守）----
        self._lock = threading.RLock()
        self._conversations: dict[str, Conversation] = {}
        self._runs: dict[str, Run] = {}
        self._events: dict[str, list[EventRecord]] = {}
        self._permissions: dict[str, PermissionRecord] = {}
        self._notifications: "queue.Queue[dict]" = queue.Queue(maxsize=1000)
        self._subscribers: list[Callable[[str], None]] = []
        self._store_healthy = True
        self._shutting_down = False
        self._closed = False
        self._cancel_requested: dict[str, bool] = {}
        self._seq_counter = 0

        # ---- asyncio 线程私有（仅事件循环线程触碰）----
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop: Optional[asyncio.Event] = None
        self._spawned = None
        self._epoch = 0
        self._session_to_run: dict[str, str] = {}
        self._perm_futures: dict[str, "asyncio.Future"] = {}
        # (run_id, tool_call_id) -> {title/kind/raw_input}：许可请求可能只带
        # toolCallId，详情从前序工具更新合并（R3）
        self._tool_call_details: dict[tuple[str, str], dict] = {}
        self._event_queue: Optional[asyncio.Queue] = None
        self._runtime_lock: Optional[asyncio.Lock] = None
        self._idle_timer: Optional[asyncio.TimerHandle] = None
        self._warn_tasks: set = set()
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

        self._recover_from_store()

    # ------------------------------------------------------------------
    # 启动/恢复
    # ------------------------------------------------------------------
    def _recover_from_store(self) -> None:
        try:
            interrupted = self._store.mark_unsettled_interrupted()
            for conv in self._store.list_conversations():
                conv.session_live = False  # 运行时未起，会话不可能 live
                self._conversations[conv.conversation_id] = conv
                # 恢复最近窗口而非最早 500 条（R5）：长对话重启后最新历史必须
                # 可见；seq 是全库自增，以各对话最大 seq 恢复全局高水位
                events = self._store.events_for_conversation(
                    conv.conversation_id, limit=RECOVERY_EVENT_WINDOW, tail=True
                )
                if events:
                    self._seq_counter = max(self._seq_counter, events[-1].seq)
                    older = self._store.count_events_before(
                        conv.conversation_id, events[0].seq
                    )
                    if older:
                        # 截断需明示：更早记录仍在本地库，不静默丢失
                        events.insert(0, EventRecord(
                            seq=events[0].seq - 1,
                            run_id="",
                            conversation_id=conv.conversation_id,
                            runtime_epoch=0,
                            kind="note",
                            payload={"text": f"更早的 {older} 条记录未载入（本地库仍保存）"},
                            observed_at=events[0].observed_at,
                        ))
                self._events[conv.conversation_id] = events
                for run in self._store.runs_for_conversation(conv.conversation_id):
                    self._runs[run.run_id] = run
            pruned = self._store.prune()
            if interrupted or pruned:
                self._notify("recovered", {"interrupted_runs": interrupted, "pruned": pruned})
        except Exception:
            self._store_healthy = False

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._thread_main, name="copilot-controller", daemon=True
            )
            self._thread.start()
        self._ready.wait(timeout=15)

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._runtime_lock = asyncio.Lock()
        self._event_queue = asyncio.Queue()
        self._stop = asyncio.Event()
        self._ready.set()
        try:
            loop.run_until_complete(self._async_main())
        finally:
            loop.close()

    async def _async_main(self) -> None:
        consumer = asyncio.create_task(self._event_consumer())
        # 进程退出监控：runtime 子进程死掉时结算在途轮次
        try:
            await self._stop.wait()
        finally:
            consumer.cancel()
            try:
                await asyncio.wait_for(consumer, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            if self._idle_timer is not None:
                self._idle_timer.cancel()

    # ------------------------------------------------------------------
    # 通知与订阅
    # ------------------------------------------------------------------
    def _notify(self, kind: str, data: Optional[dict] = None) -> None:
        item = {"kind": kind, "ts": time.time(), "data": data or {}}
        try:
            self._notifications.put_nowait(item)
        except queue.Full:
            try:
                self._notifications.get_nowait()
                self._notifications.put_nowait(item)
            except queue.Empty:
                pass
        for cb in list(self._subscribers):
            try:
                cb(kind)
            except Exception:
                pass

    def take_notifications(self, limit: int = 100) -> list[dict]:
        out: list[dict] = []
        while len(out) < limit:
            try:
                out.append(self._notifications.get_nowait())
            except queue.Empty:
                break
        return out

    def subscribe(self, callback: Callable[[str], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    # ------------------------------------------------------------------
    # 命令（任意线程调用；不阻塞事件循环）
    # ------------------------------------------------------------------
    def create_conversation(self, workspace: str, title: str = "") -> Conversation:
        workspace = str(Path(workspace).resolve())
        if not Path(workspace).is_dir():
            raise ControllerError("invalid_workspace", f"项目目录不存在: {workspace}")
        conv = Conversation(
            conversation_id=models.new_id("conv"),
            runtime_fingerprint=runtime_fingerprint(
                self._executable, self._runtime_args, self._dsh_home
            ),
            dsh_home=self._dsh_home,
            cwd=workspace,
            created_at=models.now(),
            last_activity_at=models.now(),
            title=title or Path(workspace).name,
        )
        try:
            self._store.upsert_conversation(conv)
        except Exception:
            self._store_healthy = False
            raise ControllerError("store_write_failed", "任务记录保存失败，已阻止新建对话")
        with self._lock:
            self._conversations[conv.conversation_id] = conv
            self._events.setdefault(conv.conversation_id, [])
        self._notify("conversation_created", {"conversation_id": conv.conversation_id})
        return conv

    def submit(
        self,
        conversation_id: str,
        text: str,
        context_refs: Optional[list[str]] = None,
        run_id: Optional[str] = None,
    ) -> Run:
        """显式发送。同一 run_id 重复提交返回原记录；全局单活跃轮次。"""
        with self._lock:
            # 幂等去重先于一切校验：重放（同 request_id 重发）返回原受理，
            # 不因附件在事后被移动/删除而误报失败
            if run_id and run_id in self._runs:
                return self._runs[run_id]  # 幂等：重复点击只返回现有记录
        # 附件发送前校验存在（spec 5.3：缺失文件明确指出错误）
        for ref in (context_refs or []):
            if not Path(ref).exists():
                raise ControllerError(
                    "invalid_context_ref", f"附件路径不存在: {ref}"
                )
        with self._lock:
            # F04：两线程可能同时通过首段查图与附件校验——受理前再查一次，
            # 并发同 run_id 收敛到同一条记录而不是误报 run_in_progress
            if run_id and run_id in self._runs:
                return self._runs[run_id]
            # F03：新受理必须过生命周期与会话校验（幂等前移时不得丢掉）
            if self._shutting_down or self._closed:
                raise ControllerError("shutting_down", "控制器正在关闭，不接受新任务")
            conv = self._conversations.get(conversation_id)
            if conv is None:
                raise ControllerError("unknown_conversation", "对话不存在")
            if not text.strip() and not context_refs:
                raise ControllerError("empty_prompt", "输入为空")
            if any(not r.terminal for r in self._runs.values()):
                raise ControllerError("run_in_progress", "已有任务在执行，请等待完成或先停止")
            if not self._store_healthy:
                raise ControllerError(
                    "store_unavailable",
                    "任务记录暂不可用，新派单已暂停（可停止在途任务）",
                )
            run = Run(
                run_id=run_id or models.new_id("run"),
                conversation_id=conversation_id,
                state=RunState.PREPARING,
                submitted_at=models.now(),
                last_event_at=models.now(),
                user_text=text,
                context_refs=list(context_refs or []),
            )
            self._runs[run.run_id] = run

        # 先落库再派单（spec 第 7 节时序 1）
        try:
            self._store.save_run(run)
            with self._lock:
                self._append_event_locked(run, "user", {
                    "text": text,
                    "context_refs": run.context_refs,
                })
                self._append_event_locked(run, "state", {"state": run.state.value})
        except Exception:
            self._store_healthy = False
            with self._lock:
                run.state = RunState.FAILED
                run.error_code = "store_write_failed"
                run.error_detail = "任务记录保存失败，未派发任务"
                run.settled_at = models.now()
            try:
                self._store.save_run(run)
            except Exception:
                pass
            self._notify("run_failed", {"run_id": run.run_id, "error_code": "store_write_failed"})
            raise ControllerError("store_write_failed", run.error_detail)

        self.start()
        asyncio.run_coroutine_threadsafe(self._execute_run(run.run_id), self._loop)
        self._notify("run_submitted", {"run_id": run.run_id})
        return run

    def cancel(self, run_id: str) -> bool:
        """幂等取消。PREPARING 且未写 prompt 仅撤本地；否则发 ACP cancel。"""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return False
            if run.terminal:
                return True
            self._cancel_requested[run_id] = True
            prompt_written = run.prompt_written

        if not prompt_written:
            # 本地撤销：异步任务在写 prompt 前会看到标记并清理迟到结果
            self._settle_local_cancel(run_id)
            return True

        # prompt 已可能写入：走协议取消（不阻塞 UI 线程等待）
        loop = self._loop
        if loop is not None:
            fut = asyncio.run_coroutine_threadsafe(self._acp_cancel(run_id), loop)
            fut.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        else:
            self._settle_local_cancel(run_id)
        return True

    def answer_permission(self, permission_id: str, option_id: str) -> bool:
        """回答许可。校验 pending/run/epoch；只可回答一次。"""
        with self._lock:
            perm = self._permissions.get(permission_id)
            if perm is None or not perm.pending:
                return False
            run = self._runs.get(perm.run_id)
            if run is None or run.terminal:
                return False
            if perm.runtime_epoch != self._epoch:
                perm.state = "expired"
                return False
            if not any(o.option_id == option_id for o in perm.options):
                return False
            perm.state = "resolved"
            perm.resolved_option = option_id
            perm.resolved_at = models.now()
        self._persist_permission(perm)
        self._append_event_public(
            perm.run_id, "permission_resolved",
            {"permission_id": permission_id, "option_id": option_id},
        )
        loop = self._loop
        fut = self._perm_futures.get(permission_id)
        if loop is not None and fut is not None and not fut.done():
            loop.call_soon_threadsafe(fut.set_result, ("selected", option_id))
        self._notify("permission_resolved", {"permission_id": permission_id})
        return True

    def _check_resume_identity(self, conv: Conversation) -> None:
        """恢复既有 DSH 会话前的身份校验。

        显式 resume() 与发送时的自动恢复（_ensure_session）共用（R7）：
        home 不一致说明当前运行时会话库与创建时不同，恢复会串库；
        cwd 已删除则 runtime 无法按原目录工作。失败明确报错，绝不静默
        换库或另建一段对话。
        """
        if conv.dsh_home != self._dsh_home:
            raise ControllerError(
                "home_mismatch", "当前 DSH_HOME 与该会话创建时不同，拒绝恢复以防串库"
            )
        if conv.cwd and not Path(conv.cwd).is_dir():
            raise ControllerError(
                "cwd_missing", f"该会话的工作目录已不存在: {conv.cwd}"
            )

    def resume(self, conversation_id: str) -> bool:
        """恢复已知归属的非活跃会话；不隐式重发上次 prompt。"""
        with self._lock:
            conv = self._conversations.get(conversation_id)
            if conv is None:
                raise ControllerError("unknown_conversation", "对话不存在")
            if not conv.dsh_session_id:
                raise ControllerError(
                    "no_session", "该对话没有可恢复的 DSH 会话（可能首轮失败）"
                )
            self._check_resume_identity(conv)
        self.start()
        return asyncio.run_coroutine_threadsafe(
            self._acp_resume(conversation_id), self._loop
        ).result(timeout=90)

    def close_conversation(self, conversation_id: str) -> bool:
        with self._lock:
            conv = self._conversations.get(conversation_id)
            if conv is None:
                return False
        if self._loop is None:
            return False
        try:
            return asyncio.run_coroutine_threadsafe(
                self._acp_close_session(conversation_id), self._loop
            ).result(timeout=15)
        except Exception:
            return False

    def set_selected_config(self, conversation_id: str, config_id: str, value: Any) -> None:
        """记录用户显式选择的模型/推理选项（opaque，发送前应用）。"""
        with self._lock:
            conv = self._conversations.get(conversation_id)
            if conv is None:
                raise ControllerError("unknown_conversation", "对话不存在")
            conv.selected_config[config_id] = value
            conv.last_activity_at = models.now()
        self._persist_conversation(conv)

    def force_stop(self, run_id: str) -> bool:
        """强制结束：仅 owned runtime；状态记 INTERRUPTED。"""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return False
            if run.terminal:
                return True
        if self._loop is None:
            return False
        return asyncio.run_coroutine_threadsafe(
            self._force_stop_runtime(run_id), self._loop
        ).result(timeout=20)

    def shutdown(self, timeout: float = 25.0) -> None:
        """正常退出：阻止新提交 → 取消在途/许可 → close 会话 → 关进程。"""
        with self._lock:
            if self._closed:
                return
            self._shutting_down = True
        if self._loop is not None and self._ready.is_set():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._async_shutdown(), self._loop
                ).result(timeout=timeout)
            except Exception:
                pass
            try:
                self._loop.call_soon_threadsafe(self._stop.set)
            except Exception:
                pass
            if self._thread is not None:
                self._thread.join(timeout=5.0)
        with self._lock:
            self._closed = True
        try:
            self._store.prune()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 快照（任意线程）
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            terminal = {s.value for s in models.TERMINAL_STATES}
            convs = [c.to_public() for c in self._conversations.values()]
            runs = sorted(
                (r.to_public() for r in self._runs.values()),
                key=lambda r: r["submitted_at"],
                reverse=True,
            )[:50]
            return {
                "conversations": convs,
                "runs": runs,
                "active_run": next((r for r in runs if r["state"] not in terminal), None),
                "pending_permissions": [
                    p.to_public() for p in self._permissions.values() if p.pending
                ],
                "store_healthy": self._store_healthy,
                "runtime": {
                    "spawned": self._spawned is not None,
                    "epoch": self._epoch,
                    "executable": self._executable,
                },
            }

    def events(self, conversation_id: str, after_seq: int = 0) -> list[EventRecord]:
        with self._lock:
            ring = self._events.get(conversation_id, [])
            return [e for e in ring if e.seq > after_seq]

    # ------------------------------------------------------------------
    # asyncio 线程内部
    # ------------------------------------------------------------------
    def _settle_local_cancel(self, run_id: str) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.terminal or run.prompt_written:
                return
            run.state = RunState.CANCELLED
            run.stop_reason = "cancelled_local"
            run.settled_at = models.now()
        self._persist_run(run)
        self._append_event_public(run_id, "state", {"state": "CANCELLED"})
        self._notify("run_cancelled", {"run_id": run_id})

    def _abort_before_prompt(self, run_id: str) -> bool:
        """写 prompt 前的统一取消/终态复核（R2）。

        runtime 启动、会话建立、配置应用都是异步等待点，期间 cancel() 可
        能已把轮次本地结算为 CANCELLED（终态不可被迟到任务复活）。
        """
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.terminal:
                return True
            return bool(self._cancel_requested.get(run_id)) and not run.prompt_written

    async def _execute_run(self, run_id: str) -> None:
        from acp.schema import ResourceContentBlock, TextContentBlock

        with self._lock:
            run = self._runs.get(run_id)
        if run is None:
            return
        conv = self._conversations.get(run.conversation_id)
        if conv is None:
            self._fail_run(run_id, "unknown_conversation", "对话记录缺失")
            return

        try:
            spawned = await self._ensure_runtime()
        except Exception as exc:
            self._fail_run(run_id, "runtime_start_failed", f"无法启动 DSH runtime: {exc}")
            return

        # PREPARING 段取消：迟到结果清理（绝不 prompt）
        if self._abort_before_prompt(run_id):
            self._settle_local_cancel(run_id)
            return

        prior_session_id = conv.dsh_session_id
        try:
            session_id = await self._ensure_session(spawned, conv)
        except Exception as exc:
            code = "session_resume_failed" if conv.dsh_session_id else "session_new_failed"
            self._fail_run(run_id, code, f"建立 DSH 会话失败: {exc}")
            return

        if self._abort_before_prompt(run_id):
            if prior_session_id is None:
                # 刚建的会话因取消而不再需要：关闭防孤儿，并同步转非 live
                # 持久化（S1：否则下一轮误复用已关闭会话、跳过 resume 而
                # 向非活跃 agent 发送 prompt）
                try:
                    await spawned.connection.close_session(session_id)
                except Exception:
                    pass
                with self._lock:
                    conv.session_live = False
                self._persist_conversation(conv)
            self._settle_local_cancel(run_id)
            return

        try:
            await self._apply_selected_config(spawned, conv, session_id)
        except Exception as exc:
            self._fail_run(run_id, "config_apply_failed", f"应用模型选项失败: {exc}")
            return

        if self._abort_before_prompt(run_id):
            # 等待配置应用的窗口内 cancel() 可能已本地结算为 CANCELLED
            self._settle_local_cancel(run_id)
            return

        blocks: list[Any] = []
        if run.user_text:
            blocks.append(TextContentBlock(type="text", text=run.user_text))
        for ref in run.context_refs:
            p = Path(ref)
            blocks.append(
                ResourceContentBlock(
                    type="resource_link", uri=p.resolve().as_uri(), name=p.name
                )
            )
        if not blocks:
            self._fail_run(run_id, "empty_prompt", "无可发送内容")
            return

        # -> SUBMITTING：即将写 prompt（此后取消必须走 ACP cancel）。
        # 取消复核与状态转移在同一互斥边界完成（R2）：任一异步准入步骤
        # （runtime/会话/配置）等待期间被取消或已进终态的轮次，绝不发送
        with self._lock:
            abort = self._abort_before_prompt(run_id)
            if not abort:
                run.state = RunState.SUBMITTING
                run.prompt_written = True
                run.runtime_epoch = self._epoch
        if abort:
            self._settle_local_cancel(run_id)
            return
        self._persist_run(run)
        self._append_event_public(run_id, "state", {"state": "SUBMITTING"})
        self._notify("run_state", {"run_id": run_id, "state": "SUBMITTING"})

        self._session_to_run[session_id] = run_id
        try:
            response = await spawned.connection.prompt(session_id, blocks)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            await self._handle_prompt_failure(run_id, exc)
            return

        # 等前序事件全部落库再结算
        if self._event_queue is not None:
            try:
                await asyncio.wait_for(self._event_queue.join(), timeout=10.0)
            except asyncio.TimeoutError:
                pass

        stop_reason = response.stop_reason
        with self._lock:
            if run.terminal:
                # 迟到的 prompt 返回不复活已结算轮次（取消/强制结束已定终态）
                return
            run.stop_reason = stop_reason
            run.state = models.state_for_stop_reason(stop_reason)
            run.settled_at = models.now()
        self._persist_run(run)
        self._append_event_public(
            run_id, "state", {"state": run.state.value, "stop_reason": stop_reason}
        )
        self._notify("run_settled", {"run_id": run_id, "state": run.state.value})
        self._maybe_schedule_idle_shutdown()

    async def _handle_prompt_failure(self, run_id: str, exc: Exception) -> None:
        name = type(exc).__name__
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.terminal:
                return
            if isinstance(exc, (ConnectionError, BrokenPipeError)) or "Connection" in name:
                state, code = RunState.INTERRUPTED, "connection_lost"
                detail = f"连接中断，任务是否已送达未知: {name}"
            else:
                state, code = RunState.FAILED, "prompt_failed"
                detail = f"{name}: {str(exc)[:300]}"
            run.state = state
            run.error_code = code
            run.error_detail = detail
            run.settled_at = models.now()
        self._persist_run(run)
        self._append_event_public(
            run_id, "state", {"state": state.value, "error_code": code, "detail": detail}
        )
        self._notify("run_settled", {"run_id": run_id, "state": state.value})
        if code == "connection_lost":
            # 运行时已不可用：丢弃连接引用并使 epoch 前进（不覆盖 run 状态）
            await self._discard_runtime("connection_lost")

    async def _acp_cancel(self, run_id: str) -> None:
        """prompt 已写入后的协议取消：通知 + 等待真实结算。"""
        with self._lock:
            run = self._runs.get(run_id)
        if run is None or run.terminal:
            return
        session_id = next(
            (sid for sid, rid in self._session_to_run.items() if rid == run_id), None
        )
        with self._lock:
            if run.state != RunState.CANCELLING:
                run.state = RunState.CANCELLING
        self._persist_run(run)
        self._append_event_public(run_id, "state", {"state": "CANCELLING"})
        self._notify("run_state", {"run_id": run_id, "state": "CANCELLING"})

        # 待许可 callback 回应 cancelled，避免许可等待阻止停稳
        for perm_id, fut in list(self._perm_futures.items()):
            if not fut.done():
                perm = self._permissions.get(perm_id)
                if perm is not None and perm.run_id == run_id:
                    fut.set_result(("cancelled", None))
                    perm.state = "expired"
                    perm.resolved_option = "__cancelled__"
                    perm.resolved_at = models.now()
                    self._persist_permission(perm)

        if session_id and self._spawned is not None:
            try:
                await self._spawned.connection.cancel(session_id)
            except Exception:
                pass  # 通知失败不改变 CANCELLING；结算仍以 prompt 返回为准

        async def warn() -> None:
            await asyncio.sleep(self._cancel_warning_seconds)
            self._warn_tasks.discard(current)
            with self._lock:
                cur = self._runs.get(run_id)
            if cur is not None and not cur.terminal:
                self._append_event_public(
                    run_id, "note", {"text": "停止尚未确认，可强制结束（将记为中断）"}
                )
                self._notify("cancel_unconfirmed", {"run_id": run_id})

        current = asyncio.get_running_loop().create_task(warn())
        self._warn_tasks.add(current)

    async def _force_stop_runtime(self, run_id: str) -> bool:
        with self._lock:
            run = self._runs.get(run_id)
        if run is None or run.terminal:
            return run is not None
        if self._spawned is not None:
            # 优先 Job Terminate（整个 owned 子树，含 DSH 派生的工具进程）；
            # 归属核验失败（PID 重用/无 Job）时回退单进程关闭
            killed = False
            try:
                killed = self._spawned.terminate_owned_subtree()
            except Exception:
                killed = False
            if not killed:
                await self._spawned.close(shutdown_timeout=3.0)
            await self._on_runtime_gone("force_stopped")
        with self._lock:
            run.state = RunState.INTERRUPTED
            run.error_code = "force_stopped"
            run.error_detail = "强制结束：结果未知，未回滚已执行操作"
            run.settled_at = models.now()
        self._persist_run(run)
        self._append_event_public(run_id, "state", {"state": "INTERRUPTED"})
        self._notify("run_settled", {"run_id": run_id, "state": "INTERRUPTED"})
        return True

    async def _acp_resume(self, conversation_id: str) -> bool:
        conv = self._conversations.get(conversation_id)
        if conv is None or not conv.dsh_session_id:
            return False
        try:
            spawned = await self._ensure_runtime()
        except Exception as exc:
            raise ControllerError("runtime_start_failed", f"无法启动 DSH runtime: {exc}")
        try:
            await spawned.connection.resume_session(
                session_id=conv.dsh_session_id,
                cwd=conv.cwd,
                mcp_servers=self._mcp_servers_provider(),
            )
        except Exception:
            return False
        with self._lock:
            conv.session_live = True
            conv.last_activity_at = models.now()
        self._persist_conversation(conv)
        self._append_event_public(
            None, "note",
            {
                "text": "已恢复会话（DSH 不回放历史，以上为本地已保存记录）",
                "conversation_id": conversation_id,
            },
        )
        self._notify("conversation_resumed", {"conversation_id": conversation_id})
        return True

    async def _acp_close_session(self, conversation_id: str) -> bool:
        conv = self._conversations.get(conversation_id)
        if conv is None or not conv.dsh_session_id or self._spawned is None:
            return False
        try:
            await self._spawned.connection.close_session(conv.dsh_session_id)
        except Exception:
            return False
        with self._lock:
            conv.session_live = False
        self._persist_conversation(conv)
        return True

    # ---- runtime 生命周期 ----
    async def _ensure_runtime(self):
        if self._spawned is not None and self._spawned.process.returncode is None:
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None
            return self._spawned
        assert self._runtime_lock is not None
        async with self._runtime_lock:
            if self._spawned is not None and self._spawned.process.returncode is None:
                return self._spawned
            stale = self._spawned
            if stale is not None:
                # 旧 runtime 进程已退出但引用仍在（S3，如空闲期间意外退出）：
                # 先统一回收旧资源（连接/stdio/Job 收尾、旧会话转非 live、
                # 许可过期、epoch+1），再拉起新 runtime。在途轮次的结算不
                # 在此进行——此刻的非终态轮次属于新派单，与旧进程死亡无关
                await self._discard_runtime("runtime_exit_detected", spawned=stale)
            from . import acp_adapter

            env = dict(os.environ)
            env["DSH_HOME"] = self._dsh_home
            stderr_path = (
                self._stderr_dir / f"runtime-{int(time.time())}.log"
                if self._stderr_dir
                else None
            )
            spawned = await acp_adapter.spawn_agent_subprocess(
                self._executable,
                self._runtime_args,
                env=env,
                launch_cwd=self._launch_cwd or None,
                stderr_path=stderr_path,
                on_session_update=self._on_session_update,
                on_request_permission=self._on_request_permission,
                initialize_timeout=float(self._initialize_timeout),
            )
            self._spawned = spawned
            self._epoch += 1
            self._expire_permissions("epoch_changed")
            self._notify("runtime_started", {"epoch": self._epoch})
            return spawned

    def _expire_permissions(self, reason: str) -> None:
        """运行时重启/取消：pending 许可过期，callback 回 cancelled。"""
        for fut in list(self._perm_futures.values()):
            if not fut.done():
                fut.set_result(("cancelled", None))
        for perm in self._permissions.values():
            if perm.pending:
                perm.state = "expired"
                perm.resolved_option = f"__{reason}__"
                perm.resolved_at = models.now()
                self._persist_permission(perm)

    async def _discard_runtime(self, reason: str, spawned=None) -> None:
        """运行时不可用：清引用、epoch+1、许可过期、会话转非 live。

        协议连接消失不代表进程退出（R4）：先清 ``_spawned`` 引用使新提交
        可另起 runtime，再对旧 spawned 做有界清理——close() 走完整降级链
        （connection → stdin → 限时等待 → terminate → kill），Job 句柄统一
        收尾；总时长有上界，不阻塞事件循环上的其他轮次。
        spawned 可显式传入（S3：_ensure_runtime 发现旧进程已退出时回收
        陈旧引用，不改写在途轮次状态）。
        不改动 run 状态（调用方自行决定在途轮次的终态语义）。
        """
        target = spawned if spawned is not None else self._spawned
        if target is None:
            return
        if self._spawned is target:
            self._spawned = None
        self._epoch += 1
        self._expire_permissions("runtime_gone")
        for conv in self._conversations.values():
            conv.session_live = False
        self._notify("runtime_gone", {"reason": reason})
        try:
            await asyncio.wait_for(target.close(shutdown_timeout=3.0), timeout=10.0)
        except Exception:
            pass  # 清理尽力而为：Job kill-on-close 在句柄关闭时仍由内核兜底

    async def _on_runtime_gone(self, reason: str) -> None:
        """运行时进程消失：在途轮次全部 INTERRUPTED（结果待核实，不重发）。"""
        if self._spawned is None:
            return
        for run in list(self._runs.values()):
            if not run.terminal:
                with self._lock:
                    run.state = RunState.INTERRUPTED
                    run.error_code = "runtime_exit"
                    run.error_detail = f"运行时已退出（{reason}），本轮结果待核实"
                    run.settled_at = models.now()
                self._persist_run(run)
                self._append_event_public(
                    run.run_id, "state", {"state": "INTERRUPTED", "reason": reason}
                )
                self._notify(
                    "run_settled", {"run_id": run.run_id, "state": "INTERRUPTED"}
                )
        await self._discard_runtime(reason)

    def _maybe_schedule_idle_shutdown(self) -> None:
        if self._idle_shutdown_seconds <= 0:
            return
        if any(not r.terminal for r in self._runs.values()):
            return
        if any(p.pending for p in self._permissions.values()):
            return
        if self._spawned is None:
            return
        loop = asyncio.get_running_loop()
        if self._idle_timer is not None:
            self._idle_timer.cancel()

        def _reap() -> None:
            self._idle_timer = None
            if self._spawned is None:
                return
            if any(not r.terminal for r in self._runs.values()):
                return
            self._notify("runtime_idle_close", {})

            async def _close() -> None:
                await self._spawned.close(shutdown_timeout=3.0)
                await self._on_runtime_gone("idle_shutdown")

            asyncio.ensure_future(_close())

        self._idle_timer = loop.call_later(self._idle_shutdown_seconds, _reap)

    async def _ensure_session(self, spawned, conv: Conversation) -> str:
        self._check_resume_identity(conv)  # 自动恢复与显式 resume 同一校验（R7）
        if conv.session_live and conv.dsh_session_id:
            return conv.dsh_session_id
        if conv.dsh_session_id:
            await spawned.connection.resume_session(
                session_id=conv.dsh_session_id,
                cwd=conv.cwd,
                mcp_servers=self._mcp_servers_provider(),
            )
        else:
            resp = await spawned.connection.new_session(
                cwd=conv.cwd, mcp_servers=self._mcp_servers_provider()
            )
            with self._lock:
                conv.dsh_session_id = resp.session_id
                conv.config_options = self._normalize_config_options(resp.config_options)
                conv.last_activity_at = models.now()
            self._persist_conversation(conv)
            self._append_event_public(
                None, "config",
                {
                    "text": "会话已创建，模型选项已更新",
                    "conversation_id": conv.conversation_id,
                },
            )
        with self._lock:
            conv.session_live = True
        self._persist_conversation(conv)
        return conv.dsh_session_id

    async def _apply_selected_config(self, spawned, conv: Conversation, session_id: str) -> None:
        for config_id, value in conv.selected_config.items():
            await spawned.connection.set_config_option(
                config_id=config_id, session_id=session_id, value=value
            )

    @staticmethod
    def _normalize_config_options(options) -> list[dict]:
        out: list[dict] = []
        for opt in options or []:
            out.append(
                {
                    "id": getattr(opt, "id", ""),
                    "type": getattr(opt, "type", "select"),
                    "title": getattr(opt, "name", ""),
                    "current_value": getattr(opt, "current_value", None),
                    "options": [
                        {"value": getattr(o, "value", ""), "name": getattr(o, "name", "")}
                        for o in (getattr(opt, "options", None) or [])
                    ],
                }
            )
        return out

    # ---- 事件管道 ----
    async def _on_session_update(self, session_id: str, update) -> None:
        """回调在 asyncio 线程：只归一化入队，不落库不做 UI。"""
        if self._event_queue is not None:
            await self._event_queue.put((session_id, update))

    async def _event_consumer(self) -> None:
        assert self._event_queue is not None
        while True:
            session_id, update = await self._event_queue.get()
            try:
                self._process_update(session_id, update)
            except Exception:
                pass
            finally:
                self._event_queue.task_done()

    def _process_update(self, session_id: str, update) -> None:
        run_id = self._session_to_run.get(session_id)
        if run_id is None:
            return  # 无关联轮次（如 close 后残余更新）：丢弃
        run = self._runs.get(run_id)
        if run is None:
            return
        kind = getattr(update, "session_update", "")

        if kind == "agent_message_chunk":
            text = getattr(getattr(update, "content", None), "text", "") or ""
            text, truncated = models.truncate_text(text, models.MAX_EVENT_TEXT_CHARS)
            self._append_event_public(run_id, "assistant", {"text": text, "truncated": truncated})
            self._touch_run(run, running=True)
        elif kind == "user_message_chunk":
            text = getattr(getattr(update, "content", None), "text", "") or ""
            self._append_event_public(run_id, "user_echo", {"text": text[:2000]})
            self._touch_run(run, running=False)
        elif kind == "agent_thought_chunk":
            return  # thought 默认不展示不落库（spec 第 7 节）
        elif kind in ("tool_call", "tool_call_update"):
            payload: dict[str, Any] = {
                "tool_call_id": getattr(update, "tool_call_id", ""),
                "tool_kind": getattr(update, "kind", None),
                "status": getattr(update, "status", None),
                "title": getattr(update, "title", None),
            }
            content = getattr(update, "content", None)
            if content:
                items = content if isinstance(content, list) else [content]
                texts = [
                    getattr(getattr(item, "content", None), "text", "") or ""
                    for item in items
                ]
                joined, truncated = models.truncate_text(
                    "\n".join(t for t in texts if t), models.MAX_TOOL_OUTPUT_CHARS
                )
                payload["output"] = joined
                payload["output_truncated"] = truncated
            # 许可请求可能只带 toolCallId：缓存前序工具详情供其关联（R3）
            tool_call_id = getattr(update, "tool_call_id", "") or ""
            merged = self._remember_tool_details(run_id, tool_call_id, update)
            if merged:
                self._fill_pending_permission_details(run_id, tool_call_id, merged)
            self._append_event_public(run_id, "tool", payload)
            self._touch_run(run, running=True)
        elif kind == "usage_update":
            self._append_event_public(
                run_id, "usage",
                {"used": getattr(update, "used", None), "size": getattr(update, "size", None)},
            )
            self._touch_run(run, running=False)
        elif kind == "config_option_update":
            self._append_event_public(run_id, "config", {"raw": "config_option_update"})
            self._touch_run(run, running=False)
        else:
            # session_info/plan/available_commands/current_mode：暂不支持，忽略
            return

    def _touch_run(self, run: Run, running: bool) -> None:
        with self._lock:
            run.last_event_at = models.now()
            if running and run.state == RunState.SUBMITTING:
                run.state = RunState.RUNNING
        self._persist_run(run)

    def _remember_tool_details(self, run_id: str, tool_call_id: str, update) -> dict:
        """缓存/合并 (run_id, tool_call_id) 的工具详情（R3）。

        DSH 的许可请求可能只带 toolCallId（zcode 实机记录已出现）；前序
        tool_call 更新里的 title/kind/raw_input 是许可卡片的详情来源。
        """
        if not tool_call_id:
            return {}
        key = (run_id, tool_call_id)
        merged = dict(self._tool_call_details.get(key) or {})
        for name, value in (
            ("title", getattr(update, "title", None)),
            ("kind", getattr(update, "kind", None)),
            ("raw_input", getattr(update, "raw_input", None)),
        ):
            if value is not None and value != "":
                merged[name] = value
        self._tool_call_details[key] = merged
        if len(self._tool_call_details) > TOOL_DETAIL_CACHE_MAX:
            for stale in list(self._tool_call_details)[: TOOL_DETAIL_CACHE_MAX // 2]:
                del self._tool_call_details[stale]
        return merged

    def _fill_pending_permission_details(
        self, run_id: str, tool_call_id: str, merged: dict
    ) -> None:
        """迟到的工具详情补齐 pending 许可（许可先到、详情后到的次序）。"""
        for perm in list(self._permissions.values()):
            if (
                not perm.pending
                or perm.run_id != run_id
                or perm.tool_call_id != tool_call_id
            ):
                continue
            changed = False
            if (
                (not perm.tool_title or perm.tool_title == UNKNOWN_TOOL_DETAIL)
                and merged.get("title")
            ):
                perm.tool_title = merged["title"]
                changed = True
            if perm.raw_input is None and merged.get("raw_input") is not None:
                perm.raw_input = merged["raw_input"]
                changed = True
            if not perm.tool_kind and merged.get("kind"):
                perm.tool_kind = merged["kind"]
                changed = True
            if changed:
                self._persist_permission(perm)
                self._notify(
                    "permission_details", {"permission_id": perm.permission_id}
                )

    # ---- 许可 ----
    async def _on_request_permission(self, session_id: str, tool_call, options):
        from acp.schema import (
            AllowedOutcome,
            DeniedOutcome,
            RequestPermissionResponse,
        )

        run_id = self._session_to_run.get(session_id)
        run = self._runs.get(run_id) if run_id else None
        if run is None or run.terminal:
            # 无关联轮次的许可：直接拒绝，绝不含糊允许
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        tool_call_id = getattr(tool_call, "tool_call_id", "") or ""
        # 许可请求可能只带 toolCallId：关联前序工具更新的已知详情（R3）
        known = self._tool_call_details.get((run_id, tool_call_id)) or {}
        perm = PermissionRecord(
            permission_id=models.new_id("perm"),
            run_id=run_id,
            conversation_id=run.conversation_id,
            runtime_epoch=self._epoch,
            tool_call_id=tool_call_id,
            created_at=models.now(),
            tool_title=(
                getattr(tool_call, "title", "") or known.get("title")
                or UNKNOWN_TOOL_DETAIL
            ),
            tool_kind=getattr(tool_call, "kind", "") or known.get("kind") or "",
            raw_input=getattr(tool_call, "raw_input", None) or known.get("raw_input"),
            options=[
                PermissionOptionView(option_id=o.option_id, name=o.name, kind=o.kind)
                for o in options
            ],
        )
        with self._lock:
            self._permissions[perm.permission_id] = perm
            if not run.terminal:
                run.state = RunState.WAITING_PERMISSION
                run.last_event_at = models.now()
        self._persist_run(run)
        self._persist_permission(perm)
        self._append_event_public(run_id, "permission_request", perm.to_public())
        self._notify("permission_request", {"permission_id": perm.permission_id})

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._perm_futures[perm.permission_id] = fut
        try:
            outcome_pair = await fut
        except asyncio.CancelledError:
            outcome_pair = ("cancelled", None)
        finally:
            self._perm_futures.pop(perm.permission_id, None)

        # 许可结束后回到执行态（等待 prompt 真实结算）
        with self._lock:
            if not run.terminal and run.state == RunState.WAITING_PERMISSION:
                run.state = RunState.RUNNING
        self._persist_run(run)

        action, option_id = outcome_pair
        if action == "selected" and option_id:
            return RequestPermissionResponse(
                outcome=AllowedOutcome(outcome="selected", option_id=option_id)
            )
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    # ---- 持久化辅助 ----
    def _persist_run(self, run: Run) -> None:
        try:
            self._store.save_run(run)
        except Exception:
            self._store_healthy = False
            self._notify("store_unhealthy", {"run_id": run.run_id})

    def _persist_conversation(self, conv: Conversation) -> None:
        try:
            self._store.upsert_conversation(conv)
        except Exception:
            self._store_healthy = False

    def _persist_permission(self, perm: PermissionRecord) -> None:
        try:
            self._store.save_permission(perm)
        except Exception:
            self._store_healthy = False

    def _append_event_locked(self, run: Run, kind: str, payload: dict) -> None:
        """调用方已持 _lock。"""
        event = EventRecord(
            seq=self._next_seq(),
            run_id=run.run_id,
            conversation_id=run.conversation_id,
            runtime_epoch=self._epoch,
            kind=kind,
            payload=payload,
            observed_at=models.now(),
        )
        self._events.setdefault(run.conversation_id, []).append(event)
        try:
            self._store.append_event(event)
        except Exception:
            raise

    def _append_event_public(self, run_id: Optional[str], kind: str, payload: dict) -> None:
        with self._lock:
            if run_id:
                run = self._runs.get(run_id)
                if run is None:
                    return
                conv_id = run.conversation_id
            else:
                conv_id = payload.get("conversation_id", "")
                if not conv_id:
                    return
            event = EventRecord(
                seq=self._next_seq(),
                run_id=run_id or "",
                conversation_id=conv_id,
                runtime_epoch=self._epoch,
                kind=kind,
                payload=payload,
                observed_at=models.now(),
            )
            ring = self._events.setdefault(conv_id, [])
            ring.append(event)
            if len(ring) > EVENT_RING_MAX:
                del ring[: len(ring) - EVENT_RING_MAX]
        try:
            self._store.append_event(event)
        except Exception:
            self._store_healthy = False

    def _next_seq(self) -> int:
        self._seq_counter += 1
        return self._seq_counter

    def _fail_run(self, run_id: str, code: str, detail: str) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return
            if run.terminal:
                # 终态不可被迟到失败覆盖（S2）：取消等已结算轮次保持原终态，
                # 不追加误导性的失败事件
                return
            run.state = RunState.FAILED
            run.error_code = code
            run.error_detail = detail
            run.settled_at = models.now()
        self._persist_run(run)
        self._append_event_public(
            run_id, "state", {"state": "FAILED", "error_code": code, "detail": detail}
        )
        self._notify("run_failed", {"run_id": run_id, "error_code": code})

    async def _async_shutdown(self) -> None:
        """运行时线程内的受控关闭（不抛出，尽力而为）。"""
        for task in list(self._warn_tasks):
            task.cancel()
        self._warn_tasks.clear()
        for run in list(self._runs.values()):
            if not run.terminal:
                if run.prompt_written:
                    await self._acp_cancel(run.run_id)
                else:
                    self._settle_local_cancel(run.run_id)
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if all(r.terminal for r in self._runs.values()):
                break
            await asyncio.sleep(0.2)
        if self._spawned is not None:
            try:
                for conv in self._conversations.values():
                    if conv.session_live and conv.dsh_session_id:
                        try:
                            await asyncio.wait_for(
                                self._spawned.connection.close_session(conv.dsh_session_id),
                                timeout=5.0,
                            )
                        except Exception:
                            pass
                        conv.session_live = False
                await self._spawned.close(shutdown_timeout=5.0)
            finally:
                await self._on_runtime_gone("shutdown")
