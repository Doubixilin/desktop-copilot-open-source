# -*- coding: utf-8 -*-
"""archive：完整性校验/版本化不覆盖/检索/证据/四态反馈。全离线纯文件。"""
import json

import pytest

from copilot import archive


def run_pub(run_id="run-1", state="SETTLED", user_text="帮我整理", **kw):
    return {"run_id": run_id, "conversation_id": "conv-1", "state": state,
            "stop_reason": kw.get("stop_reason"), "error_code": kw.get("error_code"),
            "submitted_at": 1791200000.0, "settled_at": 1791200060.0,
            "user_text": user_text}


def conv():
    return {"conversation_id": "conv-1", "title": "工作", "cwd": "D:/w"}


def events(texts=("开始处理", "已整理完毕"), tools=(("read_file", "done"),)):
    out = []
    seq = 0
    for t in texts:
        seq += 1
        out.append({"seq": seq, "run_id": "run-1", "kind": "assistant",
                    "payload": {"text": t}, "observed_at": 1.0})
    for title, status in tools:
        seq += 1
        out.append({"seq": seq, "run_id": "run-1", "kind": "tool",
                    "payload": {"title": title, "status": status}, "observed_at": 1.0})
    return out


class TestValidate:
    def test_ok(self):
        v = archive.validate_output(run_pub(), {"assistant_texts": ["ok"], "tool_events": []}, None)
        assert v["completeness"] == "ok" and v["caveats"] == []

    def test_empty_output_and_abnormal(self):
        v = archive.validate_output(
            run_pub(state="CANCELLED", error_code="x"),
            {"assistant_texts": [], "tool_events": []}, None)
        assert v["completeness"] == "empty"
        assert any("无助手输出" in c for c in v["caveats"])
        assert any("非正常结束" in c for c in v["caveats"])
        assert any("执行错误" in c for c in v["caveats"])

    def test_scene_coverage_caveat(self):
        v = archive.validate_output(
            run_pub(), {"assistant_texts": ["ok"], "tool_events": []},
            {"coverage": "partial"})
        assert v["completeness"] == "partial"
        assert any("覆盖" in c for c in v["caveats"])


class TestArchiveRoundtrip:
    def test_record_shape_and_hash(self, tmp_path):
        rec = archive.build_run_record(run_pub(), conv(), events(), None)
        assert rec["agent"] == "dsh(acp)"
        assert rec["output"]["final_text"] == "已整理完毕"
        assert rec["output"]["assistant_chars"] == 9  # 4+5 字
        assert rec["integrity"]["completeness"] == "ok"
        assert len(rec["content_sha256"]) == 64

    def test_write_once_never_overwrite(self, tmp_path):
        rec = archive.build_run_record(run_pub(), conv(), events(), None)
        p1 = archive.archive_run(tmp_path, rec)
        rec2 = archive.build_run_record(run_pub(user_text="改过的"), conv(), events(), None)
        p2 = archive.archive_run(tmp_path, rec2)
        assert p1 == p2
        assert "改过的" not in p1.read_text(encoding="utf-8")

    def test_scene_link_via_pkg_mark(self, tmp_path):
        pkg = {"package_id": "pkg-abc123",
               "scene": {"scenario": "ide", "coverage": "full"},
               "integrity": {"materials_count": 2, "caveats": []}}
        (tmp_path / "scene_packages").mkdir()
        (tmp_path / "scene_packages" / "pkg-abc123.json").write_text(
            json.dumps(pkg, ensure_ascii=False), encoding="utf-8")
        run = run_pub(user_text="[现场包 pkg-abc123 | 场景=ide 覆盖=full]\n...\n\n帮我整理")
        found = archive.find_scene_package(tmp_path, run)
        assert found and found["package_id"] == "pkg-abc123"
        rec = archive.build_run_record(run, conv(), events(), found)
        assert rec["scene"]["package_id"] == "pkg-abc123"
        assert rec["scene"]["materials_count"] == 2


class TestSearchGetEvidence:
    def _seed(self, tmp_path):
        recs = [
            archive.build_run_record(run_pub("run-1"), conv(), events(), None),
            archive.build_run_record(run_pub("run-2", user_text="审查合同要点"),
                                     conv(), events(("开始",)), None),
        ]
        for r in recs:
            archive.archive_run(tmp_path, r)

    def test_search_hit_and_miss(self, tmp_path):
        self._seed(tmp_path)
        out = archive.context_search(tmp_path, "合同")
        assert out["hit_count"] == 1 and out["hits"][0]["run_id"] == "run-2"
        assert "合同" in out["hits"][0]["snippet"]
        out = archive.context_search(tmp_path, "不存在的东西")
        assert out["hit_count"] == 0 and "空结果不代表" in out["note"]

    def test_get_with_budget_and_feedback(self, tmp_path):
        self._seed(tmp_path)
        big_events = events(("x" * 20000,))
        rec = archive.build_run_record(run_pub("run-9"), conv(), big_events, None)
        archive.archive_run(tmp_path, rec)
        out = archive.context_get(tmp_path, "run-9")
        text = out["output"]["assistant_texts"][0]
        assert len(text) <= archive.GET_TEXT_BUDGET + 10
        assert out["feedback"] is None
        # 反馈后可读
        r = archive.save_feedback(tmp_path, "run-9", "modified", "改了两处措辞")
        assert r["ok"]
        assert archive.context_get(tmp_path, "run-9")["feedback"]["verdict"] == "modified"

    def test_get_missing(self, tmp_path):
        assert archive.context_get(tmp_path, "nope")["error"]["code"] == "not_found"

    def test_evidence(self, tmp_path):
        self._seed(tmp_path)
        out = archive.context_evidence(tmp_path, "run-1")
        assert out["integrity"]["completeness"] == "ok"
        assert out["tool_events"][0]["title"] == "read_file"
        assert out["scene"] is None and out["materials"] == []


class TestFeedback:
    def test_verdicts_and_reject(self, tmp_path):
        rec = archive.build_run_record(run_pub(), conv(), events(), None)
        archive.archive_run(tmp_path, rec)
        assert archive.save_feedback(tmp_path, "run-1", "bogus")["error"]["code"] == "bad_verdict"
        assert archive.save_feedback(tmp_path, "nope", "accepted")["error"]["code"] == "not_found"
        assert archive.save_feedback(tmp_path, "run-1", "needs_review", "")["ok"]
        assert archive.get_feedback(tmp_path, "run-1")["verdict"] == "needs_review"
        # 反馈可更新（最新态生效），run.json 不被触碰
        archive.save_feedback(tmp_path, "run-1", "accepted", "确认采用")
        assert archive.get_feedback(tmp_path, "run-1")["note"] == "确认采用"


class TestIntegrityFields:
    """票03：完整性显式布尔字段——不把"有文字"等同于完整。"""

    def test_explicit_bools_ok(self):
        v = archive.validate_output(run_pub(), {"assistant_texts": ["ok"], "tool_events": []}, None)
        assert v["text_truncated"] is False
        assert v["abnormal_end"] is False
        assert v["materials_gap"] is False
        assert v["assistant_chars"] == 2

    def test_explicit_bools_gaps(self):
        v = archive.validate_output(
            run_pub(state="INTERRUPTED"),
            {"assistant_texts": ["部分输出…[截断]"], "tool_events": []},
            {"coverage": "partial"})
        assert v["text_truncated"] is True
        assert v["abnormal_end"] is True
        assert v["materials_gap"] is True
        assert v["completeness"] == "partial"

    def test_long_text_over_8000_kept_intact(self, tmp_path):
        """A12：>8000 字原文无损入档；投喂预算只影响读取面。"""
        text = "A" * 2000 + "B" * 2000 + "C" * 2000 + "D" * 2500 + "【END】"
        rec = archive.build_run_record(
            run_pub(), conv(),
            [{"seq": 1, "run_id": "run-1", "kind": "assistant",
              "payload": {"text": text}, "observed": 1.0}], None)
        assert rec["output"]["assistant_chars"] == len(text)
        assert rec["integrity"]["text_truncated"] is False
        p = archive.archive_run(tmp_path, rec)
        on_disk = json.loads(p.read_text(encoding="utf-8"))
        assert on_disk["output"]["final_text"] == text, "落盘无损"


class TestVersionedRepair:
    """票03：已有错误归档 → 新版本 + index 指针；旧档保留。"""

    def _write_v1(self, tmp_path, text):
        rec = archive.build_run_record(
            run_pub(), conv(),
            [{"seq": 1, "run_id": "run-1", "kind": "assistant",
              "payload": {"text": text}, "observed": 1.0}], None)
        archive.archive_run(tmp_path, rec)
        return rec

    def test_needs_rebuild_detection(self, tmp_path):
        full = "全文" * 100
        existing = self._write_v1(tmp_path, full[:2005] + "…[截断]")
        rebuilt = archive.build_run_record(
            run_pub(), conv(),
            [{"seq": 1, "run_id": "run-1", "kind": "assistant",
              "payload": {"text": full}, "observed": 1.0}], None)
        reason = archive.needs_rebuild(existing, rebuilt)
        assert reason and "裁剪" in reason
        # 完整档无需修复；字数相同亦无需
        assert archive.needs_rebuild(rebuilt, rebuilt) is None

    def test_new_version_writes_v2_and_index(self, tmp_path):
        existing = self._write_v1(tmp_path, "被裁剪的旧正文" + "…[截断]")
        d = archive.run_dir(tmp_path, "conv-1", "run-1", existing["submitted_at"])
        rebuilt = archive.build_run_record(
            run_pub(), conv(),
            [{"seq": 1, "run_id": "run-1", "kind": "assistant",
              "payload": {"text": "完整的旧正文"}, "observed": 1.0}], None)
        p2 = archive.archive_run_new_version(tmp_path, rebuilt, "测试修复原因")
        assert p2.name == "run.v2.json"
        assert (d / "run.json").exists(), "旧档保留"
        cur = archive.load_current_record(d)
        assert cur["output"]["final_text"] == "完整的旧正文"
        assert cur["repair"]["reason"] == "测试修复原因"
        assert cur["content_sha256"] and len(cur["content_sha256"]) == 64
        # 再次修复 → v3，v2 保留
        p3 = archive.archive_run_new_version(tmp_path, {
            **rebuilt,
            "output": {**rebuilt["output"], "final_text": "再次修复"}},
            "第二次修复")
        assert p3.name == "run.v3.json"
        idx = json.loads((d / "run.index.json").read_text(encoding="utf-8"))
        assert idx["current_version"] == 3
        assert [v["file"] for v in idx["versions"]] == [
            "run.json", "run.v2.json", "run.v3.json"]

    def test_search_prefers_current_version(self, tmp_path):
        existing = self._write_v1(tmp_path, "旧关键词被裁剪正文")
        rebuilt = archive.build_run_record(
            run_pub(user_text="查旧关键词"), conv(),
            [{"seq": 1, "run_id": "run-1", "kind": "assistant",
              "payload": {"text": "旧关键词 完整正文带新词Zebra"}, "observed": 1.0}],
            None)
        archive.archive_run_new_version(tmp_path, rebuilt, "裁剪修复")
        hits = archive.context_search(tmp_path, "zebra")["hits"]
        assert hits and hits[0]["run_id"] == "run-1"
        got = archive.context_get(tmp_path, "run-1")
        assert "Zebra" in got["output"]["final_text"]


class TestCrashTolerance:
    """票03：归档中崩溃——tmp 残留不阻塞重档，目标文件要么完整要么不存在。"""

    def test_stale_tmp_does_not_block_rearchive(self, tmp_path):
        rec = archive.build_run_record(run_pub(), conv(), events(), None)
        d = archive.run_dir(tmp_path, "conv-1", "run-1", rec["submitted_at"])
        d.mkdir(parents=True)
        (d / "run.json.tmp").write_text("崩溃残留的半截 JSON", encoding="utf-8")
        p = archive.archive_run(tmp_path, rec)
        assert p.exists()
        assert not (d / "run.json.tmp").exists(), "崩溃残留 tmp 被本次写入覆盖"
        # 重档幂等：已有完整档不再改写
        assert archive.archive_run(tmp_path, rec) == p

    def test_repair_index_write_atomic_leftover_tolerated(self, tmp_path):
        rec = archive.build_run_record(run_pub(), conv(), events(), None)
        archive.archive_run(tmp_path, rec)
        d = archive.run_dir(tmp_path, "conv-1", "run-1", rec["submitted_at"])
        (d / "run.index.json.tmp").write_text("半截 index", encoding="utf-8")
        p2 = archive.archive_run_new_version(
            tmp_path, archive.build_run_record(
                run_pub(user_text="修复版"), conv(), events(), None), "崩溃后重试修复")
        assert p2.name == "run.v2.json"
        assert not (d / "run.index.json.tmp").exists()
        idx = json.loads((d / "run.index.json").read_text(encoding="utf-8"))
        assert idx["current_version"] == 2


class TestTruncatedCompleteness:
    """复核 F12：已知截断必须压成 partial 并写明原因，不得 ok。"""

    def test_truncated_text_is_partial_with_caveat(self):
        v = archive.validate_output(
            run_pub(), {"assistant_texts": ["开头" + "内" * 20 + "…[截断]"],
                        "tool_events": []}, None)
        assert v["text_truncated"] is True
        assert v["completeness"] == "partial"
        assert any("截断" in c for c in v["caveats"])

    def test_hard_cap_marker_also_partial(self):
        v = archive.validate_output(
            run_pub(), {"assistant_texts": ["超长" + "…[归档硬顶截断]"],
                        "tool_events": []}, None)
        assert v["text_truncated"] is True
        assert v["completeness"] == "partial"

    def test_clean_text_still_ok(self):
        v = archive.validate_output(run_pub(),
                                    {"assistant_texts": ["干净正文"], "tool_events": []}, None)
        assert v["text_truncated"] is False
        assert v["completeness"] == "ok"
        assert v["caveats"] == []
