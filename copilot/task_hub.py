"""TaskHub：ACP 会话轮次与 CLI 任务的统一门面。

ACP 侧只依赖 controller 的两个公开面：``subscribe(callback)`` 与
``snapshot()``（返回含 ``runs`` 的 dict）——鸭子类型，不 import
CopilotController，测试用假 controller。

RunState → TaskStatus 映射（spec 决策 5）：

    PREPARING/SUBMITTING/RUNNING/CANCELLING → running
    WAITING_PERMISSION                     → needs_input
    SETTLED/LIMITED                        → ready（LIMITED 带 note）
    FAILED/INTERRUPTED/CANCELLED           → blocked（各带 note）

CLI 侧直接把 GenericCliAdapter 的任务写进同一个 TaskPool。统一快照按
展示优先级 → 最近更新排序，供 HUD/托盘/胶囊（蓝包语义）消费。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional

from .cli_agents import AgentsFile, load_agents
from .generic_cli import GenericCliAdapter
from .tasks import Task, TaskPool

#: RunState.value → (task_status, note)
RUN_STATE_MAP: dict[str, tuple[str, str]] = {
    "PREPARING": ("running", ""),
    "SUBMITTING": ("running", ""),
    "RUNNING": ("running", ""),
    "CANCELLING": ("running", "正在停止"),
    "WAITING_PERMISSION": ("needs_input", "待许可"),
    "SETTLED": ("ready", ""),
    "LIMITED": ("ready", "已到本轮上限"),
    "FAILED": ("blocked", "执行失败"),
    "INTERRUPTED": ("blocked", "连接中断，结果待核实"),
    "CANCELLED": ("blocked", "已停止"),
}

ACP_AGENT_LABEL = "dsh"


class AcpTaskSource:
    """把 controller 快照里的 runs 投影为 Task；通知驱动 + 显式同步。"""

    def __init__(self, controller_like: Any, pool: TaskPool, *, agent_label: str = ACP_AGENT_LABEL):
        self._ctl = controller_like
        self._pool = pool
        self._agent_label = agent_label
        self._lock = threading.Lock()
        controller_like.subscribe(self._on_notify)

    def _on_notify(self, kind: str) -> None:
        self.sync()

    def sync(self) -> int:
        """从 snapshot() 全量对账一次；返回本次维护的 run 数。"""
        snap = self._ctl.snapshot()
        n = 0
        with self._lock:
            for run in snap.get("runs", []):
                state = str(run.get("state", ""))
                mapped = RUN_STATE_MAP.get(state)
                if mapped is None:
                    continue
                status, note = mapped
                extra: dict[str, Any] = {"conversation_id": run.get("conversation_id")}
                if run.get("stop_reason"):
                    extra["stop_reason"] = run["stop_reason"]
                if run.get("error_code"):
                    extra["error_code"] = run["error_code"]
                user_text = run.get("user_text") or ""
                self._pool.upsert(
                    "acp",
                    run["run_id"],
                    agent=self._agent_label,
                    title=user_text or run["run_id"],
                    status=status,
                    note=note,
                    extra=extra,
                )
                n += 1
        return n


class CliTaskSource:
    """GenericCliAdapter 的薄包装：submit/cancel 透传 + agent 清单。"""

    def __init__(self, adapter: GenericCliAdapter, agents_file: AgentsFile):
        self.adapter = adapter
        self.agents_file = agents_file

    def submit(self, agent_name: str, prompt: str, *, title: str = "",
               scene: "dict | None" = None) -> Task:
        return self.adapter.submit(agent_name, prompt, title=title, scene=scene)

    def cancel(self, task_id: str) -> bool:
        return self.adapter.cancel(task_id)

    def agent_names(self) -> list[str]:
        return sorted(self.agents_file.agents)


class TaskHub:
    """统一任务门面：TaskPool + ACP 源 + CLI 源。"""

    def __init__(
        self,
        controller_like: Any,
        *,
        data_dir: Path,
        agents_path: Optional[Path] = None,
        max_concurrent: int = 3,
        agent_label: str = ACP_AGENT_LABEL,
    ):
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self.pool = TaskPool(data_dir / "task_pool.json")
        agents_path = Path(agents_path) if agents_path else data_dir / "copilot_agents.json"
        self.agents_file = load_agents(agents_path)
        self.cli = CliTaskSource(
            GenericCliAdapter(
                self.agents_file.agents,
                self.pool,
                data_dir / "cli_logs",
                max_concurrent=max_concurrent,
            ),
            self.agents_file,
        )
        self.acp = AcpTaskSource(controller_like, self.pool, agent_label=agent_label)
        self.acp.sync()

    # ---- 只读面 ----
    def snapshot(self) -> dict[str, Any]:
        tasks = [t.to_public() for t in self.pool.list_tasks()]
        return {
            "tasks": tasks,
            "stats": self.pool.stats(),
            "agents": self.cli.agent_names(),
            "agents_skipped": list(self.agents_file.skipped),
        }

    def tasks(self) -> list[Task]:
        return self.pool.list_tasks()

    # ---- 命令面 ----
    def mark_seen(self, task_id: str) -> bool:
        return self.pool.mark_seen(task_id)

    def submit_cli(self, agent_name: str, prompt: str, *, title: str = "",
                   scene: "dict | None" = None) -> Task:
        return self.cli.submit(agent_name, prompt, title=title, scene=scene)

    def cancel(self, task_id: str) -> bool:
        task = self.pool.get(task_id)
        if task is None:
            return False
        if task.source == "cli":
            return self.cli.cancel(task_id)
        return False  # ACP 取消走 controller.cancel，不经 hub（保持单一命令面）

    def clear_finished(self) -> int:
        return self.pool.clear_finished()

    def shutdown(self) -> None:
        self.cli.adapter.shutdown()
        self.pool.close()
