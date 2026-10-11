"""04 票：面板副驾入口（视图切换/格式化/关闭语义）。

Tk 相关用 withdrawn 窗口测逻辑（同 test_search_panel 风格）；纯逻辑部分
（事件格式化、目录短名）无窗口依赖。中文 IME/失焦/Win+D 属人工验收项。
"""
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import search_panel as sp  # noqa: E402


class TestCopilotDisabledByDefault(unittest.TestCase):
    """无 copilot 配置时：面板构造不受影响、不显示副驾 tab、模式 auto。"""

    def setUp(self):
        # 隔离到空配置目录：本机可能存在真实启用配置（如人工验收会话写入
        # %LOCALAPPDATA% 的 copilot.json），不能作为"无配置"前提
        import os
        import tempfile

        from copilot import runtime_host

        self._tmp = tempfile.mkdtemp(prefix="qd-panel-noconfig-")
        self._env = os.environ.get("QD_COPILOT_DIR")
        os.environ["QD_COPILOT_DIR"] = self._tmp
        # get_controller 有进程内缓存，须一并复位，否则沿用真实配置结果
        self._state = dict(runtime_host._state)
        runtime_host._state.update(controller=None, error=None, started=False)

    def tearDown(self):
        import os
        import shutil

        from copilot import runtime_host

        if self._env is None:
            os.environ.pop("QD_COPILOT_DIR", None)
        else:
            os.environ["QD_COPILOT_DIR"] = self._env
        runtime_host._state.clear()
        runtime_host._state.update(self._state)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_init_without_config(self):
        app = sp.SearchPanelApp()
        self.assertFalse(app._copilot_enabled)
        self.assertEqual(app._panel_mode, "auto")
        self.assertEqual(app._view, "idle")
        self.assertIsNone(app._copilot_view)


class TestEventFormatting(unittest.TestCase):
    """CopilotView._format_event：事件 → 展示文本与颜色 tag。"""

    def _fmt(self, kind, payload):
        from copilot.panel_view import CopilotView

        class _Ev:
            pass

        ev = _Ev()
        ev.kind = kind
        ev.payload = payload
        return CopilotView._format_event(ev)

    def test_user_line(self):
        text, tag = self._fmt("user", {"text": "读 README", "context_refs": []})
        self.assertIn("你:", text)
        self.assertIn("读 README", text)
        self.assertEqual(tag, "user")

    def test_user_with_refs(self):
        text, _ = self._fmt("user", {"text": "概括", "context_refs": ["a.md"]})
        self.assertIn("[1 个附件]", text)

    def test_assistant(self):
        text, tag = self._fmt("assistant", {"text": "三点概况…"})
        self.assertTrue(text.startswith("三点概况"))
        self.assertEqual(tag, "assistant")

    def test_tool_line(self):
        text, tag = self._fmt(
            "tool",
            {"tool_call_id": "t1", "title": "读文件", "status": "completed",
             "output": "line1\nline2"},
        )
        self.assertIn("读文件", text)
        self.assertIn("(completed)", text)
        self.assertIn("→ line1", text)
        self.assertEqual(tag, "tool")

    def test_state_settled_shows_reply_not_done(self):
        text, tag = self._fmt("state", {"state": "SETTLED", "stop_reason": "end_turn"})
        self.assertIn("已回复", text)      # 不写"任务完成"
        self.assertIn("end_turn", text)    # 原始原因保留
        self.assertEqual(tag, "state")

    def test_state_interrupted(self):
        text, _ = self._fmt("state", {"state": "INTERRUPTED", "detail": "连接中断"})
        self.assertIn("中断", text)

    def test_store_failure_note(self):
        text, tag = self._fmt(
            "state", {"state": "FAILED", "error_code": "store_write_failed",
                      "detail": "disk full"})
        self.assertIn("记录保存失败", text)
        self.assertEqual(tag, "error")

    def test_permission_request_card_text(self):
        text, tag = self._fmt(
            "permission_request", {"tool_title": "写入 config.toml"})
        self.assertIn("待确认", text)
        self.assertIn("config.toml", text)
        self.assertEqual(tag, "warn")

    def test_permission_without_detail(self):
        text, _ = self._fmt("permission_request", {"tool_title": ""})
        self.assertIn("操作详情暂不可用", text)

    def test_usage_line(self):
        text, _ = self._fmt("usage", {"used": 100, "size": 2000})
        self.assertIn("100/2000", text)


class TestCopilotVisibility(unittest.TestCase):
    def test_restore_minimized_window_without_wallpaper_gate(self):
        app = sp.SearchPanelApp()
        app._panel_mode = "copilot"
        app._wallpaper_ticks = 99
        app._drain_results = lambda: None
        app._debouncer.due = lambda now: None
        calls = []

        class Root:
            def state(self):
                return "iconic"

            def winfo_viewable(self):
                return False

            def deiconify(self):
                calls.append("deiconify")

            def lift(self):
                calls.append("lift")

            def after(self, ms, callback):
                calls.append(("after", ms))

            def winfo_id(self):
                return 1

        app.root = Root()
        with patch.object(sp, "wallpaper_is_classic", side_effect=AssertionError("wallpaper gate")):
            app._tick()
        self.assertEqual(calls[:2], ["deiconify", "lift"])


class TestViewSwitch(unittest.TestCase):
    """withdrawn Tk 上验证视图切换：文件构件隐藏/恢复、草稿保留。"""

    def _app(self):
        import tkinter as tk
        import tkinter.font as tkfont
        app = sp.SearchPanelApp()
        app.root = tk.Tk()
        app.root.withdraw()
        app.font = tkfont.Font(family="Courier", size=-17)
        app.small_font = tkfont.Font(family="Courier", size=-13)
        app._head_row = tk.Frame(app.root)
        app.head = tk.Label(app._head_row)
        app.copilot_head = tk.Label(app._head_row)  # 模拟副驾可用时的 tab
        app.box = tk.Frame(app.root, height=30)
        app.box.pack_propagate(False)
        app.hint = tk.Label(app.box)
        app.hint.pack(fill="both", expand=True)
        app.entry = tk.Entry(app.box)
        app.placeholder = tk.Label(app.box)
        app.sep = tk.Frame(app.root)
        app.results_host = tk.Frame(app.root)
        app._base_h = 64
        # 注入桩副驾视图（不构造真实 CopilotView/控制器）
        class _StubView:
            def __init__(self):

                self.draft = "草稿保留"
                self.frame = tk.Frame(app.root)
                self.shown = False

            def show(self):
                self.shown = True
                self.frame.pack(fill="both", expand=True)

            def hide(self):
                self.shown = False
                self.frame.pack_forget()

        app._copilot_view = _StubView()
        app._copilot_enabled = True
        return app

    def test_open_copilot_hides_files(self):
        app = self._app()
        app._activate()
        app._open_copilot()
        app.root.update_idletasks()
        self.assertEqual(app._view, "copilot")
        self.assertFalse(app.box.winfo_manager())
        self.assertFalse(app.results_host.winfo_manager())
        self.assertTrue(app._copilot_view.shown)
        self.assertEqual(app.machine.state, "IDLE")  # 文件状态机已复位
        app.root.destroy()

    def test_search_focusout_after_copilot_open_keeps_expanded_height(self):
        """首次点击后的迟到 Entry FocusOut 不得把副驾压回搜索高度。"""
        app = self._app()
        app._open_copilot()
        app.root.update_idletasks()
        expanded = app.root.winfo_height()
        app._deactivate("blur")
        app.root.update_idletasks()
        self.assertEqual(app._view, "copilot")
        self.assertEqual(app.root.winfo_height(), expanded)
        app.root.destroy()

    def test_close_copilot_restores_files_widgets(self):
        app = self._app()
        app._open_copilot()
        app._close_copilot(to_files=False)
        app.root.update_idletasks()
        self.assertEqual(app._view, "idle")
        self.assertTrue(app.box.winfo_manager())
        self.assertTrue(app.results_host.winfo_manager())
        self.assertFalse(app._copilot_view.shown)
        app.root.destroy()

    def test_collapse_keeps_stub_state(self):
        app = self._app()
        app._open_copilot()
        app._close_copilot(to_files=False)
        # 桩视图对象保留（草稿在视图内，不因收起销毁）
        self.assertIsNotNone(app._copilot_view)
        app._open_copilot()
        self.assertTrue(app._copilot_view.shown)
        app.root.destroy()

    def test_activate_while_copilot_returns_to_files(self):
        app = self._app()
        app._open_copilot()
        app._activate()  # 点 SEARCH tab
        app.root.update_idletasks()
        self.assertEqual(app._view, "files")
        self.assertEqual(app.machine.state, "ACTIVE")
        self.assertFalse(app._copilot_view.shown)
        app.root.destroy()

    def test_file_deactivate_resets_view(self):
        app = self._app()
        app._activate()
        app._deactivate("esc")
        self.assertEqual(app._view, "idle")
        app.root.destroy()


class TestMarkdownTable(unittest.TestCase):
    def test_streamed_table_becomes_labeled_rows(self):
        import tkinter as tk
        import tkinter.font as tkfont
        from copilot.panel_view import CopilotView

        root = tk.Tk()
        root.withdraw()
        font = tkfont.Font(root=root, family="Segoe UI", size=-20)
        view = CopilotView(root, font, font, get_controller=lambda: (None, "fixture"))
        try:
            view.transcript.configure(state="normal")
            for chunk in ("| 来源 | 结", "果 |\n|---|---|\n| 00 | Cold",
                          " open |\n| 01 | 108 |"):
                view._append_markdown_chunk(chunk)
            view._finish_markdown_stream()
            rendered = view.transcript.get("1.0", "end")
            self.assertIn("来源：00", rendered)
            self.assertIn("结果：Cold open", rendered)
            self.assertIn("来源：01", rendered)
            self.assertNotIn("|---|---|", rendered)
        finally:
            view.destroy()
            root.destroy()


if __name__ == "__main__":
    unittest.main()
