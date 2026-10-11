"""Listary 引擎纯逻辑测试（ticket 02）。

覆盖离线复现不了或不好复现的分支：防抖窗口、限流退避、空结果、
offset 翻页请求构造、连接失败与 SEARCH_UNAVAILABLE 的离线映射、
路径显示的父子拆分与截断。HTTP 层本身由真机取证覆盖。
"""

import unittest

import listary_engine as eng


class TestDebouncer(unittest.TestCase):
    def setUp(self):
        self.d = eng.Debouncer(window_s=0.2)

    def test_rapid_changes_coalesce_to_final(self):
        self.d.feed("r", 0.00)
        self.d.feed("re", 0.05)
        self.d.feed("rea", 0.10)
        self.assertIsNone(self.d.due(0.15))  # 还在窗口内
        self.assertIsNone(self.d.due(0.29))  # 距最后一次改动不足 0.2s
        self.assertEqual(self.d.due(0.30), "rea")
        self.assertIsNone(self.d.due(0.50))  # 只发一次

    def test_newer_query_wins(self):
        self.d.feed("readme", 0.0)
        self.d.feed("readme.md", 0.1)
        self.assertEqual(self.d.due(0.31), "readme.md")

    def test_same_query_not_resent(self):
        self.d.feed("abc", 0.0)
        self.assertEqual(self.d.due(0.2), "abc")
        self.d.feed("abc", 1.0)
        self.assertIsNone(self.d.due(1.2))

    def test_empty_query_never_fires(self):
        self.d.feed("", 0.0)
        self.assertIsNone(self.d.due(0.5))

    def test_cancel_drops_pending(self):
        self.d.feed("abc", 0.0)
        self.d.cancel()
        self.assertIsNone(self.d.due(1.0))

    def test_reset_allows_resend(self):
        self.d.feed("abc", 0.0)
        self.d.due(0.2)
        self.d.reset()
        self.d.feed("abc", 1.0)
        self.assertEqual(self.d.due(1.2), "abc")


class TestBuildRequest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(eng.build_request("word"),
                         {"query": "word", "limit": eng.DEFAULT_LIMIT, "offset": 0})

    def test_offset_passthrough(self):
        req = eng.build_request("word", limit=20, offset=40)
        self.assertEqual(req, {"query": "word", "limit": 20, "offset": 40})


class TestParseResponse(unittest.TestCase):
    def _payload(self, results, total):
        return {"ok": True, "data": {"query": "q", "total": total, "offset": 0,
                                     "limit": 8, "count": len(results),
                                     "results": results}}

    def test_parses_items_and_total(self):
        p = self._payload([{"path": r"D:\Work\invoice.pdf", "name": "invoice.pdf",
                            "type": "file", "size_bytes": 1, "modified_at": "x",
                            "score": 9}], 42)
        r = eng.parse_response(p)
        self.assertTrue(r.ok)
        self.assertEqual(r.total, 42)
        self.assertEqual(len(r.items), 1)
        self.assertEqual(r.items[0].name, "invoice.pdf")
        self.assertEqual(r.items[0].path, r"D:\Work\invoice.pdf")
        self.assertEqual(r.items[0].type, "file")

    def test_empty_results(self):
        r = eng.parse_response(self._payload([], 0))
        self.assertTrue(r.ok)
        self.assertEqual(r.total, 0)
        self.assertEqual(r.items, [])

    def test_missing_data_is_zero_not_crash(self):
        r = eng.parse_response({"ok": True, "data": None})
        self.assertTrue(r.ok)
        self.assertEqual(r.total, 0)
        self.assertEqual(r.items, [])

    def test_more_than_limit_items_kept_as_is(self):
        p = self._payload([{"path": f"C:\\f{i}.txt", "name": f"f{i}.txt",
                            "type": "file", "size_bytes": 0, "modified_at": "",
                            "score": 0} for i in range(12)], 12)
        self.assertEqual(len(eng.parse_response(p).items), 12)


class TestClassifyFailure(unittest.TestCase):
    def test_connection_error_is_offline(self):
        self.assertEqual(eng.classify_failure(OSError("refused")), "offline")

    def test_search_unavailable_is_offline(self):
        self.assertEqual(eng.classify_failure({"ok": False, "error": "SEARCH_UNAVAILABLE"}),
                         "offline")

    def test_rate_limited(self):
        self.assertEqual(eng.classify_failure({"ok": False, "error": "TOO_MANY_REQUESTS"}),
                         "rate_limited")

    def test_other_payload_error(self):
        self.assertEqual(eng.classify_failure({"ok": False, "error": "INTERNAL_ERROR"}),
                         "error")

    def test_ok_payload_is_not_failure(self):
        self.assertIsNone(eng.classify_failure({"ok": True, "data": {}}))


class TestBackoff(unittest.TestCase):
    def test_doubling_capped(self):
        self.assertEqual([eng.backoff_delay(n) for n in range(1, 8)],
                         [0.5, 1.0, 2.0, 4.0, 5.0, 5.0, 5.0])


class TestDisplay(unittest.TestCase):
    def test_splits_parent_and_name(self):
        name, parent = eng.display_parts(r"D:\Work\docs\合同 v2.docx")
        self.assertEqual(name, "合同 v2.docx")
        self.assertEqual(parent, r"D:\Work\docs")

    def test_drive_root_parent(self):
        name, parent = eng.display_parts(r"C:\a.txt")
        self.assertEqual(name, "a.txt")
        self.assertEqual(parent, "C:\\")

    def test_elide_left(self):
        self.assertEqual(eng.elide_left(r"D:\a\b\c\file.txt", 9), "…file.txt")
        self.assertEqual(eng.elide_left("short", 10), "short")


if __name__ == "__main__":
    unittest.main()


class TestReviewFollowups(unittest.TestCase):
    """评审回改的补充覆盖：elide_right、非 OSError 异常归类、feed_due。"""

    def test_elide_right(self):
        self.assertEqual(eng.elide_right("超长文件名document.docx", 9), "超长文件名doc…")
        self.assertEqual(eng.elide_right("short", 10), "short")

    def test_value_error_is_error_not_offline(self):
        # 引擎在线但吐非 JSON：不是离线，不能冒充 ENGINE OFFLINE
        self.assertEqual(eng.classify_failure(ValueError("bad json")), "error")

    def test_feed_due_bypasses_window(self):
        d = eng.Debouncer(window_s=0.2)
        d.feed("abc", 100.0)
        self.assertEqual(d.due(100.05), None)   # 窗口内
        d.feed_due("abc")
        self.assertEqual(d.due(100.06), "abc")  # 立即到期

    def test_feed_due_allows_resent(self):
        d = eng.Debouncer(window_s=0.2)
        d.feed_due("abc")
        self.assertEqual(d.due(0.0), "abc")
        d.feed_due("abc")
        self.assertEqual(d.due(1.0), "abc")     # 已发过，重复 feed_due 仍去重
        d.feed_due("")
        self.assertIsNone(d.due(2.0))


class TestDecideAction(unittest.TestCase):
    """键 → 动作映射（ticket 03 纯逻辑）。"""

    def test_arrows(self):
        self.assertEqual(eng.decide_action("up"), "prev")
        self.assertEqual(eng.decide_action("down"), "next")

    def test_return_variants(self):
        self.assertEqual(eng.decide_action("return", ctrl=False), "open")
        self.assertEqual(eng.decide_action("return", ctrl=True), "reveal")

    def test_other_keys_are_none(self):
        self.assertIsNone(eng.decide_action("a"))
        self.assertIsNone(eng.decide_action("escape"))
        self.assertIsNone(eng.decide_action(""))


class TestSelectionModel(unittest.TestCase):
    def test_reset_to_first_on_new_results(self):
        s = eng.SelectionModel()
        s.set_count(5)
        s.move(3)
        s.set_count(4)          # 结果刷新：重置回首项
        self.assertEqual(s.index, 0)

    def test_empty_results_no_selection(self):
        s = eng.SelectionModel()
        s.set_count(0)
        self.assertEqual(s.index, -1)
        self.assertEqual(s.move(1), -1)

    def test_clamp_at_both_ends(self):
        s = eng.SelectionModel()
        s.set_count(3)
        self.assertEqual(s.move(-1), 0)   # 首项向上：停在首项（不环绕）
        self.assertEqual(s.move(1), 1)
        self.assertEqual(s.move(1), 2)
        self.assertEqual(s.move(1), 2)    # 尾项向下：停在尾项

    def test_negative_one_when_uninitialized(self):
        s = eng.SelectionModel()
        self.assertEqual(s.index, -1)
