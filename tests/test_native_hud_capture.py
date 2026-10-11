# -*- coding: utf-8 -*-
"""scene_capture：DPI/截窗/OCR 单测（真图像走真机，OCR 引擎可缺席）+ op=capture 往返。"""
import ctypes
import json
import subprocess
import sys
import threading
import time
from unittest import mock

import pytest

sys.path.insert(0, "native_hud")
import scene_capture as sc  # noqa: E402
import scene_probe as sp  # noqa: E402


class TestDpi:
    def test_ensure_dpi_aware_idempotent(self):
        assert isinstance(sc.ensure_dpi_aware(), bool)
        assert sc.ensure_dpi_aware() == sc.ensure_dpi_aware()  # 幂等


def make_tiny_window(w=240, h=120):
    """在独立线程创建并泵消息的小窗口。

    跨进程 PrintWindow 会 SendMessage 到窗口所属线程——无消息泵的窗口
    会把对方永久阻塞（实测教训）；必须泵。
    """
    made: dict = {}

    def run():
        u = ctypes.windll.user32
        u.CreateWindowExW.restype = ctypes.c_void_p
        hwnd = u.CreateWindowExW(
            0, "STATIC", "capture-probe", 0x90000000,  # WS_POPUP|WS_VISIBLE
            0, 0, w, h, 0, 0, 0, 0)
        made["hwnd"] = hwnd or 0
        if not hwnd:
            return
        msg = ctypes.wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))

    threading.Thread(target=run, daemon=True).start()
    for _ in range(100):
        if "hwnd" in made:
            return made["hwnd"]
        time.sleep(0.02)
    return 0


def close_tiny_window(hwnd):
    if hwnd:
        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE


class TestCapture:
    def test_capture_small_window_shape(self):
        """真机：截自建小窗口，返回 (w,h,bytes,note) 或 None（不炸即过）。"""
        hwnd = make_tiny_window()
        if not hwnd:
            pytest.skip("本机无法创建窗口")
        try:
            got = sc.capture_window_bytes(hwnd)
        finally:
            close_tiny_window(hwnd)
        if got is None:
            pytest.skip("本机截窗不可用（远程会话/无桌面）")
        w, h, bgra, note = got
        assert w > 0 and h > 0
        assert len(bgra) >= w * h * 4 - 8  # GetDIBits 行对齐容差


class TestOcr:
    def test_ocr_missing_engine_note(self, monkeypatch):
        # 引擎标记为不可用 → 人话原因
        with sc._ocr_lock:
            saved = dict(sc._ocr_state)
            sc._ocr_state["engine"] = None
            sc._ocr_state["reason"] = "OCR 不可用（ImportError）"
        try:
            text, note = sc.ocr_bytes(4, 4, b"\x00" * 64)
            assert text is None and "OCR 不可用" in note
        finally:
            with sc._ocr_lock:
                sc._ocr_state.clear()
                sc._ocr_state.update(saved)

    def test_ocr_real_image_offline(self):
        """真机：rapidocr 在场时对合成图像跑一次（文字识别结果不设硬断言，结构即过）。"""
        pytest.importorskip("rapidocr_onnxruntime")
        sc._ocr_state  # noqa: B018
        with sc._ocr_lock:
            sc._ocr_state["engine"] = ...  # 重置，强制真初始化
            sc._ocr_state["reason"] = ""
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (320, 80), "white")
        draw = ImageDraw.Draw(img)
        draw.text((16, 24), "AGENT DECK 12345", fill="black")
        raw = img.convert("RGBA").tobytes()
        text, note = sc.ocr_bytes(320, 80, raw)
        # 结构断言：要么拿到文本，要么拿到人话原因；两者都算链路健康
        assert (text is None and note) or (text is not None)

    def test_capture_material_shape(self):
        ocr_lines_value = [{"text": "识别文本", "box": [0, 0, 10, 10],
                            "score": 0.9, "row": 0}]
        with mock.patch.object(sc, "capture_window_bytes") as cap, \
             mock.patch.object(sc, "ocr_lines") as ocr:
            cap.return_value = (100, 50, b"x" * 20000, None)
            ocr.return_value = (ocr_lines_value, None)
            m = sc.capture_material_inline(42)
        assert m["kind"] == "screen_text" and m["source"] == "ocr"
        assert m["text"] == "识别文本" and m["chars"] == 4
        assert m["lines"][0]["box"] == [0, 0, 10, 10]  # M6：行级结构随材料出境
        # caveat 注入
        with mock.patch.object(sc, "capture_window_bytes") as cap, \
             mock.patch.object(sc, "ocr_lines") as ocr:
            cap.return_value = (100, 50, b"x" * 20000, "PrintWindow 空图，改用屏幕区域截图（可能含遮挡）")
            ocr.return_value = ([{"text": "文本", "box": [0, 0, 5, 5], "score": 0.9, "row": 0}], None)
            m = sc.capture_material_inline(42)
        assert "遮挡" in m["caveat"]

    def test_capture_material_worker_path_real(self):
        """worker 子进程全链（截窗→OCR→材料 JSON）：可见小窗应出真材料。"""
        tiny = make_tiny_window()
        if not tiny:
            pytest.skip("本机无法创建窗口")
        try:
            m = sc.capture_material(tiny, timeout=30)
        finally:
            close_tiny_window(tiny)
        assert m is not None, "可见带文本小窗的 L2 兜底应产出材料"
        assert m["kind"] == "screen_text" and m["source"] == "ocr"
        assert isinstance(m["text"], str) and m["text"].strip()

    def test_capture_material_none_paths(self):
        with mock.patch.object(sc, "capture_window_bytes", return_value=None):
            assert sc.capture_material_inline(1) is None
        with mock.patch.object(sc, "capture_window_bytes", return_value=(1, 1, b"", None)), \
             mock.patch.object(sc, "ocr_lines", return_value=(None, "OCR 未识别到文字")):
            assert sc.capture_material_inline(1) is None


class TestOrderLines:
    """M6：OCR 行阅读顺序（行聚类 + 行内按 x）。"""

    def test_rows_and_columns(self):
        result = [
            ([[0, 40], [100, 40], [100, 60], [0, 60]], "左下", 0.9),
            ([[300, 0], [400, 0], [400, 20], [300, 20]], "右上", 0.9),
            ([[0, 0], [100, 0], [100, 20], [0, 20]], "左上", 0.9),
        ]
        lines = sc.order_lines(result)
        assert [l["text"] for l in lines] == ["左上", "右上", "左下"]
        assert lines[0]["row"] == lines[1]["row"] == 0 and lines[2]["row"] == 1
        assert sc.lines_to_text(lines) == "左上  右上\n左下"
        assert lines[0]["box"] == [0.0, 0.0, 100.0, 20.0]

    def test_skips_blank_and_bad(self):
        assert sc.order_lines(None) == []
        assert sc.order_lines([([[0, 0], [1, 0], [1, 1], [0, 1]], "  ", 0.5)]) == []
        assert sc.order_lines([("bad",)]) == []
        assert sc.lines_to_text([]) == ""


class TestCaptureRoundtrip:
    def _start(self):
        return subprocess.Popen(
            [sys.executable, "-B", "-u", "native_hud/scene_adapter.py"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", cwd=".",
        )

    def _collect(self, proc, want_ids, deadline_s=30.0):
        """后台收行（阻塞 readline 不吞截止时间），收齐 want_ids 或超时返回。"""
        import queue as _q

        out: dict = {}
        q: "queue.Queue[object]" = _q.Queue()

        def reader():
            try:
                for line in proc.stdout:
                    q.put(line)
            except OSError:
                pass
            q.put(None)

        threading.Thread(target=reader, daemon=True).start()
        deadline = time.time() + deadline_s
        while time.time() < deadline and not set(out) >= set(want_ids):
            try:
                item = q.get(timeout=max(0.05, deadline - time.time()))
            except _q.Empty:
                break
            if item is None:
                break
            try:
                msg = json.loads(item)
            except ValueError:
                continue
            if msg.get("id") in want_ids:
                out[msg["id"]] = msg
        return out

    def test_capture_over_stdio(self):
        """真子进程：op=capture 对泵消息小窗应答 ok（material 可空但结构合法）。"""
        proc = self._start()
        tiny = make_tiny_window()
        if not tiny:
            pytest.skip("本机无法创建窗口")
        try:
            proc.stdin.write(json.dumps({"id": 1, "op": "capture", "hwnd": tiny}) + chr(10))
            proc.stdin.flush()
            got = self._collect(proc, {1}, deadline_s=35)
            assert 1 in got, f"id=1 无应答（悬挂）——已收: {sorted(got)}"
            assert got[1]["status"] == "ok", got[1]
            # 协议断言：必应答不悬挂；性能由 test_capture_material_worker_path_real 把关
            m = got[1].get("material")
            if m is not None:
                assert m["kind"] == "screen_text" and m["source"] == "ocr"
                assert isinstance(m["text"], str) and m["text"]
        finally:
            close_tiny_window(tiny)
            try:
                proc.stdin.close()
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()

    def test_validate_capture(self):
        import scene_adapter as sa

        ok = sa._validate(json.dumps({"id": 1, "op": "capture", "hwnd": 5}))
        assert ok == ({"id": 1, "op": "capture", "hwnd": 5, "materials": []}, None)
        req, err = sa._validate('{"id":1,"op":"capture","hwnd":0}')
        assert req is None and err == "bad_hwnd"

    def test_validate_capture_materials_sanitized(self):
        import scene_adapter as sa

        # M6 融合：materials 消毒——只留 kind/text/合法 lines，非法不拒绝请求
        req, err = sa._validate(json.dumps({
            "id": 1, "op": "capture", "hwnd": 5,
            "materials": [
                {"kind": "dialog_text", "text": "出错",
                 "lines": [{"text": "出错", "box": [1, 2, 3, 4]},
                           {"text": "坏框", "box": "bad"},
                           {"text": "缺框"}]},
                {"kind": "selection", "text": 123},  # 非字符串 text → 空
                "junk",
            ]}))
        assert err is None
        mats = req["materials"]
        assert len(mats) == 2
        assert mats[0]["lines"] == [{"text": "出错", "box": [1.0, 2.0, 3.0, 4.0]}]
        assert mats[1] == {"kind": "selection", "text": ""}
        # 非列表 materials 视为无
        req, _ = sa._validate('{"id":1,"op":"capture","hwnd":5,"materials":"x"}')
        assert req["materials"] == []
