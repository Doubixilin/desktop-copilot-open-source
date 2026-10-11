# -*- coding: utf-8 -*-
"""settlement：结算驱动归档/通知接缝/有界重试/启动补偿/版本化修复。

票03（B06 B07 / R06 R07 / A12）：终态轮次不开 HUD 也归档；原始回复不被
展示裁剪损坏；历史裁剪归档修复为新版本且旧档保留。
"""
import json
import time

import pytest

from copilot import archive, hud_api, models, settlement
from copilot.store import CopilotStore


class FakeController:
    """只提供 subscribe 捕获——通知接缝测试用。"""

    def __init__(self):
        self.callbacks = []

    def subscribe(self, cb):
        self.callbacks.append(cb)


def seed_store(tmp_path, *, with_running=False):
    """conv-1 + 终态 run-1（4518 字含尾标记）+ 可选在途 run-2。"""
    store = CopilotStore(tmp_path / "copilot.sqlite3")
    conv = models.Conversation(
        conversation_id="conv-1", runtime_fingerprint="fp", dsh_home="h",
        cwd="D:/w", created_at=1.0, last_activity_at=2.0, title="结算会话")
    store.upsert_conversation(conv)
    text = "结论先行。" + "论据与推演。" * 450 + "【尾标记END】"
    store.save_run(models.Run(
        run_id="run-1", conversation_id="conv-1",
        state=models.RunState.SETTLED, submitted_at=1791200000.0,
        last_event_at=1791200060.0, settled_at=1791200060.0, user_text="任务一"))
    store.append_event(models.EventRecord(
        seq=1, run_id="run-1", conversation_id="conv-1", runtime_epoch=0,
        kind="user", payload={"text": "任务一"}, observed_at=1.0))
    store.append_event(models.EventRecord(
        seq=2, run_id="run-1", conversation_id="conv-1", runtime_epoch=0,
        kind="assistant", payload={"text": text}, observed_at=2.0))
    if with_running:
        store.save_run(models.Run(
            run_id="run-2", conversation_id="conv-1",
            state=models.RunState.RUNNING, submitted_at=1791200100.0,
            last_event_at=1791200100.0, user_text="任务二"))
    store.close()
    return text


def worker_for(tmp_path, **kw):
    ctl = FakeController()
    w = settlement.SettlementWorker(
        lambda: (ctl, None), lambda: tmp_path, **kw)
    return ctl, w


def read_record(tmp_path):
    return json.loads(
        next((tmp_path / "archive").rglob("run.json")).read_text("utf-8"))


class TestSettlementArchive:
    def test_terminal_archived_without_hud_full_text(self, tmp_path):
        """R07/R06：不开 HUD 由结算作业归档；长输出全文保真、尾标记在。"""
        text = seed_store(tmp_path)
        _ctl, w = worker_for(tmp_path)
        assert w.enqueue_missing_archives() == 1
        pending = w._store.list_jobs(state="pending")
        assert [j["dedupe_key"] for j in pending] == ["archive:run-1"]
        assert w.process_due() == 1

        rec = read_record(tmp_path)
        assert rec["output"]["assistant_texts"][0] == text, "全文无损"
        assert rec["output"]["assistant_chars"] == len(text)
        assert rec["output"]["final_text"].endswith("【尾标记END】")
        assert rec["integrity"]["text_truncated"] is False
        assert rec["integrity"]["abnormal_end"] is False
        done = w._store.list_jobs(state="done")
        assert [j["dedupe_key"] for j in done] == ["archive:run-1"]

    def test_settlement_idempotent_repeat(self, tmp_path):
        """重复结算（补偿+重连）不重复归档：run.json 存在即幂等。"""
        seed_store(tmp_path)
        _ctl, w = worker_for(tmp_path)
        w.enqueue_missing_archives()
        w.process_due()
        assert w.enqueue_missing_archives() == 0
        assert w.process_due() == 0
        assert read_record(tmp_path)["run_id"] == "run-1"

    def test_running_run_not_enqueued(self, tmp_path):
        seed_store(tmp_path, with_running=True)
        _ctl, w = worker_for(tmp_path)
        assert w.enqueue_missing_archives() == 1, "只有终态 run-1 入队"
        keys = [j["dedupe_key"] for j in w._store.list_jobs(state="pending")]
        assert keys == ["archive:run-1"], "在途 run-2 不入结算队列"

    def test_notify_settle_kinds_trigger_enqueue(self, tmp_path):
        """通知接缝：结算类通知入队；无关通知不动。"""
        seed_store(tmp_path)
        ctl, w = worker_for(tmp_path)
        w._ensure()
        ctl.subscribe(w._on_notify)  # 线程内订阅 seam 的等价注入
        ctl.callbacks[0]("run_settled")
        assert len(w._store.list_jobs(state="pending")) == 1
        ctl.callbacks[0]("run_state")  # 非结算通知
        ctl.callbacks[0]("runtime_started")
        assert len(w._store.list_jobs(state="pending")) == 1


class TestRetryBounded:
    def test_archive_failure_retries_with_backoff_then_failed(self, tmp_path):
        """失败可观察且有界重试：指数退避，超限标 failed；入队可复活。"""
        seed_store(tmp_path)
        _ctl, w = worker_for(tmp_path)
        w.enqueue_missing_archives()
        store = w._store
        job_id = store.list_jobs(state="pending")[0]["id"]

        states = []
        for attempt in range(1, 6):
            jobs = store.claim_due_jobs(now=time.time() + attempt * 1000)
            assert len(jobs) == 1 and jobs[0]["id"] == job_id
            states.append(store.finish_job(
                job_id, ok=False, error=f"disk full #{attempt}"))
        assert states == ["retry", "retry", "retry", "retry", "failed"]
        failed = store.list_jobs(state="failed")[0]
        assert "disk full #5" in failed["last_error"], "失败原因可见"

        # failed 后入队复活：复用原 id、立即可领取重试
        job_id2 = store.enqueue_job("archive", "archive:run-1",
                                    {"run_id": "run-1"})
        assert job_id2 == job_id, "failed 作业复活复用原 id"
        assert store.claim_due_jobs(now=time.time()), "复活后立即可领取"

    def test_unknown_job_type_completes_without_retry(self, tmp_path):
        seed_store(tmp_path)
        _ctl, w = worker_for(tmp_path)
        w._ensure()
        store = w._store
        store.enqueue_job("nonsense", "nonsense:1", {"run_id": "ghost"})
        assert w.process_due() == 1
        assert [j["state"] for j in store.list_jobs()] == ["done"]


class TestStartupCompensation:
    def test_repair_clipped_archive_to_new_version(self, tmp_path):
        """B06 修复：历史裁剪归档 → 新版本全文；旧档保留；检索读当前版本。"""
        text = seed_store(tmp_path)
        # 复现历史缺陷：用展示裁剪（_event_public 2000 字）后的 events 归档
        store = CopilotStore(tmp_path / "copilot.sqlite3")
        run = store.get_run("run-1")
        raw_events = [e for e in store.events_for_conversation("conv-1")
                      if e.run_id == "run-1"]
        clipped = [hud_api._event_public(e) for e in raw_events]
        old = archive.build_run_record(
            run.to_public(),
            {"conversation_id": "conv-1", "title": "结算会话", "cwd": "D:/w"},
            clipped, None)
        archive.archive_run(tmp_path, old)
        store.close()
        assert old["output"]["assistant_chars"] < len(text), "前置：旧档确实被裁"

        _ctl, w = worker_for(tmp_path)
        assert w.enqueue_repairs() == 1, "可修复缺口入 repair 队"
        assert w.process_due() == 1

        d = archive.run_dir(tmp_path, "conv-1", "run-1", run.submitted_at)
        assert (d / "run.json").exists(), "旧档保留不修写"
        assert (d / "run.v2.json").exists(), "新版本落盘"
        cur = archive.load_current_record(d)
        assert cur["output"]["assistant_chars"] == len(text)
        assert cur["output"]["final_text"].endswith("【尾标记END】")
        assert cur["repair"]["version"] == 2
        assert "裁剪" in cur["repair"]["reason"]
        # 检索面（context_get）读当前版本全文
        got = archive.context_get(tmp_path, "run-1")
        assert got["output"]["final_text"].endswith("【尾标记END】")
        # 反馈仍可用（feedback.json 与版本无关）
        assert hud_api.save_feedback(tmp_path, {"run_id": "run-1",
                                                "verdict": "accepted"})["ok"]
        # 修复幂等：无缺口不再入队
        _ctl2, w2 = worker_for(tmp_path)
        assert w2.enqueue_repairs() == 0

    def test_intact_archive_not_repaired(self, tmp_path):
        seed_store(tmp_path)
        _ctl, w = worker_for(tmp_path)
        w.enqueue_missing_archives()
        w.process_due()
        _ctl2, w2 = worker_for(tmp_path)
        assert w2.enqueue_repairs() == 0, "保真归档不触发修复"


class TestStoreMigration:
    def _make_v1_db(self, tmp_path):
        db = tmp_path / "copilot.sqlite3"
        store = CopilotStore(db)  # 当前版本
        with store._conn:
            store._conn.execute(
                "UPDATE meta SET value='1' WHERE key='schema_version'")
            store._conn.execute("DROP TABLE jobs")
        store.close()
        assert not list(tmp_path.glob("*.bak-*"))
        return db

    def test_v1_migrates_to_v2_with_backup(self, tmp_path):
        db = self._make_v1_db(tmp_path)
        store = CopilotStore(db)  # 触发迁移
        assert store.list_jobs() == []
        version = store._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"]
        assert int(version) == 2
        backups = list(tmp_path.glob("copilot.sqlite3.bak-*"))
        assert backups, "迁移前必须有一致性备份"
        store.close()

    def test_future_schema_rejected(self, tmp_path):
        db = tmp_path / "copilot.sqlite3"
        store = CopilotStore(db)
        with store._conn:
            store._conn.execute(
                "UPDATE meta SET value='99' WHERE key='schema_version'")
        store.close()
        with pytest.raises(RuntimeError, match="拒绝启动"):
            CopilotStore(db)


class TestJobQueue:
    def test_enqueue_dedupe_and_revive(self, tmp_path):
        store = CopilotStore(tmp_path / "copilot.sqlite3")
        j1 = store.enqueue_job("archive", "archive:r1", {"run_id": "r1"})
        assert j1 is not None
        assert store.enqueue_job("archive", "archive:r1") is None, "pending 去重"
        # done 后仍去重
        store.claim_due_jobs()
        store.finish_job(j1, ok=True)
        assert store.enqueue_job("archive", "archive:r1") is None
        # failed 可复活（同 id）
        j2 = store.enqueue_job("archive", "archive:r2")
        store.claim_due_jobs()
        for _ in range(5):
            store.finish_job(j2, ok=False, error="x")
        assert store.list_jobs(state="failed")
        assert store.enqueue_job("archive", "archive:r2") == j2
        assert store.list_jobs(state="pending")
        store.close()

    def test_claim_marks_running_and_respects_due(self, tmp_path):
        store = CopilotStore(tmp_path / "copilot.sqlite3")
        store.enqueue_job("archive", "archive:r1")
        now = time.time()
        due = store.claim_due_jobs(now=now + 10)
        assert len(due) == 1 and due[0]["type"] == "archive"
        assert store.list_jobs(state="running")
        # 未到期不领取
        store.enqueue_job("archive", "archive:r2", )
        store._conn.execute(
            "UPDATE jobs SET next_attempt_at=? WHERE dedupe_key='archive:r2'",
            (now + 3600,))
        store._conn.commit()
        assert store.claim_due_jobs(now=now + 10) == []
        store.close()


class TestReviewFixes:
    """复核 F09/F10/F11：中断作业恢复、长会话分页读全、修复游标推进。"""

    def test_claimed_job_recovered_after_crash(self, tmp_path):
        """F09：领取后进程中断（running 遗留）→ 新 worker 恢复并完成归档。"""
        text = seed_store(tmp_path)
        _ctl, w = worker_for(tmp_path)
        w.enqueue_missing_archives()
        assert len(w._store.claim_due_jobs()) == 1  # 崩溃缝：已领取、未 finish
        w._store.close()

        _c2, w2 = worker_for(tmp_path)
        assert w2.enqueue_missing_archives() == 0, "恢复不重复入队"
        assert w2.process_due() == 1, "恢复的 running 作业重新可领取"
        rec = read_record(tmp_path)
        assert rec["output"]["assistant_texts"][0] == text
        assert all(j["state"] == "done" for j in w2._store.list_jobs(limit=10))

    def test_find_events_pages_long_conversations(self, tmp_path):
        """F10：事件按 seq 分页读全——先截会话前 N 条会丢后部轮次。"""
        text = seed_store(tmp_path)
        store = CopilotStore(tmp_path / "copilot.sqlite3")
        # 单元层：小页分页，12 条填充 + 目标轮事件跨页
        with store._conn:
            store._conn.executemany(
                "INSERT INTO events(run_id,conversation_id,runtime_epoch,kind,"
                "payload_json,observed_at) VALUES (?,?,?,?,?,?)",
                [("prev-run", "conv-1", 0, "note", '{"text":"旧"}', 0.5)] * 12)
        store.close()

        _ctl, w = worker_for(tmp_path)
        found = w._find_events("conv-1", "run-1", page=5)  # 3 页读取
        kinds = [e.kind for e in found]
        assert kinds == ["user", "assistant"], "目标轮事件完整跨页取回"
        assert found[-1].payload["text"] == text

    def test_long_conversation_archives_tail_run(self, tmp_path):
        """F10 集成：>页大小填充事件后，当前轮正文完整入档。"""
        text = seed_store(tmp_path)
        store = CopilotStore(tmp_path / "copilot.sqlite3")
        with store._conn:
            store._conn.executemany(
                "INSERT INTO events(run_id,conversation_id,runtime_epoch,kind,"
                "payload_json,observed_at) VALUES (?,?,?,?,?,?)",
                [("prev-run", "conv-1", 0, "note", '{"text":"旧"}', 0.5)] * 1005)
        store.close()
        _ctl, w = worker_for(tmp_path)
        w.enqueue_missing_archives()
        assert w.process_due() == 1
        rec = read_record(tmp_path)
        assert rec["output"]["assistant_chars"] == len(text), "后部轮次正文不得丢"
        assert rec["output"]["final_text"].endswith("【尾标记END】")

    def test_repair_cursor_advances_across_restarts(self, tmp_path):
        """F11：游标推进——第三次启动能覆盖第三档，不再重复扫描同一段。"""
        store = CopilotStore(tmp_path / "copilot.sqlite3")
        store.upsert_conversation(models.Conversation(
            conversation_id="conv-1", runtime_fingerprint="fp", dsh_home="h",
            cwd="D:/w", created_at=1.0, last_activity_at=2.0, title="游标"))
        for i in range(3):
            run = models.Run(
                run_id=f"r{i}", conversation_id="conv-1",
                state=models.RunState.SETTLED, submitted_at=1791200000.0 + i,
                last_event_at=1.0, settled_at=100.0 + i, user_text=f"t{i}")
            store.save_run(run)
            store.append_event(models.EventRecord(
                seq=i + 1, run_id=f"r{i}", conversation_id="conv-1",
                runtime_epoch=0, kind="assistant",
                payload={"text": f"正文{i}"}, observed_at=1.0))
            rec = archive.build_run_record(run.to_public(),
                                           {"conversation_id": "conv-1"}, [
                {"seq": i + 1, "run_id": f"r{i}", "kind": "assistant",
                 "payload": {"text": f"正文{i}"}, "observed_at": 1.0}], None)
            if i == 2:  # 第三档模拟历史裁剪
                rec["output"]["assistant_texts"] = []
                rec["output"]["assistant_chars"] = 0
                rec["integrity"] = archive.validate_output(
                    run.to_public(), rec["output"], None)
            archive.archive_run(tmp_path, rec)
        store.close()

        scans = []
        for _ in range(2):
            _ctl, w = worker_for(tmp_path, repair_scan_limit=2)
            scans.append(w.enqueue_repairs())
            w._store.close()
        assert scans[0] == 0, "前两档完整：本轮零入队，游标前移"
        assert scans[1] == 1, "第二轮必须覆盖第三档（游标推进后不再重扫前段）"
        # 修复执行后，后续启动不再入队
        _c3, w3 = worker_for(tmp_path, repair_scan_limit=2)
        assert w3.process_due() >= 0
        w3.enqueue_missing_archives()
        w3.process_due()
        w3._store.close()
        _c4, w4 = worker_for(tmp_path, repair_scan_limit=2)
        assert w4.enqueue_repairs() == 0
