# -*- coding: utf-8 -*-
"""scene_fusion：L1/L2 去重融合纯逻辑（M6，spec .scratch/scene-m6/）。"""
import sys

sys.path.insert(0, "native_hud")
import scene_fusion as sf  # noqa: E402


def ocr(text, box=None):
    return {"text": text, "box": box}


class TestNormAndOverlap:
    def test_norm_strips_ws_and_case(self):
        assert sf.norm_text(" A b\nC ") == "abc"
        assert sf.norm_text("  ") == ""

    def test_char_overlap(self):
        assert sf.char_overlap("abc", "xxabcxx") == 1.0
        assert sf.char_overlap("abc", "ab") == 2 / 3
        assert sf.char_overlap("", "x") == 0.0


class TestIou:
    def test_basic(self):
        assert sf.iou([0, 0, 10, 10], [0, 0, 10, 10]) == 1.0
        assert sf.iou([0, 0, 10, 10], [20, 20, 5, 5]) == 0.0
        # 交 50 / 并 150
        assert abs(sf.iou([0, 0, 10, 10], [5, 0, 10, 10]) - 50 / 150) < 1e-9

    def test_bad_boxes(self):
        assert sf.iou(None, [0, 0, 1, 1]) == 0.0
        assert sf.iou([0, 0, 0, 10], [0, 0, 10, 10]) == 0.0


class TestFuseLines:
    L1 = [{"kind": "dialog_text", "text": "无法连接到服务器\n错误代码 0x8007"}]

    def test_contained_dropped(self):
        kept, dropped = sf.fuse_lines(self.L1, [ocr("无法连接到服务器"), ocr("全新内容")])
        assert [l["text"] for l in kept] == ["全新内容"] and dropped == 1

    def test_overlap_dropped_with_noise(self):
        # OCR 噪声导致逐字包含失败，但字符集高重叠仍剔除
        kept, dropped = sf.fuse_lines(self.L1, [ocr("无法连椄到服务器")])
        assert kept == [] and dropped == 1

    def test_noise_short_lines_filtered(self):
        kept, dropped = sf.fuse_lines([], [ocr("·"), ocr("有效内容")])
        assert [l["text"] for l in kept] == ["有效内容"] and dropped == 0

    def test_iou_rule(self):
        l1 = [{"kind": "dialog_text", "text": "确定按钮",
               "lines": [{"text": "确定按钮", "box": [100, 100, 40, 20]}]}]
        # 框重叠 + 文本半像（OCR 噪声：扭/钮）→ IoU 规则剔除
        kept, dropped = sf.fuse_lines(l1, [ocr("确定按扭", [102, 101, 40, 20])])
        assert kept == [] and dropped == 1
        # 框不重叠且文本不重叠 → 保留
        kept2, dropped2 = sf.fuse_lines(l1, [ocr("其他内容", [300, 300, 60, 20])])
        assert len(kept2) == 1 and dropped2 == 0

    def test_input_order_preserved(self):
        kept, _ = sf.fuse_lines([], [ocr("第一行"), ocr("第二行"), ocr("第三行")])
        assert [l["text"] for l in kept] == ["第一行", "第二行", "第三行"]


class TestMergeMaterial:
    def test_all_dup_returns_none(self):
        l1 = [{"kind": "terminal_tail", "text": "FAILED tests/test_x.py"}]
        m = {"kind": "screen_text", "source": "ocr", "text": "FAILED tests/test_x.py",
             "lines": [ocr("FAILED tests/test_x.py")]}
        merged, note = sf.merge_material(l1, m)
        assert merged is None and "重复" in note

    def test_partial_keeps_new(self):
        l1 = [{"kind": "selection", "text": "print('hi')"}]
        m = {"kind": "screen_text", "source": "ocr", "text": "print('hi')\nTraceback: boom",
             "lines": [ocr("print('hi')"), ocr("Traceback: boom")]}
        merged, note = sf.merge_material(l1, m)
        assert merged is not None and merged["kind"] == "screen_text"
        assert merged["text"] == "Traceback: boom"
        assert merged["lines"][0]["text"] == "Traceback: boom"
        assert "1" in note  # 剔除行数显性化

    def test_no_lines_fallback(self):
        l1 = [{"kind": "selection", "text": "整段文本"}]
        merged, note = sf.merge_material(l1, {"kind": "screen_text", "text": "整段文本"})
        assert merged is None and "重复" in note
        merged2, note2 = sf.merge_material(l1, {"kind": "screen_text", "text": "别的东西"})
        assert merged2 is not None and note2 == ""

    def test_none_material(self):
        assert sf.merge_material([], None) == (None, "")

    def test_caveat_carried(self):
        m = {"kind": "screen_text", "source": "ocr", "text": "新内容",
             "caveat": "遮挡风险", "lines": [ocr("新内容")]}
        merged, _ = sf.merge_material([], m)
        assert merged["caveat"] == "遮挡风险"
