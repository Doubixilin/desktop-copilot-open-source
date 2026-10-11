"""GenericCliAdapter：L0 通用 CLI 兜底执行器（subprocess + 读线程版）。

设计源：蓝包桌宠 AgentBridge L0 适配器（QProcess 版），按 spec 决策移植：
- 列表传参不经 shell，``{prompt}`` 逐元素替换（空格/引号天然安全）；
- ``json_lines`` 时按 NDJSON 事件映射（session/status/text/final/tool_call/
  tool_result/thinking 只进日志）；解析失败容忍（协议注明 DSH 单事件 32KB
  截断的现实）；
- ``needs_input`` 只是 stdout 正则命中的**过程态**，进程退出码最终覆盖；
- 退出 0 → ready，非 0 / 启动失败 / 取消 → blocked；
- 并发上限排队（排队中仍展示 running + queued 标记）；
- 取消：排队中直接 blocked；运行中 Windows 用 ``taskkill /T /F`` 杀进程树
  （agent 可能再派生子进程，仅 kill 主进程会留孤儿）。

线程模型：一个派发线程 + 每进程一个读线程；任务状态唯一写入 TaskPool。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Optional

from .cli_agents import AgentConfig
from .scene_package import freeze_scene_package, render_scene_block
from .tasks import TaskPool

MAX_PROMPT_CHARS = 8000
DEFAULT_MAX_CONCURRENT = 3
_IS_WINDOWS = os.name == "nt"
_CREATE_NO_WINDOW = 0x08000000


class CliAdapterError(Exception):
    """命令拒绝原因（面向用户可读）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _Job:
    __slots__ = ("task_id", "cfg", "prompt", "queued", "cancelled")

    def __init__(self, task_id: str, cfg: AgentConfig, prompt: str):
        self.task_id = task_id
        self.cfg = cfg
        self.prompt = prompt
        self.queued = True
        self.cancelled = False


class _Run:
    __slots__ = ("job", "proc", "cancel_requested", "log_path")

    def __init__(self, job: _Job, proc: subprocess.Popen, log_path: Path):
        self.job = job
        self.proc = proc
        self.cancel_requested = False
        self.log_path = log_path


class GenericCliAdapter:
    def __init__(
        self,
        agents: dict[str, AgentConfig],
        pool: TaskPool,
        log_dir: Path,
        *,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    ):
        self._agents = agents
        self._pool = pool
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._max_concurrent = max(1, max_concurrent)
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._queue: deque[_Job] = deque()
        self._running: dict[str, _Run] = {}
        self._patterns: dict[str, list[re.Pattern]] = {}
        for name, cfg in agents.items():
            self._patterns[name] = [re.compile(p) for p in cfg.needs_input_patterns]
        self._closed = False
        self._thread = threading.Thread(
            target=self._dispatch_loop, name="cli-adapter", daemon=True
        )
        self._thread.start()

    # ---- 对外命令 ----
    def submit(self, agent_name: str, prompt: str, *, title: str = "",
               scene: "dict | None" = None):
        """入队一个 CLI 任务；返回 TaskPool 中的任务。

        scene（M3）：最近一次现场探测结果；给出时冻结现场包、
        prompt 前注入现场块、task.extra 记录包追溯信息。
        """
        cfg = self._agents.get(agent_name)
        if cfg is None:
            raise CliAdapterError(
                "unknown_agent", f"未配置的 agent: {agent_name}"
            )
        prompt = (prompt or "").strip()
        if not prompt:
            raise CliAdapterError("empty_prompt", "提示词为空")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise CliAdapterError(
                "prompt_too_long", f"提示词超长（>{MAX_PROMPT_CHARS} 字符）"
            )
        package = None
        if scene:
            package = freeze_scene_package(
                prompt, scene, package_dir=self._log_dir / "packages"
            )
            prompt = render_scene_block(package) + "\n\n" + prompt
        with self._wake:
            if self._closed:
                raise CliAdapterError("adapter_closed", "适配器已关闭")
            will_queue = len(self._running) >= self._max_concurrent
            ref = f"cli-{uuid.uuid4().hex[:8]}"
            log_path = self._log_dir / f"{ref}.log"
            extra = {"queued": will_queue, "program": cfg.program}
            if package is not None:
                extra.update({
                    "scene_package_id": package["package_id"],
                    "scene_scenario": package["scene"]["scenario"],
                    "scene_coverage": package["scene"]["coverage"],
                    "scene_materials": package["integrity"]["materials_count"],
                })
            task = self._pool.upsert(
                "cli",
                ref,
                agent=cfg.display or cfg.name,
                title=title or prompt,
                status="running",
                log_path=str(log_path),
                extra=extra,
            )
            job = _Job(task.task_id, cfg, prompt)
            if will_queue:
                self._queue.append(job)
            else:
                self._spawn_locked(job, log_path)
            self._wake.notify_all()
        return task

    def cancel(self, task_id: str) -> bool:
        with self._wake:
            for job in self._queue:
                if job.task_id == task_id:
                    job.cancelled = True
                    self._queue.remove(job)
                    self._pool.upsert(
                        "cli",
                        self._ref_of(task_id),
                        status="blocked",
                        note="已取消（排队中）",
                        unread=False,
                    )
                    return True
            run = self._running.get(task_id)
            if run is None:
                return False
            run.cancel_requested = True
            self._kill_tree(run.proc)
            return True

    def shutdown(self, timeout: float = 10.0) -> None:
        with self._wake:
            self._closed = True
            for job in list(self._queue):
                job.cancelled = True
            self._queue.clear()
            for run in list(self._running.values()):
                run.cancel_requested = True
                self._kill_tree(run.proc)
            self._wake.notify_all()
        self._thread.join(timeout=timeout)

    # ---- 内部 ----
    def _ref_of(self, task_id: str) -> str:
        task = self._pool.get(task_id)
        return task.ref_id if task else task_id

    def _dispatch_loop(self) -> None:
        while True:
            with self._wake:
                while not self._closed and (
                    not self._queue or len(self._running) >= self._max_concurrent
                ):
                    self._wake.wait()
                if self._closed:
                    return
                job = self._queue.popleft()
                if job.cancelled:
                    continue
                log_path = self._log_dir / f"{self._ref_of(job.task_id)}.log"
                self._spawn_locked(job, log_path)

    def _spawn_locked(self, job: _Job, log_path: Path) -> None:
        cfg = job.cfg
        argv = cfg.build_argv(job.prompt)
        env = dict(os.environ)
        env.update(cfg.env)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        popen_kwargs: dict[str, Any] = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=cfg.cwd or None,
            env=env,
        )
        if _IS_WINDOWS:
            popen_kwargs["creationflags"] = _CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(  # noqa: S603 列表传参不经 shell
                argv,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **popen_kwargs,
            )
        except (FileNotFoundError, PermissionError, OSError) as e:
            self._pool.upsert(
                "cli",
                self._ref_of(job.task_id),
                status="blocked",
                note=f"启动失败：{e}",
                unread=True,
            )
            self._wake.notify_all()
            return
        run = _Run(job, proc, log_path)
        self._running[job.task_id] = run
        if job.queued:
            self._pool.upsert(
                "cli",
                self._ref_of(job.task_id),
                extra={"queued": False, "started": True},
            )
        reader = threading.Thread(
            target=self._read_loop,
            args=(run,),
            name=f"cli-read-{job.task_id}",
            daemon=True,
        )
        reader.start()

    def _read_loop(self, run: _Run) -> None:
        proc = run.proc
        ref = self._ref_of(run.job.task_id)
        cfg = run.job.cfg
        patterns = self._patterns.get(cfg.name, [])
        try:
            with open(run.log_path, "a", encoding="utf-8", errors="replace") as log:
                log.write(f"# argv: {cfg.program} {' '.join(cfg.args)}\n")
                assert proc.stdout is not None
                for raw in proc.stdout:
                    line = raw.rstrip("\r\n")
                    try:
                        log.write(line + "\n")
                    except OSError:
                        pass
                    self._consume_line(ref, cfg, patterns, line)
        except OSError:
            pass
        finally:
            try:
                rc = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._kill_tree(proc)
                rc = proc.wait()
            self._finalize(run, ref, rc)

    def _consume_line(
        self, ref: str, cfg: AgentConfig, patterns: list[re.Pattern], line: str
    ) -> None:
        mapped = False
        if cfg.json_lines and line.startswith("{"):
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                ev = None
            if isinstance(ev, dict) and "type" in ev:
                mapped = True
                self._apply_event(ref, ev)
        if not mapped:
            self._pool.upsert("cli", ref, last_text=line)
        for pat in patterns:
            if pat.search(line):
                task = self._pool.find("cli", ref)
                if task is not None and task.status == "running":
                    self._pool.upsert("cli", ref, status="needs_input")
                    self._pool.upsert(
                        "cli", ref, extra={"needs_input_hint": line}
                    )
                break

    def _apply_event(self, ref: str, ev: dict[str, Any]) -> None:
        etype = str(ev.get("type", ""))
        if etype == "session":
            self._pool.upsert(
                "cli", ref, extra={"session_id": ev.get("sessionId") or ev.get("session_id")}
            )
        elif etype == "status":
            self._pool.upsert("cli", ref, extra={"phase": ev.get("status") or ev.get("phase")})
        elif etype in ("text", "final"):
            self._pool.upsert("cli", ref, last_text=str(ev.get("text", "")))
            if etype == "final":
                self._pool.upsert("cli", ref, extra={"final": True})
        elif etype == "tool_call":
            self._pool.upsert(
                "cli", ref, extra={"last_tool": ev.get("name") or ev.get("tool") or "tool"}
            )
        elif etype == "tool_result":
            if ev.get("error"):
                self._pool.upsert("cli", ref, extra={"last_error": str(ev.get("error"))[:200]})
        # thinking / 未知类型：只进日志，不动任务（协议语义）

    def _finalize(self, run: _Run, ref: str, rc: int) -> None:
        with self._wake:
            self._running.pop(run.job.task_id, None)
            self._wake.notify_all()
        if run.cancel_requested:
            self._pool.upsert(
                "cli", ref, status="blocked", exit_code=rc,
                note="已取消", unread=True,
            )
        elif rc == 0:
            self._pool.upsert(
                "cli", ref, status="ready", exit_code=0, unread=True,
                extra={"queued": False},
            )
        else:
            note = f"退出码 {rc}"
            task = self._pool.find("cli", ref)
            if task is not None and task.status == "needs_input":
                note = f"等待输入后退出（退出码 {rc}）"
            self._pool.upsert(
                "cli", ref, status="blocked", exit_code=rc,
                note=note, unread=True,
            )
        self._pool.flush()

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        try:
            if _IS_WINDOWS:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    creationflags=_CREATE_NO_WINDOW,
                    timeout=10,
                )
            else:
                proc.kill()
        except (OSError, subprocess.SubprocessError):
            pass
