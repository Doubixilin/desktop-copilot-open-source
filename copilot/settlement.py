"""结算驱动的后台作业（票03）：终态轮次无论界面是否打开都能归档。

链路：controller 落库通知（run_settled / run_failed / run_cancelled /
recovered）→ jobs 表入队（dedupe: archive:<run_id>）→ 工作线程领取执行。
- archive：hud_api.archive_one——原始事件保真归档；run.json 已存在即幂等跳过
- repair：库内事件重建 vs 既有归档，存在保真缺口 → archive_run_new_version
  （旧档保留，index 指向新版本）
启动补偿：扫描终态 run——未归档者入 archive 队；可修复的历史裁剪归档入
repair 队（有界）。失败指数退避重试，超限标 failed 可观察（list_jobs）。

通知回调与作业执行解耦：回调只入队不做事，任何异常不影响 controller。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Optional

from . import archive, hud_api
from .store import CopilotStore

#: 触发补偿扫描的控制器通知种类（终态与恢复）
_SETTLE_KINDS = {
    "run_settled", "run_failed", "run_cancelled", "recovered",
}

#: 事件读取页大小：按 seq 分页读完整会话再筛 run（F10——先截会话前 N 条
#: 再筛会丢长会话后部轮次的正文）
EVENT_PAGE = 1000

#: 修复扫描游标（store meta 键）：值 "settled_at|run_id"，避免每次启动重复
#: 检查同一段最旧候选（F11）
_REPAIR_CURSOR_KEY = "settlement.repair_cursor"


class SettlementWorker:
    def __init__(
        self,
        controller_provider: Callable[[], tuple],
        data_dir_factory: Callable[[], Path],
        *,
        poll_interval: float = 5.0,
        claim_limit: int = 5,
        repair_scan_limit: int = 200,
    ):
        self._controller_provider = controller_provider
        self._data_dir_factory = data_dir_factory
        self._poll_interval = poll_interval
        self._claim_limit = claim_limit
        self._repair_scan_limit = repair_scan_limit
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._store: Optional[CopilotStore] = None
        self._data_dir: Optional[Path] = None

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="copilot-settlement", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        # 惰性等待 controller：副驾未启用时轮询等待，绝不冒泡
        controller = None
        while not self._stop.is_set():
            try:
                controller, _err = self._controller_provider()
            except Exception:
                controller = None
            if controller is not None:
                break
            if self._stop.wait(30.0):
                return
        if controller is None or self._stop.is_set():
            return
        try:
            controller.subscribe(self._on_notify)
        except Exception as exc:
            print(f"[settlement] 订阅失败：{exc}", flush=True)
        try:
            n_arch = self.enqueue_missing_archives()
            n_rep = self.enqueue_repairs()
            if n_arch or n_rep:
                print(f"[settlement] 启动补偿：archive={n_arch} repair={n_rep}",
                      flush=True)
        except Exception as exc:
            print(f"[settlement] 启动补偿失败：{exc}", flush=True)
        while not self._stop.is_set():
            try:
                self.process_due()
            except Exception as exc:
                print(f"[settlement] 作业循环异常：{exc}", flush=True)
            self._wake.wait(self._poll_interval)
            self._wake.clear()
        try:
            if self._store is not None:
                self._store.close()
        except Exception:
            pass

    # ---- 通知接缝（controller 线程调用，只入队） ----

    def _on_notify(self, kind: str) -> None:
        if kind not in _SETTLE_KINDS:
            return
        try:
            n = self.enqueue_missing_archives()
            if n:
                print(f"[settlement] 结算入队 {n} 个归档作业（{kind}）", flush=True)
        except Exception as exc:
            print(f"[settlement] 结算入队失败：{exc}", flush=True)
        self._wake.set()

    # ---- 补偿/入队（同步可测；store 惰性初始化） ----

    def _ensure(self) -> tuple[CopilotStore, Path]:
        if self._store is None or self._data_dir is None:
            self._data_dir = Path(self._data_dir_factory())
            self._store = CopilotStore(self._data_dir / "copilot.sqlite3")
        return self._store, self._data_dir

    def _terminal_runs(self):
        store, _ = self._ensure()
        runs = [r for r in store.all_runs() if r.terminal]
        runs.sort(key=lambda r: r.settled_at or r.submitted_at or 0)
        return runs

    def enqueue_missing_archives(self) -> int:
        """未归档终态 run → archive 作业（幂等）。

        先恢复崩溃遗留的 running 作业（F09）：领取后中断的作业回到 pending，
        本轮继续处理，不再永久卡死。
        """
        store, data_dir = self._ensure()
        recovered = store.recover_stale_running()
        if recovered:
            print(f"[settlement] 恢复 {recovered} 个中断作业为 pending",
                  flush=True)
        n = 0
        for run in self._terminal_runs():
            if hud_api.is_archived(data_dir, run):
                continue
            if store.enqueue_job(
                    "archive", f"archive:{run.run_id}",
                    {"run_id": run.run_id}) is not None:
                n += 1
        return n

    def enqueue_repairs(self) -> int:
        """可修复的历史裁剪归档 → repair 作业（有界，游标推进）。

        F11：持久化扫描游标（settled_at|run_id），预算消耗在未核验候选上；
        一轮看完全部候选后清游标从头再扫（周期性全量复核）。
        """
        store, data_dir = self._ensure()
        runs = self._terminal_runs()
        raw = store.get_meta(_REPAIR_CURSOR_KEY) or ""
        start = 0
        if raw:
            try:
                cs, cid = raw.split("|", 1)
                cs = float(cs)
            except ValueError:
                cs, cid = None, None
            if cs is not None:
                for i, r in enumerate(runs):
                    if (r.settled_at or 0) == cs and r.run_id == cid:
                        start = i + 1
                        break
                # 游标指向的 run 已不存在（被清理）：从头重扫
        candidates = runs[start:]
        if not candidates:
            candidates = runs  # 走完全部候选：环绕重扫
        n = 0
        examined = 0
        last = None
        for run in candidates:
            if examined >= self._repair_scan_limit:
                break
            if not hud_api.is_archived(data_dir, run):
                examined += 1
                last = run
                continue  # 未归档走 archive 队列
            d = archive.run_dir(data_dir, run.conversation_id, run.run_id,
                                run.submitted_at)
            existing = archive.load_current_record(d)
            if existing is None:
                examined += 1
                last = run
                continue
            rebuilt = self._build_record(run)
            if rebuilt is None or archive.needs_rebuild(existing, rebuilt) is None:
                examined += 1
                last = run
                continue
            if store.enqueue_job(
                    "repair", f"repair:{run.run_id}",
                    {"run_id": run.run_id}) is not None:
                n += 1
            examined += 1
            last = run
        if examined < self._repair_scan_limit:
            store.set_meta(_REPAIR_CURSOR_KEY, "")  # 候选已看尽：下轮从头
        elif last is not None:
            store.set_meta(_REPAIR_CURSOR_KEY,
                           f"{last.settled_at or 0}|{last.run_id}")
        return n

    # ---- 作业执行（同步可测） ----

    def process_due(self) -> int:
        store, _ = self._ensure()
        jobs = store.claim_due_jobs(limit=self._claim_limit)
        for job in jobs:
            try:
                ok, error = self._execute(job)
            except Exception as exc:  # 单作业失败不拖垮循环
                ok, error = False, f"{type(exc).__name__}: {exc}"
            state = store.finish_job(job["id"], ok=ok, error=error)
            if state == "failed":
                print(f"[settlement] 作业 {job['dedupe_key']} 重试超限失败："
                      f"{error[:200]}", flush=True)
        return len(jobs)

    def _execute(self, job: dict) -> tuple[bool, str]:
        store, data_dir = self._ensure()
        run_id = str(job.get("payload", {}).get("run_id") or "")
        run = store.get_run(run_id) if run_id else None
        if run is None:
            return True, ""  # run 已被清理：无事可做，直接完结
        if job["type"] == "archive":
            if hud_api.is_archived(data_dir, run):
                return True, ""
            hud_api.archive_one(data_dir, run, self._find_events,
                                self._find_conversation)
            return True, ""
        if job["type"] == "repair":
            rebuilt = self._build_record(run)
            if rebuilt is None:
                return True, ""
            d = archive.run_dir(data_dir, run.conversation_id, run.run_id,
                                run.submitted_at)
            existing = archive.load_current_record(d)
            reason = (archive.needs_rebuild(existing, rebuilt)
                      if existing is not None else None)
            if reason is None:
                return True, ""  # 已修复或无可修复缺口
            archive.archive_run_new_version(data_dir, rebuilt, reason)
            return True, ""
        return True, ""  # 未知类型：完结，不无限重试

    # ---- 记录构建（与 hud_api.archive_one 同源） ----

    def _build_record(self, run) -> Optional[dict[str, Any]]:
        store, data_dir = self._ensure()
        run_public = run.to_public()
        events = [hud_api._event_raw(e)
                  for e in self._find_events(run.conversation_id, run.run_id)]
        return archive.build_run_record(
            run_public,
            self._find_conversation(run.conversation_id) or
            {"conversation_id": run.conversation_id, "title": "", "cwd": ""},
            events,
            archive.find_scene_package(data_dir, run_public),
        )

    def _find_events(self, conversation_id: str, run_id: str,
                     *, page: int = EVENT_PAGE) -> list:
        """按 seq 分页读完整会话事件再筛 run（F10）。

        先截会话前 N 条再筛会丢长会话后部轮次——分页读全，页大小只限制
        单次内存，不限制总条数。
        """
        store, _ = self._ensure()
        out = []
        after = 0
        while True:
            batch = store.events_for_conversation(
                conversation_id, after_seq=after, limit=page)
            if not batch:
                break
            out.extend(e for e in batch if e.run_id == run_id)
            after = batch[-1].seq
            if len(batch) < page:
                break
        return out

    def _find_conversation(self, conversation_id: str) -> dict:
        store, _ = self._ensure()
        conv = store.get_conversation(conversation_id)
        if conv is None:
            return {}
        return {"conversation_id": conv.conversation_id, "title": conv.title,
                "cwd": conv.cwd}


# ---- 进程级单例（server.py 接线用） ----

_worker: Optional[SettlementWorker] = None
_singleton_lock = threading.Lock()


def start_default(controller_provider, data_dir_factory) -> SettlementWorker:
    global _worker
    with _singleton_lock:
        if _worker is None or not _worker.running:
            _worker = SettlementWorker(controller_provider, data_dir_factory)
            _worker.start()
        return _worker


def shutdown_default() -> None:
    global _worker
    with _singleton_lock:
        if _worker is not None:
            _worker.stop()
            _worker = None
