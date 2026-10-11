"""统一任务展示语义层：Task 模型 + TaskPool。

设计源：蓝包桌宠 AgentBridge 的 Task/TaskStore（M-A1），按 spec 决策去掉
Qt 依赖（Signal→回调列表）并修掉"每事件全量落盘"（改防抖合并写）。

五态与展示优先级（needs_input > blocked > ready > running > unknown）：
- ``needs_input`` 等用户决定（ACP 待许可 / CLI 正则命中）——最优先；
- ``blocked`` 需注意的异常（失败/取消/中断/孤儿）——高于 ready，因为不看会漏事；
- ``ready`` 有结果待看；``running`` 在途；``unknown`` 载入后状态不明。

未读语义：``running → ready/blocked/needs_input`` 的状态变化沿自动置
``unread``（Codex Mini"完成"与"完成未读"分开的语义）；``mark_seen`` 清除。

持久化：脏标记 + 防抖定时合并写（原子替换），终态与 close() 显式 flush；
保留最近 ``keep_tasks`` 条（按 updated_at 环形裁剪）。载入时
running/needs_input 残留对账为 blocked（宿主重启后不假装任务还活着）。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

#: 稳定序：展示排序与"点开哪个"的依据（值小者先）
TASK_STATUSES = ("running", "needs_input", "ready", "blocked", "unknown")
STATUS_PRIORITY = {
    "needs_input": 0,
    "blocked": 1,
    "ready": 2,
    "running": 3,
    "unknown": 4,
}

#: 状态变化沿自动置未读的目标状态
_UNREAD_ON_ENTER = frozenset({"needs_input", "blocked", "ready"})

TITLE_MAX_CHARS = 40
LAST_TEXT_MAX_CHARS = 200
KEEP_TASKS = 100
PERSIST_SCHEMA = 1


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


@dataclass
class Task:
    """一个可展示的任务单元；来源 ``acp``（Run 投影）或 ``cli``（子进程任务）。"""

    task_id: str
    source: str  # "acp" | "cli"
    ref_id: str  # acp: run_id；cli: 本地任务号
    agent: str
    title: str
    status: str = "running"
    unread: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    exit_code: Optional[int] = None
    last_text: str = ""
    log_path: str = ""
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def priority(self) -> int:
        return STATUS_PRIORITY.get(self.status, STATUS_PRIORITY["unknown"])

    def to_public(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "source": self.source,
            "ref_id": self.ref_id,
            "agent": self.agent,
            "title": self.title,
            "status": self.status,
            "priority": self.priority,
            "unread": self.unread,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "exit_code": self.exit_code,
            "last_text": self.last_text,
            "log_path": self.log_path,
            "note": self.note,
            "extra": dict(self.extra),
        }

    @staticmethod
    def from_public(data: dict[str, Any]) -> "Task":
        t = Task(
            task_id=data["task_id"],
            source=data.get("source", "unknown"),
            ref_id=data.get("ref_id", ""),
            agent=data.get("agent", ""),
            title=data.get("title", ""),
        )
        t.status = data.get("status", "unknown")
        t.unread = bool(data.get("unread", False))
        t.created_at = float(data.get("created_at", time.time()))
        t.updated_at = float(data.get("updated_at", t.created_at))
        t.exit_code = data.get("exit_code")
        t.last_text = data.get("last_text", "")
        t.log_path = data.get("log_path", "")
        t.note = data.get("note", "")
        t.extra = dict(data.get("extra") or {})
        if t.status not in TASK_STATUSES:
            t.status = "unknown"
        return t


class TaskPool:
    """线程安全的任务池：唯一写入口 ``upsert``，订阅回调 + 防抖持久化。"""

    def __init__(
        self,
        persist_path: Optional[Path] = None,
        *,
        debounce_seconds: float = 0.5,
        keep_tasks: int = KEEP_TASKS,
        clock: Callable[[], float] = time.time,
    ):
        self._lock = threading.RLock()
        self._tasks: dict[str, Task] = {}
        self._by_ref: dict[tuple[str, str], str] = {}
        self._subscribers: list[Callable[[str], None]] = []
        self._persist_path = Path(persist_path) if persist_path else None
        self._debounce = debounce_seconds
        self._keep = keep_tasks
        self._clock = clock
        self._dirty = False
        self._flush_timer: Optional[threading.Timer] = None
        self._closed = False
        if self._persist_path is not None:
            self._load()

    # ---- 查询 ----
    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            t = self._tasks.get(task_id)
            return t

    def find(self, source: str, ref_id: str) -> Optional[Task]:
        with self._lock:
            tid = self._by_ref.get((source, ref_id))
            return self._tasks.get(tid) if tid else None

    def list_tasks(self) -> list[Task]:
        """按展示优先级 → 最近更新排序的快照。"""
        with self._lock:
            tasks = list(self._tasks.values())
        tasks.sort(key=lambda t: (t.priority, -t.updated_at))
        return tasks

    def stats(self) -> dict[str, int]:
        with self._lock:
            out = {"total": len(self._tasks), "unread": 0}
            for t in self._tasks.values():
                if t.unread:
                    out["unread"] += 1
            for s in TASK_STATUSES:
                out[s] = 0
            for t in self._tasks.values():
                out[t.status] = out.get(t.status, 0) + 1
        return out

    # ---- 写入 ----
    def upsert(
        self,
        source: str,
        ref_id: str,
        *,
        agent: str = "",
        title: str = "",
        status: Optional[str] = None,
        last_text: Optional[str] = None,
        exit_code: Optional[int] = None,
        note: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
        log_path: Optional[str] = None,
        unread: Optional[bool] = None,
    ) -> Task:
        """按 (source, ref_id) 定位或新建；仅覆盖显式给出的字段。

        状态变化沿（running → needs_input/blocked/ready）自动置 unread，
        调用方显式传 unread 时以其为准。
        """
        if status is not None and status not in TASK_STATUSES:
            raise ValueError(f"unknown task status: {status}")
        with self._lock:
            if self._closed:
                raise RuntimeError("task pool closed")
            task = self.find(source, ref_id)
            created = False
            if task is None:
                task = Task(
                    task_id=f"t-{uuid.uuid4().hex[:8]}",
                    source=source,
                    ref_id=ref_id,
                    agent=agent,
                    title=_clip(title, TITLE_MAX_CHARS) or f"{agent or source} 任务",
                )
                self._tasks[task.task_id] = task
                self._by_ref[(source, ref_id)] = task.task_id
                created = True
            if status is not None and status != task.status:
                if task.status == "running" and status in _UNREAD_ON_ENTER:
                    task.unread = True
                task.status = status
            if agent:
                task.agent = agent
            if title:
                task.title = _clip(title, TITLE_MAX_CHARS)
            if last_text is not None:
                task.last_text = _clip(last_text, LAST_TEXT_MAX_CHARS)
            if exit_code is not None:
                task.exit_code = exit_code
            if note is not None:
                task.note = note
            if log_path is not None:
                task.log_path = log_path
            if extra:
                merged = dict(task.extra)
                merged.update(extra)
                task.extra = merged
            if unread is not None:
                task.unread = unread
            task.updated_at = self._clock()
            self._mark_dirty()
        self._emit(created and "created" or "updated")
        return task

    def mark_seen(self, task_id: str) -> bool:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None or not t.unread:
                return False
            t.unread = False
            t.updated_at = self._clock()
            self._mark_dirty()
        self._emit("seen")
        return True

    def clear_finished(self) -> int:
        """清除全部终态任务（ready/blocked/unknown）；running/needs_input 保留。"""
        with self._lock:
            doomed = [
                tid
                for tid, t in self._tasks.items()
                if t.status in ("ready", "blocked", "unknown")
            ]
            for tid in doomed:
                t = self._tasks.pop(tid)
                self._by_ref.pop((t.source, t.ref_id), None)
            if doomed:
                self._mark_dirty()
        if doomed:
            self._emit("cleared")
        return len(doomed)

    # ---- 订阅 ----
    def subscribe(self, callback: Callable[[str], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def _emit(self, reason: str) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(reason)
            except Exception:
                pass

    # ---- 持久化 ----
    def _mark_dirty(self) -> None:
        self._dirty = True
        if self._persist_path is None:
            return
        if self._flush_timer is None:
            self._flush_timer = threading.Timer(
                self._debounce, self._flush_timer_fire
            )
            self._flush_timer.daemon = True
            self._flush_timer.start()

    def _flush_timer_fire(self) -> None:
        with self._lock:
            self._flush_timer = None
        self.flush()

    def _write_file(self, path: Path, payload: dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)

    def flush(self) -> bool:
        """把当前池落盘（环形裁剪后）。无持久化路径或无脏数据时为空操作。"""
        with self._lock:
            if self._persist_path is None or not self._dirty:
                return False
            self._trim_locked()
            payload = {
                "schema": PERSIST_SCHEMA,
                "tasks": [t.to_public() for t in self._tasks.values()],
            }
            try:
                self._write_file(self._persist_path, payload)
            except OSError:
                return False
            self._dirty = False
            return True

    def _trim_locked(self) -> None:
        if len(self._tasks) <= self._keep:
            return
        ordered = sorted(self._tasks.values(), key=lambda t: t.updated_at, reverse=True)
        for t in ordered[self._keep :]:
            self._tasks.pop(t.task_id, None)
            self._by_ref.pop((t.source, t.ref_id), None)

    def _load(self) -> None:
        path = self._persist_path
        assert path is not None
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict) or data.get("schema") != PERSIST_SCHEMA:
            return
        for item in data.get("tasks", []):
            try:
                task = Task.from_public(item)
            except (KeyError, TypeError, ValueError):
                continue
            # 孤儿对账：宿主重启后在途状态不可信，统一对账为 blocked
            if task.status in ("running", "needs_input"):
                task.status = "blocked"
                task.note = "宿主重启：任务状态未知，请核实"
                task.unread = True
            self._tasks[task.task_id] = task
            self._by_ref[(task.source, task.ref_id)] = task.task_id

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._flush_timer is not None:
                self._flush_timer.cancel()
                self._flush_timer = None
        self.flush()
