# -*- coding: utf-8 -*-
"""scene_package：冻结/落盘/sha256/完整性/注入块 + 带包发起端到端。"""
import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, "native_hud")
from copilot.scene_package import (  # noqa: E402
    SCENE_PACKAGE_SCHEMA,
    freeze_scene_package,
    load_scene_package,
    render_scene_block,
    verify_package_materials,
)
from copilot.cli_agents import AgentConfig  # noqa: E402
from copilot.generic_cli import GenericCliAdapter  # noqa: E402
from copilot.tasks import TaskPool  # noqa: E402

MOCK = str(Path(__file__).parent / "fixtures" / "mock_cli_agent.py")


def probe_payload(materials, scenario="ide", coverage="full", note="",
                  identity=None):
    return {
        "scenario": scenario, "coverage": coverage, "note": note,
        "identity": identity or {"pid": 123, "process": "Code.exe",
                                 "exe": r"C:\Code.exe", "title": "server.py — VS Code",
                                 "class": "Chrome_WidgetWin_1", "ts": 1791200000.0},
        "materials": materials,
    }


def material(kind="selection", text="print('hi')", source="uia:text", ts=1791200001.0):
    return {"kind": kind, "source": source, "chars": len(text),
            "preview": text[:80], "text": text, "ts": ts}


class TestFreeze:
    def test_fields_and_sha256(self, tmp_path):
        pkg = freeze_scene_package("帮我看看这段", probe_payload(
            [material(), material("file", "server.py", "title")]),
            package_dir=tmp_path)
        assert pkg["schema"] == SCENE_PACKAGE_SCHEMA
        assert pkg["task"]["prompt"] == "帮我看看这段"
        assert pkg["scene"]["scenario"] == "ide"
        assert pkg["scene"]["identity"]["title"].startswith("server.py")
        assert len(pkg["materials"]) == 2
        m0 = pkg["materials"][0]
        assert m0["tool"] == "UIA TextPattern"
        assert m0["sha256"] == hashlib.sha256("print('hi')".encode()).hexdigest()
        assert pkg["integrity"]["materials_count"] == 2
        assert pkg["integrity"]["chars_total"] == len("print('hi')") + len("server.py")

    def test_caveats_explicit(self, tmp_path):
        pkg = freeze_scene_package("p", probe_payload(
            [], scenario="generic", coverage="none", note="未识别场景：仅现场身份"),
            package_dir=tmp_path)
        caveats = pkg["integrity"]["caveats"]
        assert any("无材料" in c for c in caveats)
        assert any("未识别场景" in c for c in caveats)
        assert any("覆盖" in c for c in caveats)

    def test_bad_material_recorded_missing(self, tmp_path):
        payload = probe_payload([{"kind": "x"}, "junk", material()])
        pkg = freeze_scene_package("p", payload, package_dir=tmp_path)
        assert len(pkg["materials"]) == 1

    def test_none_probe_freezes_identity_only(self, tmp_path):
        pkg = freeze_scene_package("p", None, package_dir=tmp_path)
        assert pkg["materials"] == []
        assert pkg["scene"]["scenario"] == "unknown"

    def test_persist_and_reload(self, tmp_path):
        pkg = freeze_scene_package("p", probe_payload([material()]), package_dir=tmp_path)
        loaded = load_scene_package(tmp_path, pkg["package_id"])
        assert loaded == pkg
        assert verify_package_materials(loaded) == [True]
        # 完整性自证：改一个字即失配
        loaded["materials"][0]["text"] += "x"
        assert verify_package_materials(loaded) == [False]

    def test_atomic_no_tmp_left(self, tmp_path):
        freeze_scene_package("p", probe_payload([material()]), package_dir=tmp_path)
        assert not list(tmp_path.glob("*.tmp"))


class TestRenderBlock:
    def test_contains_materials_and_bounds(self, tmp_path):
        big1 = material("selection", "x" * 3000)
        big2 = material("terminal_tail", "y" * 3000, "uia:document")
        pkg = freeze_scene_package("p", probe_payload([big1, big2]),
                                   package_dir=tmp_path)
        block = render_scene_block(pkg)
        assert block.startswith("[现场包 pkg-")
        assert "场景=ide" in block and "现场：Code.exe" in block
        assert "材料[selection]" in block and "材料[terminal_tail]" in block
        assert "[现场包结束]" in block
        # 3000+5 文本超 4000 预算 → 截断注明
        assert "已截断" in block
        assert len(block) < 4600

    def test_no_materials_block(self, tmp_path):
        pkg = freeze_scene_package("p", None, package_dir=tmp_path)
        block = render_scene_block(pkg)
        assert "[现场包结束]" in block and "材料" not in block


class TestSubmitWithScene:
    def _adapter(self, tmp_path):
        cfg = AgentConfig(
            name="mock-echo", program=sys.executable,
            args=[MOCK, "--mode", "echo", "{prompt}"], display="Mock",
        )
        pool = TaskPool(None)
        return GenericCliAdapter({cfg.name: cfg}, pool, tmp_path / "logs",
                                 max_concurrent=2), pool

    def test_prompt_injected_and_package_persisted(self, tmp_path):
        adapter, pool = self._adapter(tmp_path)
        try:
            scene = probe_payload([material("selection", "SELECTED_TEXT_你好"),
                                   material("file", "server.py", "title")])
            task = adapter.submit("mock-echo", "解释这段", scene=scene)
            deadline = time.time() + 15
            t = None
            while time.time() < deadline:
                t = pool.find("cli", task.ref_id)
                if t and t.status in ("ready", "blocked"):
                    break
                time.sleep(0.05)
            assert t is not None and t.status == "ready", t and (t.status, t.note)
            # echo 把最终 prompt 原样回吐；prompt 含换行 → 逐行覆盖 last_text，
            # 断言读任务日志全文（echo 输出 = 最终 prompt）
            log_text = Path(t.log_path).read_text(encoding="utf-8")
            assert "SELECTED_TEXT_你好" in log_text
            assert "[现场包 pkg-" in log_text and "解释这段" in log_text
            # 任务与包互相可追溯
            pkg_id = t.extra["scene_package_id"]
            assert t.extra["scene_scenario"] == "ide"
            assert t.extra["scene_materials"] == 2
            pkg = load_scene_package(tmp_path / "logs" / "packages", pkg_id)
            assert pkg is not None
            assert verify_package_materials(pkg) == [True, True]
            assert pkg["task"]["prompt"] == "解释这段"  # 原始 prompt 未被现场块污染
        finally:
            adapter.shutdown()

    def test_submit_without_scene_unchanged(self, tmp_path):
        adapter, pool = self._adapter(tmp_path)
        try:
            task = adapter.submit("mock-echo", "plain")
            deadline = time.time() + 15
            t = None
            while time.time() < deadline:
                t = pool.find("cli", task.ref_id)
                if t and t.status in ("ready", "blocked"):
                    break
                time.sleep(0.05)
            assert t.status == "ready"
            assert t.last_text == "ECHO|plain"
            assert "scene_package_id" not in t.extra
            assert not (tmp_path / "logs" / "packages").exists()
        finally:
            adapter.shutdown()


class TestSceneRounds:
    """票02：scene_id/generation 落包、浏览器身份结构预留、obtained 显式化。"""

    def test_round_metadata_frozen(self, tmp_path):
        pkg = freeze_scene_package("任务", probe_payload([material()]) | {
            "scene_id": "scene-3-ab", "generation": 3,
        }, package_dir=tmp_path)
        assert pkg["scene"]["scene_id"] == "scene-3-ab"
        assert pkg["scene"]["generation"] == 3
        assert " 代次=3" in render_scene_block(pkg)

    def test_round_metadata_absent_omitted(self, tmp_path):
        pkg = freeze_scene_package("任务", probe_payload([material()]),
                                   package_dir=tmp_path)
        assert "scene_id" not in pkg["scene"]
        assert "generation" not in pkg["scene"]
        assert "代次" not in render_scene_block(pkg)

    def test_browser_identity_reserved(self, tmp_path):
        ident = {"pid": None, "process": "chrome.exe", "title": "文章页",
                 "url": "https://example.invalid/a", "document_id": "doc-1",
                 "tab_id": 7, "content_revision": "sha-x"}
        pkg = freeze_scene_package(
            "任务", probe_payload([material()], identity=ident), package_dir=tmp_path)
        frozen = pkg["scene"]["identity"]
        assert frozen["url"] == "https://example.invalid/a"
        assert frozen["document_id"] == "doc-1"
        assert frozen["tab_id"] == 7
        assert frozen["content_revision"] == "sha-x"

    def test_desktop_identity_has_no_browser_keys(self, tmp_path):
        pkg = freeze_scene_package("任务", probe_payload([material()]),
                                   package_dir=tmp_path)
        assert set(pkg["scene"]["identity"].keys()) == {
            "pid", "process", "exe", "title", "class", "captured_at"}

    def test_obtained_flag(self, tmp_path):
        pkg = freeze_scene_package("任务", probe_payload([
            material(),  # 默认 obtained=True（文本已取得）
            material("link_clue", "附件链接文本", "browser:anchor", ) ,
        ]), package_dir=tmp_path)
        obtained = {m["kind"]: m["obtained"] for m in pkg["materials"]}
        assert obtained["selection"] is True
        assert obtained["link_clue"] is True  # 有文本即"内容取得"；纯链接材料由票11启用 False
