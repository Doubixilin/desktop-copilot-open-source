# -*- coding: utf-8 -*-
"""hud_api 归档 tick 与 feedback——真 CopilotStore + 假事件。全离线。"""
from copilot import hud_api, models
from copilot.store import CopilotStore


def _mk_store(tmp_path, conv, runs, events):
    store = CopilotStore(tmp_path / "copilot.sqlite3")
    store.upsert_conversation(conv)
    for r in runs:
        store.save_run(r)
    for e in events:
        store.append_event(e)
    return store


def _fixtures(now=1791200000.0):
    conv = models.Conversation(
        conversation_id="conv-1", runtime_fingerprint="fp", dsh_home="h",
        cwd="D:/w", created_at=now, last_activity_at=now, title="档案会话")
    r1 = models.Run(run_id="run-1", conversation_id="conv-1",
                    state=models.RunState.SETTLED, submitted_at=now,
                    last_event_at=now + 5, settled_at=now + 5, user_text="任务一")
    r2 = models.Run(run_id="run-2", conversation_id="conv-1",
                    state=models.RunState.RUNNING, submitted_at=now + 10,
                    last_event_at=now + 10, user_text="任务二（未终态）")
    ev1 = models.EventRecord(seq=1, run_id="run-1", conversation_id="conv-1",
                             runtime_epoch=0, kind="user", payload={"text": "任务一"},
                             observed_at=now)
    ev2 = models.EventRecord(seq=2, run_id="run-1", conversation_id="conv-1",
                             runtime_epoch=0, kind="assistant",
                             payload={"text": "结果一"}, observed_at=now + 5)
    return conv, [r1, r2], [ev1, ev2]


class TestArchiveTick:
    def test_archives_only_terminal(self, tmp_path):
        conv, runs, events = _fixtures()
        store = _mk_store(tmp_path, conv, runs, events)
        n = hud_api.archive_tick(
            tmp_path, store.all_runs(),
            lambda cid, rid: [e for e in store.events_for_conversation(cid)
                              if e.run_id == rid],
            lambda cid: {"title": conv.title, "cwd": conv.cwd,
                         "conversation_id": cid},
        )
        assert n == 1  # 只归档终态 run-1，RUNNING 的 run-2 不动
        import json as _json

        record = _json.loads(
            (tmp_path / "archive").rglob("run.json").__next__().read_text("utf-8"))
        assert record["run_id"] == "run-1"
        assert record["conversation_title"] == "档案会话"
        assert record["output"]["final_text"] == "结果一"
        # 幂等：再 tick 零新档
        n2 = hud_api.archive_tick(
            tmp_path, store.all_runs(),
            lambda cid, rid: [e for e in store.events_for_conversation(cid)
                              if e.run_id == rid],
            lambda cid: {})
        assert n2 == 0


class TestFeedbackApi:
    def test_feedback_roundtrip(self, tmp_path):
        conv, runs, events = _fixtures()
        store = _mk_store(tmp_path, conv, runs, events)
        hud_api.archive_tick(
            tmp_path, store.all_runs(),
            lambda cid, rid: [e for e in store.events_for_conversation(cid)
                              if e.run_id == rid],
            lambda cid: {})
        out = hud_api.save_feedback(tmp_path, {"run_id": "run-1",
                                               "verdict": "accepted",
                                               "note": "好"})
        assert out["ok"]
        assert out["feedback"]["verdict"] == "accepted"
        # 缺 run_id / 坏 verdict / 未归档
        assert hud_api.save_feedback(tmp_path, {"verdict": "accepted"})["error"]["code"] == "bad_request"
        assert hud_api.save_feedback(tmp_path, {"run_id": "run-1", "verdict": "zzz"})["error"]["code"] == "bad_verdict"
        assert hud_api.save_feedback(tmp_path, {"run_id": "ghost", "verdict": "accepted"})["error"]["code"] == "not_found"


class TestArchiveFidelity:
    """票03（B06/R06/A12）：归档读取原始事件——4518 字与 >8000 字无损，
    展示裁剪（_event_public 2000 字）只属于显示面，不得进档案。"""

    def _tick(self, tmp_path, store, conv):
        return hud_api.archive_tick(
            tmp_path, store.all_runs(),
            lambda cid, rid: [e for e in store.events_for_conversation(cid)
                              if e.run_id == rid],
            lambda cid: {"title": conv.title, "cwd": conv.cwd,
                         "conversation_id": cid},
        )

    def test_4518_chars_full_text_archived(self, tmp_path):
        text = "前" * 4513 + "【尾标记】"  # 4513+5=4518，复现 B06 量级
        assert len(text) == 4518 and len(text) > 2000
        conv, runs, events = _fixtures()
        events = events + [models.EventRecord(
            seq=3, run_id="run-1", conversation_id="conv-1", runtime_epoch=0,
            kind="assistant", payload={"text": text}, observed_at=1791200002.0)]
        store = _mk_store(tmp_path, conv, runs, events)
        assert self._tick(tmp_path, store, conv) == 1
        import json as _json
        rec = _json.loads(next((tmp_path / "archive").rglob("run.json")).read_text("utf-8"))
        # 尾事件全文入档（多段 assistant 拼接保留，不裁剪）
        joined = "".join(rec["output"]["assistant_texts"])
        assert text in joined and joined.endswith("【尾标记】")
        assert rec["integrity"]["text_truncated"] is False
        # 旧缺陷对照：展示序列化会裁成 2005 字并丢失尾标记
        clipped = hud_api._event_public(events[-1])
        assert len(clipped["payload"]["text"]) < len(text)

    def test_over_8000_chars_archived_intact(self, tmp_path):
        text = "长" * 9000 + "【END】"
        conv, runs, events = _fixtures()
        events = [events[0]] + [models.EventRecord(
            seq=2, run_id="run-1", conversation_id="conv-1", runtime_epoch=0,
            kind="assistant", payload={"text": text}, observed_at=1791200001.0)]
        store = _mk_store(tmp_path, conv, runs, events)
        self._tick(tmp_path, store, conv)
        import json as _json
        rec = _json.loads(next((tmp_path / "archive").rglob("run.json")).read_text("utf-8"))
        assert rec["output"]["assistant_chars"] == len(text)
        assert rec["output"]["final_text"].endswith("【END】")
