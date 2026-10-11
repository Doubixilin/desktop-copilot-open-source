# -*- coding: utf-8 -*-
"""scene_probe：场景匹配 / 标题解析 / 材料预算 / 伪源探测全链路。

不碰真实 UIA（UiaSource 由伪源替换；弹窗采集 monkeypatch）。
真机探测由冒烟与人工验收覆盖（.scratch/scene-m2/）。
"""
import json
import subprocess
import sys
import time
from unittest import mock

sys.path.insert(0, "native_hud")
import scene_probe as sp  # noqa: E402


class TestMatchScenario:
    def test_table(self):
        assert sp.match_scenario("Code.exe", "x — Visual Studio Code", "Chrome_WidgetWin_1") == "ide"
        assert sp.match_scenario("pycharm64.exe", "t", "SunAwtFrame") == "ide"
        assert sp.match_scenario("explorer.exe", "t", "#32770") == "dialog"
        assert sp.match_scenario("WindowsTerminal.exe", "cmd", "CASCADIA_HOSTING_WINDOW_CLASS") == "terminal"
        assert sp.match_scenario("cmd.exe", "cmd", "ConsoleWindowClass") == "terminal"
        assert sp.match_scenario("WPS.exe", "doc - WPS", "Qt5156QWindowIcon") == "generic"
        assert sp.match_scenario("", "", "") == "generic"

    def test_dialog_priority_over_process(self):
        # #32770 优先：即便进程名像 IDE，标准对话框按弹窗处理
        assert sp.match_scenario("notepad.exe", "记事本", "#32770") == "dialog"


class TestParseFileFromTitle:
    def test_conventions(self):
        assert sp.parse_file_from_title("server.py — desktop-deck — Visual Studio Code") == "server.py"
        assert sp.parse_file_from_title("main.py - desktop-copilot - PyCharm") == "main.py"
        assert sp.parse_file_from_title("*方案评审.docx - WPS 文字") == "方案评审.docx"
        assert sp.parse_file_from_title("D:\\work\\合同 v2.docx — Word") == "D:\\work\\合同 v2.docx"
        assert sp.parse_file_from_title("index.test.ts — repo — Visual Studio Code") == "index.test.ts"

    def test_rejections(self):
        assert sp.parse_file_from_title("Visual Studio Code") is None       # 无扩展名
        assert sp.parse_file_from_title("设置 - Windows") is None           # 首段无扩展名
        assert sp.parse_file_from_title("") is None
        assert sp.parse_file_from_title("关于本机 — Mac") is None


class TestMakeMaterial:
    def test_preview_clips_and_counts(self):
        m = sp.make_material("selection", "uia:text", "  x" * 100)
        assert m["kind"] == "selection" and m["source"] == "uia:text"
        assert m["chars"] == 298  # "  x"×100 去首尾空白后 3*100-2
        assert len(m["preview"]) <= sp.PREVIEW_CHARS
        assert m["preview"].endswith("…")

    def test_blank_becomes_zero_chars(self):
        m = sp.make_material("file", "title", "   \n  ")
        assert m["chars"] == 0 and m["preview"] == ""


class FakeSource:
    def __init__(self, selection=None, doc_tail=None, raise_on=None):
        self._sel = selection
        self._tail = doc_tail
        self._raise = raise_on or set()

    def selection(self, hwnd, limit=None):
        if "sel" in self._raise:
            raise RuntimeError("com boom")
        return self._sel

    def document_tail(self, hwnd, limit=None):
        if "doc" in self._raise:
            raise RuntimeError("com boom")
        return self._tail


def ident(process="Code.exe", title="server.py — VS Code", cls="Chrome_WidgetWin_1"):
    return {"pid": 1, "process": process, "exe": r"C:\x\Code.exe", "title": title, "class": cls, "ts": 1.0}


class TestProbeWindow:
    def test_ide_full(self):
        src = FakeSource(selection="print('hi')\n")
        out = sp.probe_window(99, ident(), src)
        assert out["scenario"] == "ide" and out["coverage"] == "full"
        kinds = [m["kind"] for m in out["materials"]]
        assert kinds == ["file", "selection"]
        assert out["materials"][1]["preview"] == "print('hi')"

    def test_ide_uia_raises_note(self):
        src = FakeSource(raise_on={"sel"})
        out = sp.probe_window(99, ident(), src)
        assert "选区不可得" in out["note"]
        assert [m["kind"] for m in out["materials"]] == ["file"]

    def test_ide_no_uia_degrades(self):
        out = sp.probe_window(99, ident(), None)
        assert out["coverage"] == "partial"
        assert "comtypes" in out["note"]
        assert [m["kind"] for m in out["materials"]] == ["file"]

    def test_generic_identity_only(self):
        out = sp.probe_window(99, ident(process="wps.exe", title="文档 - WPS", cls="Qt"), FakeSource())
        assert out["scenario"] == "generic"
        assert out["materials"] == [] and out["coverage"] == "none"

    def test_dialog_win32(self):
        fake = {"statics": ["无法连接到服务器", "错误代码 0x8007"], "buttons": ["确定", "重试"], "coverage": "full"}
        with mock.patch.object(sp, "collect_dialog_texts", return_value=fake):
            out = sp.probe_window(7, ident(process="app.exe", cls="#32770"), None)
        assert out["scenario"] == "dialog" and out["coverage"] == "full"
        kinds = sorted(m["kind"] for m in out["materials"])
        assert kinds == ["dialog_buttons", "dialog_text"]

    def test_dialog_partial_dui(self):
        fake = {"statics": [], "buttons": ["关闭"], "coverage": "partial"}
        with mock.patch.object(sp, "collect_dialog_texts", return_value=fake):
            out = sp.probe_window(7, ident(cls="#32770"), None)
        assert out["coverage"] == "partial" and "DirectUI" in out["note"]

    def test_dialog_none(self):
        with mock.patch.object(sp, "collect_dialog_texts", return_value=None):
            out = sp.probe_window(7, ident(cls="#32770"), None)
        assert out["coverage"] == "partial" and out["materials"] == []

    def test_terminal_tail(self):
        src = FakeSource(doc_tail="FAILED tests/test_x.py::test_y - assert 1 == 2")
        out = sp.probe_window(
            5, ident(process="WindowsTerminal.exe", cls="CASCADIA_HOSTING_WINDOW_CLASS"), src)
        assert out["scenario"] == "terminal" and out["coverage"] == "full"
        assert out["materials"][0]["kind"] == "terminal_tail"
        assert "FAILED" in out["materials"][0]["preview"]

    def test_terminal_no_uia_or_blank(self):
        out = sp.probe_window(5, ident(process="wt.exe", cls="CASCADIA_HOSTING_WINDOW_CLASS"), None)
        assert out["coverage"] == "partial"
        # 空白尾文本视为未取得（契约内聚，不依赖采集源自净）
        src = FakeSource(doc_tail="   ")
        out = sp.probe_window(5, ident(process="wt.exe", cls="CASCADIA_HOSTING_WINDOW_CLASS"), src)
        assert out["coverage"] == "partial" and out["materials"] == []


class TestProbeRoundtrip:
    """scene_adapter op=probe 端到端（真子进程；generic 场景不依赖 UIA）。"""

    def _start(self):
        return subprocess.Popen(
            [sys.executable, "-B", "-u", "native_hud/scene_adapter.py"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", cwd=".",
        )

    def _send(self, proc, obj):
        proc.stdin.write(json.dumps(obj) + chr(10))
        proc.stdin.flush()

    def _wait_id(self, proc, rid, deadline_s=15.0):
        deadline = time.time() + deadline_s
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                return None
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == rid:
                return msg
        return None

    def _stop(self, proc):
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def test_probe_sequential(self):
        proc = self._start()
        try:
            self._send(proc, {"id": 1, "op": "ping"})
            assert self._wait_id(proc, 1)["status"] == "ok"
            self._send(proc, {"id": 2, "op": "probe", "hwnd": 0})
            got2 = self._wait_id(proc, 2)
            assert got2["status"] == "ok" and got2["scenario"] == "generic"
            assert got2["materials"] == [] and got2["coverage"] == "none"
            assert got2["tier"] == "full"  # M6：应答回显档位
            self._send(proc, {"id": 3, "op": "probe", "hwnd": 1})  # 桌面窗口恒 generic
            got3 = self._wait_id(proc, 3)
            assert got3["status"] == "ok"
        finally:
            self._stop(proc)

    def test_probe_flood_no_dangling_ids(self):
        import threading as _th

        proc = self._start()
        answered = {}

        def collect():
            try:
                for line in proc.stdout:
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if "id" in msg:
                        answered[msg["id"]] = msg
            except OSError:
                pass

        reader = _th.Thread(target=collect, daemon=True)
        reader.start()
        try:
            self._send(proc, {"id": 10, "op": "probe", "hwnd": 0})
            self._send(proc, {"id": 11, "op": "probe", "hwnd": 1})
            self._send(proc, {"id": 12, "op": "probe", "hwnd": 0})
            deadline = time.time() + 15
            while len(answered) < 3 and time.time() < deadline:
                time.sleep(0.05)
            # 三个 id 都必须有应答；最新者 ok，被超越者 superseded（无悬挂 id）
            assert set(answered) == {10, 11, 12}, f"answered={sorted(answered)}"
            assert answered[12]["status"] == "ok"
            for rid in (10, 11):
                assert answered[rid].get("error") == "superseded"
        finally:
            self._stop(proc)

    def test_validate_probe(self):
        import scene_adapter as sa
        assert sa._validate(json.dumps({"id": 1, "op": "probe", "hwnd": 123})) == (
            {"id": 1, "op": "probe", "hwnd": 123, "tier": "full"}, None)
        for bad in ('{"id":1,"op":"probe","hwnd":"x"}', '{"id":1,"op":"probe","hwnd":-5}'):
            req, err = sa._validate(bad)
            assert req is None and err == "bad_hwnd"
        # 缺 hwnd 默认 0（探测无窗口 → generic），属合法请求
        assert sa._validate('{"id":1,"op":"probe"}') == (
            {"id": 1, "op": "probe", "hwnd": 0, "tier": "full"}, None)

    def test_validate_probe_tier(self):
        import scene_adapter as sa
        # M6 分档：fast 透传；非法 tier 回退 full（不拒绝请求）
        req, _ = sa._validate(json.dumps({"id": 1, "op": "probe", "hwnd": 1, "tier": "fast"}))
        assert req["tier"] == "fast"
        req, _ = sa._validate(json.dumps({"id": 1, "op": "probe", "hwnd": 1, "tier": "x"}))
        assert req["tier"] == "full"


class TestProbeTiers:
    """M6 感知分档：fast 跳过 UIA，full 保持旧行为。"""

    def test_ide_fast_skips_uia(self):
        src = FakeSource(selection="不应出现")
        out = sp.probe_window(99, ident(), src, tier="fast")
        assert [m["kind"] for m in out["materials"]] == ["file"]
        assert out["coverage"] == "partial" and "快档" in out["note"]

    def test_terminal_fast_identity_only(self):
        src = FakeSource(doc_tail="不应出现")
        out = sp.probe_window(
            5, ident(process="wt.exe", cls="CASCADIA_HOSTING_WINDOW_CLASS"), src, tier="fast")
        assert out["materials"] == [] and out["coverage"] == "none" and "快档" in out["note"]

    def test_dialog_fast_still_collects_with_boxes(self):
        fake = {"statics": ["出错"], "buttons": ["确定"], "coverage": "full",
                "items": [{"text": "出错", "role": "static", "box": [10, 10, 100, 20]},
                          {"text": "确定", "role": "button", "box": [10, 40, 60, 24]}]}
        with mock.patch.object(sp, "collect_dialog_texts", return_value=fake):
            out = sp.probe_window(7, ident(cls="#32770"), None, tier="fast")
        assert out["coverage"] == "full"  # 快档即满：宿主不再升级 full（省 UIA）
        m = {x["kind"]: x for x in out["materials"]}
        assert m["dialog_text"]["lines"][0]["box"] == [10, 10, 100, 20]
        assert m["dialog_buttons"]["lines"][0]["text"] == "确定"

    def test_generic_ignores_tier(self):
        out = sp.probe_window(99, ident(process="wps.exe", title="t", cls="Qt"),
                              FakeSource(), tier="fast")
        assert out["coverage"] == "none" and out["materials"] == []
