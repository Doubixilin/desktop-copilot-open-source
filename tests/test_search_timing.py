"""搜索异步时序回归（审计 2026-09-26 B03/B04/B05，票 audit-remediation 01）。

产品化 docs/reviews/evidence/2026-09-26-search-interaction-probe.py 的 8 项检查：
隐藏 Tk 控件 + 真实 StringVar 文本变化回调 + 人工排队的虚构响应；外部打开
动作替换为记录函数，无网络请求、无真实文件打开。原取证脚本与 JSON 保留
不动，本文件是它的常驻等价覆盖。
"""

import unittest
from types import SimpleNamespace

import listary_engine as engine
from tests.test_search_panel import build_app


def wired_app():
    """build_app + 绑定 StringVar 追踪 + 激活：复刻生产文本变化链路。"""
    import tkinter as tk
    app = build_app()
    app.var = tk.StringVar(app.root)
    app.entry.configure(textvariable=app.var)
    app.var.trace_add("write", lambda *_: app._on_text_changed())
    app.machine.on_event("click")
    app._trace = lambda *_: None
    return app


def payload(name):
    return {"ok": True, "data": {"total": 1, "results": [
        {"path": "C:/synthetic/" + name, "name": name, "type": "file"}
    ]}}


def populate(app, name="a.txt"):
    app._render_results(engine.parse_response(payload(name)))


class TestSearchTiming(unittest.TestCase):
    """probe 八项检查的逐条产品化（期望与取证脚本一致）。"""

    def test_current_generation_response_renders(self):
        # 复核 F04 后的 fixture 修正：真实流程中响应必然属于某个已输入的查询，
        # 直塞"无查询的当前代际响应"不再成立（原始 probe 检查 1 的等价覆盖）
        app = wired_app()
        try:
            app.var.set("a")
            app._results_q.put((app._search_gen, "a", payload("a.txt"), None))
            app._drain_results()
            self.assertEqual([x.name for x in app._items], ["a.txt"])
        finally:
            app.root.destroy()

    def test_enter_and_ctrl_enter_route_without_opening_files(self):
        app = wired_app()
        try:
            populate(app)
            calls = []
            app._act = lambda path, reveal: calls.append([path, reveal])
            app._on_key("down", SimpleNamespace(state=0))
            app._on_key("up", SimpleNamespace(state=0))
            app._on_key("return", SimpleNamespace(state=0))
            app._on_key("return", SimpleNamespace(state=4))
            self.assertEqual(calls, [["C:/synthetic/a.txt", False],
                                     ["C:/synthetic/a.txt", True]])
        finally:
            app.root.destroy()

    def test_row_callback_stops_propagation(self):
        app = wired_app()
        try:
            populate(app)
            calls = []
            app._act = lambda path, reveal: calls.append([path, reveal])
            result = app._row_click_handler(0)(None)
            self.assertEqual(result, "break")
            self.assertEqual(calls, [["C:/synthetic/a.txt", False]])
        finally:
            app.root.destroy()

    def test_escape_clears_input_query_results_and_selection(self):
        app = wired_app()
        try:
            app.var.set("a")
            populate(app)
            app._deactivate("esc")
            self.assertEqual(app.machine.state, "IDLE")
            self.assertEqual(app.machine.query, "")
            self.assertEqual(app.var.get(), "")
            self.assertEqual(len(app._items), 0)
            self.assertEqual(app._sel.index, -1)
        finally:
            app.root.destroy()

    def test_response_after_escape_is_ignored(self):
        app = wired_app()
        try:
            app.var.set("a")
            app._deactivate("esc")
            app._results_q.put((0, "a", payload("a.txt"), None))
            app._drain_results()
            self.assertEqual(len(app._items), 0)
        finally:
            app.root.destroy()

    def test_cleared_query_rejects_old_response(self):
        """B04：清空输入没有作废在途响应，旧结果回填空面板。"""
        app = wired_app()
        try:
            app.var.set("old")
            self.assertEqual(app._search_gen, 1)   # 每次变更即递增（F04）
            app.var.set("")                        # → 代际 2
            app._results_q.put((1, "old", payload("old.txt"), None))
            app._drain_results()
            self.assertEqual([x.name for x in app._items], [])
        finally:
            app.root.destroy()

    def test_nonempty_edit_rejects_prior_query_response(self):
        """F04：old→new 的非空修改在防抖窗口内同样作废在途响应。"""
        app = wired_app()
        try:
            app.var.set("old")
            stale_gen = app._search_gen
            app.var.set("new")   # 防抖未到期，尚未发出新请求
            app._results_q.put((stale_gen, "old", payload("old.txt"), None))
            app._drain_results()
            self.assertEqual([x.name for x in app._items], [])
        finally:
            app.root.destroy()

    def test_stale_results_are_not_executable_during_debounce(self):
        """F04：结果仍显示旧查询时，Enter/行点击不得打开旧文件。"""
        app = wired_app()
        try:
            app.var.set("old")
            app._results_q.put((app._search_gen, "old", payload("old.txt"), None))
            app._drain_results()
            self.assertEqual([x.name for x in app._items], ["old.txt"])
            app.var.set("new")   # 旧结果还挂在列表上，但已不属于当前查询
            calls = []
            app._act = lambda path, reveal: calls.append((path, reveal))
            app._on_key("return", SimpleNamespace(state=0))
            app._on_row_click(0)
            self.assertEqual(calls, [])
            # 新查询结果到达后，动作恢复可用
            app._results_q.put((app._search_gen, "new", payload("new.txt"), None))
            app._drain_results()
            app._on_key("return", SimpleNamespace(state=0))
            self.assertEqual(calls, [("C:/synthetic/new.txt", False)])
        finally:
            app.root.destroy()

    def test_out_of_order_new_then_old_keeps_latest(self):
        """B05：新代际先到、旧代际后到，不得把最新有效结果挤掉。"""
        app = wired_app()
        try:
            app.var.set("new")
            app._results_q.put((app._search_gen, "new", payload("new.txt"), None))
            app._results_q.put((app._search_gen - 1, "old", payload("old.txt"), None))
            app._drain_results()
            self.assertEqual([x.name for x in app._items], ["new.txt"])
        finally:
            app.root.destroy()

    def test_stale_batch_renders_nothing(self):
        """B05 边界：整批都过期时保持原状，不渲染任何旧代际。"""
        app = wired_app()
        try:
            app.var.set("abc")
            gen = app._search_gen
            app.var.set("abcd")   # 继续输入 → 旧代际全部过期
            app._results_q.put((gen, "abc", payload("a.txt"), None))
            app._results_q.put((gen - 1, "ab", payload("ab.txt"), None))
            app._drain_results()
            self.assertEqual([x.name for x in app._items], [])
        finally:
            app.root.destroy()


class TestDebouncerRetype(unittest.TestCase):
    """B03：清空后再重输同一个词，必须重新发出（防抖去重随清空重置）。"""

    def test_retyping_cleared_query_searches_again(self):
        d = engine.Debouncer()
        d.feed("abc", 0)
        self.assertEqual(d.due(1), "abc")
        d.feed("", 2)
        d.feed("abc", 3)
        self.assertEqual(d.due(4), "abc")

    def test_continuous_same_query_still_not_resent(self):
        d = engine.Debouncer()
        d.feed("abc", 0)
        self.assertEqual(d.due(1), "abc")
        d.feed("abc", 2)
        self.assertIsNone(d.due(3))


if __name__ == "__main__":
    unittest.main()
