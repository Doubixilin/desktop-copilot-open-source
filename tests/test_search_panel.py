"""搜索面板状态机的纯逻辑测试（ticket 01）。

窗口层不做单测（视觉行为，走截图验收）；这里只锁定
待机态 ⇄ 活动态的转移规则与查询词清空语义。
"""

import unittest

import listary_engine as engine
import search_panel as sp
from search_panel import PanelStateMachine


class TestPanelStateMachine(unittest.TestCase):
    def setUp(self):
        self.m = PanelStateMachine()

    def test_initial_state_is_idle(self):
        self.assertEqual(self.m.state, "IDLE")
        self.assertEqual(self.m.query, "")

    def test_click_activates(self):
        self.assertEqual(self.m.on_event("click"), "ACTIVE")
        self.assertEqual(self.m.state, "ACTIVE")

    def test_esc_deactivates_and_clears_query(self):
        self.m.on_event("click")
        self.m.set_query("abc")
        self.assertEqual(self.m.on_event("esc"), "IDLE")
        self.assertEqual(self.m.query, "")

    def test_blur_deactivates_and_clears_query(self):
        self.m.on_event("click")
        self.m.set_query("合同")
        self.assertEqual(self.m.on_event("blur"), "IDLE")
        self.assertEqual(self.m.query, "")

    def test_esc_in_idle_is_noop(self):
        self.assertEqual(self.m.on_event("esc"), "IDLE")

    def test_blur_in_idle_is_noop(self):
        self.assertEqual(self.m.on_event("blur"), "IDLE")

    def test_click_in_active_keeps_state_and_query(self):
        self.m.on_event("click")
        self.m.set_query("xyz")
        self.assertEqual(self.m.on_event("click"), "ACTIVE")
        self.assertEqual(self.m.query, "xyz")

    def test_activate_after_deactivate_starts_clean(self):
        self.m.on_event("click")
        self.m.set_query("old")
        self.m.on_event("esc")
        self.m.on_event("click")
        self.assertEqual(self.m.query, "")


def build_app():
    """共享测试桩：withdrawn Tk + 复刻 run() 的构件（不进 mainloop）。"""
    import tkinter as tk
    import tkinter.font as tkfont
    from search_panel import SearchPanelApp
    app = SearchPanelApp()
    app.root = tk.Tk()
    app.root.withdraw()
    app.font = tkfont.Font(family="Courier", size=-17)
    app.head = tk.Label(app.root)
    app.box = tk.Frame(app.root, height=30)
    app.box.pack_propagate(False)
    app.hint = tk.Label(app.box)
    app.hint.pack(fill="both", expand=True)   # 复刻 run()：待机态 hint 在栈内
    app.entry = tk.Entry(app.box)
    app.placeholder = tk.Label(app.box)
    app.sep = tk.Frame(app.root)
    app.results_host = tk.Frame(app.root)
    app._base_h = 64
    return app


class TestPanelRenderLogic(unittest.TestCase):
    """窗口渲染逻辑（withdrawn Tk，无屏幕依赖）：待机提示/活动占位/文本三态。"""

    def _stacked(self, w):
        # withdrawn 窗口上 ismapped 恒 0；用几何管理器归属断言堆叠关系
        return w.winfo_manager() != ""

    def _app(self):
        return build_app()

    def test_idle_shows_hint_only(self):
        app = self._app()
        app.root.update_idletasks()
        self.assertTrue(self._stacked(app.hint))
        self.assertFalse(self._stacked(app.entry))
        self.assertFalse(self._stacked(app.placeholder))
        app.root.destroy()

    def test_active_empty_shows_entry_and_placeholder(self):
        app = self._app()
        app._render_active()
        app.root.update_idletasks()
        self.assertFalse(self._stacked(app.hint))
        self.assertTrue(self._stacked(app.entry))
        self.assertTrue(self._stacked(app.placeholder))
        app.root.destroy()

    def test_active_with_text_hides_placeholder(self):
        app = self._app()
        app.machine.on_event("click")
        app.entry.insert(0, "ok")
        app._render_active()
        app.root.update_idletasks()
        self.assertTrue(self._stacked(app.entry))
        self.assertFalse(self._stacked(app.placeholder))
        app.root.destroy()

    def test_deactivate_restores_hint(self):
        app = self._app()
        app.machine.on_event("click")
        app.entry.insert(0, "ok")
        app._render_active()
        app._deactivate("esc")
        app.root.update_idletasks()
        self.assertEqual(app.machine.state, "IDLE")
        self.assertEqual(app.machine.query, "")
        self.assertTrue(self._stacked(app.hint))
        self.assertFalse(self._stacked(app.entry))
        self.assertFalse(self._stacked(app.placeholder))
        app.root.destroy()


class TestRenderResults(unittest.TestCase):
    """结果区渲染：行数、TOTAL 页脚、空结果与离线徽标。"""

    def _app(self):
        return build_app()

    def test_rows_and_footer(self):
        app = self._app()
        items = [engine.ResultItem(path=f"C:/dir/f{i}.txt", name=f"f{i}.txt", type="file")
                 for i in range(3)]
        app._render_results(engine.SearchResults(ok=True, total=9, items=items))
        app.root.update_idletasks()
        kids = app.results_host.winfo_children()
        self.assertEqual(len(kids), 4)  # 3 行 + TOTAL
        footer = kids[-1]
        self.assertIn("TOTAL 9", footer.cget("text"))

    def test_more_than_limit_capped(self):
        app = self._app()
        items = [engine.ResultItem(path=f"C:/f{i}.txt", name=f"f{i}.txt", type="file")
                 for i in range(20)]
        app._render_results(engine.SearchResults(ok=True, total=20, items=items))
        app.root.update_idletasks()
        self.assertEqual(len(app.results_host.winfo_children()), engine.DEFAULT_LIMIT + 1)

    def test_empty_results_message(self):
        app = self._app()
        app._render_results(engine.SearchResults(ok=True, total=0, items=[]))
        app.root.update_idletasks()
        self.assertIn("NO RESULTS", app.results_host.winfo_children()[0].cget("text"))

    def test_offline_badge_and_clear_on_deactivate(self):
        from search_panel import PanelStateMachine
        app = self._app()
        app.machine.on_event("click")
        app._render_offline()
        app.root.update_idletasks()
        self.assertIn("ENGINE OFFLINE", app.results_host.winfo_children()[0].cget("text"))
        app._deactivate("esc")
        app.root.update_idletasks()
        self.assertEqual(app.results_host.winfo_children(), [])
        app.root.destroy()


class TestSelectionRender(unittest.TestCase):
    """选中高亮：结果渲染后首项反白；↑↓ 移动后新旧行样式互换。"""

    def _render3(self):
        app = build_app()
        items = [engine.ResultItem(path="C:/d/f" + str(i) + ".txt",
                                   name="f" + str(i) + ".txt", type="file")
                 for i in range(3)]
        app._render_results(engine.SearchResults(ok=True, total=3, items=items))
        app.root.update_idletasks()
        return app

    def test_first_row_selected_after_render(self):
        app = self._render3()
        self.assertEqual(app._sel.index, 0)
        row0, name0, path0 = app._rows[0]
        self.assertEqual(row0.cget("bg"), sp.FG)
        self.assertEqual(name0.cget("fg"), sp.BG)
        row1, _, _ = app._rows[1]
        self.assertEqual(row1.cget("bg"), sp.BG)
        app.root.destroy()

    def test_move_selection_swaps_style(self):
        app = self._render3()
        app._move_selection(1)
        row0, _, _ = app._rows[0]
        row1, name1, _ = app._rows[1]
        self.assertEqual(app._sel.index, 1)
        self.assertEqual(row0.cget("bg"), "#000000")
        self.assertEqual(row1.cget("bg"), sp.FG)
        self.assertEqual(name1.cget("fg"), sp.BG)
        app.root.destroy()

    def test_clear_results_resets_selection(self):
        app = self._render3()
        app._move_selection(2)
        app._clear_results()
        self.assertEqual(app._sel.index, -1)
        self.assertEqual(app._rows, [])
        app.root.destroy()


class TestRowClickBinding(unittest.TestCase):
    """单击结果行应触发 _act（等同 Enter）：索引→路径传递与 reveal 语义。"""

    def test_row_click_calls_act_open(self):
        app = build_app()
        items = [engine.ResultItem(path="C:/d/a.txt", name="a.txt", type="file"),
                 engine.ResultItem(path="C:/d/b.txt", name="b.txt", type="file")]
        app._render_results(engine.SearchResults(ok=True, total=2, items=items))
        app.root.update_idletasks()
        calls = []
        app._act = lambda path, reveal=False: calls.append((path, reveal))
        app._on_row_click(1)
        self.assertEqual(calls, [("C:/d/b.txt", False)])
        app._on_row_click(0)
        self.assertEqual(calls[-1], ("C:/d/a.txt", False))
        app._on_row_click(9)   # 越界点击：安全忽略
        self.assertEqual(len(calls), 2)
        app.root.destroy()


if __name__ == "__main__":
    unittest.main()
