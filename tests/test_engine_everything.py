"""Everything 引擎适配接缝测试：payload 归一化 + 引擎分发，全部离线纯逻辑。"""
import json
import os
import unittest

import listary_engine as eng


class NormalizeTest(unittest.TestCase):
    def test_everything_payload_to_ok_model(self):
        payload = {"totalResults": 2, "results": [
            {"type": "file", "name": "a.md", "path": "D:\\docs", "size": "123"},
            {"type": "folder", "name": "sub", "path": "D:\\docs"},
        ]}
        ok = eng.normalize_everything(payload)
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["data"]["total"], 2)
        a, sub = ok["data"]["results"]
        self.assertEqual(a["path"], "D:\\docs\\a.md")
        self.assertEqual(a["size_bytes"], 123)
        self.assertEqual(sub["type"], "folder")

    def test_parse_response_accepts_normalized(self):
        ok = eng.normalize_everything({"totalResults": 1, "results": [
            {"type": "file", "name": "x.txt", "path": "C:\\t", "size": "5"}]})
        model = eng.parse_response(ok)
        self.assertTrue(model.ok)
        self.assertEqual(model.total, 1)
        self.assertEqual(model.items[0].path, "C:\\t\\x.txt")

    def test_bad_rows_skipped(self):
        ok = eng.normalize_everything({"totalResults": 2, "results": ["junk", {"name": "n"}]})
        self.assertEqual(len(ok["data"]["results"]), 1)

    def test_non_dict_raises(self):
        with self.assertRaises(ValueError):
            eng.normalize_everything([1, 2])


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self._orig = eng._engine_choice

    def tearDown(self):
        eng._engine_choice = self._orig

    def test_env_everything_dispatches(self):
        os.environ["QD_SEARCH_ENGINE"] = "everything"
        try:
            called = {}

            def fake(q, limit=8, offset=0):
                called["q"] = q
                return {"ok": True, "data": {"total": 0, "results": []}}

            eng.everything_http_search = fake
            self.assertEqual(eng.http_search("x")["ok"], True)
            self.assertEqual(called["q"], "x")
        finally:
            del os.environ["QD_SEARCH_ENGINE"]

    def test_env_listary_dispatches(self):
        os.environ["QD_SEARCH_ENGINE"] = "listary"
        eng._engine_choice = None
        try:
            orig = eng.listary_http_search
            eng.listary_http_search = lambda q, limit=8, offset=0: {"ok": True}
            self.assertEqual(eng.http_search("x")["ok"], True)
        finally:
            eng.listary_http_search = orig
            del os.environ["QD_SEARCH_ENGINE"]

    def test_auto_detect_prefers_reachable_everything(self):
        eng._engine_choice = None
        eng._everything_reachable = lambda: True
        try:
            self.assertEqual(eng._choose_engine(), "everything")
        finally:
            eng._engine_choice = None

    def test_auto_detect_falls_back_to_listary(self):
        eng._engine_choice = None
        eng._everything_reachable = lambda: False
        try:
            self.assertEqual(eng._choose_engine(), "listary")
        finally:
            eng._engine_choice = None

    def test_invalid_env_falls_back_to_auto_detect(self):
        os.environ["QD_SEARCH_ENGINE"] = "bogus"
        eng._engine_choice = None
        eng._env_warned = True   # 告警路径只压输出，行为仍走自动探测
        orig_reachable = eng._everything_reachable
        eng._everything_reachable = lambda: False
        try:
            self.assertEqual(eng._choose_engine(), "listary")
        finally:
            eng._everything_reachable = orig_reachable
            eng._engine_choice = None
            eng._env_warned = False
            del os.environ["QD_SEARCH_ENGINE"]

    def _raise_oserror(self, q, limit=8, offset=0):
        raise OSError("engine down")

    def test_consecutive_offline_failures_trigger_reprobe(self):
        eng._engine_choice = "everything"
        eng._engine_failures = 0
        orig = eng.everything_http_search
        eng.everything_http_search = self._raise_oserror
        try:
            for _ in range(eng._REPROBE_AFTER):
                with self.assertRaises(OSError):
                    eng.http_search("x")
            # 缓存已清：下次调用重新探测（引擎后启动能跟上）
            self.assertIsNone(eng._engine_choice)
        finally:
            eng.everything_http_search = orig
            eng._engine_failures = 0

    def test_env_pinned_engine_never_reprobes(self):
        os.environ["QD_SEARCH_ENGINE"] = "everything"
        eng._engine_failures = 0
        orig = eng.everything_http_search
        eng.everything_http_search = self._raise_oserror
        try:
            for _ in range(eng._REPROBE_AFTER):
                with self.assertRaises(OSError):
                    eng.http_search("x")
            self.assertEqual(eng._engine_failures, eng._REPROBE_AFTER)
        finally:
            eng.everything_http_search = orig
            eng._engine_failures = 0
            del os.environ["QD_SEARCH_ENGINE"]


if __name__ == "__main__":
    unittest.main()
