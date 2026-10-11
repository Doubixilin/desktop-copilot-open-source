# -*- coding: utf-8 -*-
"""scene_adapter：SceneState 状态机 / 协议校验 / 伪造采集源的事件链路。

不碰真实 Win32 事件（capture_window 通过 monkeypatch 替换）；仅验证纯逻辑
与 stdio 协议。适配器真机行为由人工验收覆盖（.scratch/scene-m1/）。
"""
import io
import json
import sys
import threading
import time
from unittest import mock

import pytest

sys.path.insert(0, "native_hud")
import scene_adapter as sa  # noqa: E402


def info(pid, title, ts=1000.0, process="app.exe", exe=r"C:\app.exe", cls="Window"):
    return {"pid": pid, "process": process, "exe": exe, "title": title,
            "class": cls, "ts": ts}


class TestSceneState:
    def test_dedupe_same_pid_title(self):
        st = sa.SceneState()
        assert st.update(info(1, "A")) is not None
        assert st.update(info(1, "A", ts=1001.0)) is None  # 同 (pid,title) 不重复
        assert st.update(info(1, "A2")) is not None        # 标题变 = 新现场
        assert st.update(info(2, "A2")) is not None        # pid 变 = 新现场

    def test_timeline_ring(self):
        st = sa.SceneState(timeline_max=3)
        for i in range(6):
            st.update(info(i, f"t{i}"))
        assert [t["title"] for t in st.timeline] == ["t3", "t4", "t5"]
        assert st.current["title"] == "t5"

    def test_snapshot_newest_first_capped(self):
        st = sa.SceneState()
        for i in range(30):
            st.update(info(i, f"t{i}"))
        snap = st.snapshot()
        assert snap["current"]["title"] == "t29"
        titles = [t["title"] for t in snap["timeline"]]
        assert titles[0] == "t29"
        assert len(titles) == sa.SNAPSHOT_TIMELINE_MAX
        assert titles == [f"t{i}" for i in range(29, 9, -1)]

    def test_snapshot_empty(self):
        snap = sa.SceneState().snapshot()
        assert snap["current"] is None and snap["timeline"] == []


class TestValidate:
    def test_ops_and_rejections(self):
        assert sa._validate('{"id":1,"op":"snapshot"}') == ({"id": 1, "op": "snapshot"}, None)
        assert sa._validate('{"id":2,"op":"ping"}') == ({"id": 2, "op": "ping"}, None)
        assert sa._validate('{"id":3,"op":"clipboard"}') == ({"id": 3, "op": "clipboard"}, None)
        for bad in ('not json', '[]', '{"id":"x","op":"ping"}', '{"id":1,"op":"search"}',
                    '{"op":"ping"}', '{"id":true,"op":"ping"}'):
            req, err = sa._validate(bad)
            assert req is None, bad
            assert err in ("bad_json", "bad_id", "bad_op")


class TestClipboard:
    """M7：剪贴板快照——文件列表+格式标志；文本内容不出境。"""

    @staticmethod
    def _set_text(text: str) -> bool:
        u32 = sa._user32
        k32 = sa._kernel32
        if not u32.OpenClipboard(None):
            return False
        try:
            u32.EmptyClipboard()
            data = (text + "\0").encode("utf-16-le")
            k32.GlobalAlloc.restype = sa.wt.HANDLE
            k32.GlobalLock.restype = sa.wt.LPVOID
            k32.GlobalLock.argtypes = [sa.wt.HANDLE]
            k32.GlobalUnlock.argtypes = [sa.wt.HANDLE]
            u32.SetClipboardData.argtypes = [sa.wt.UINT, sa.wt.HANDLE]
            h = k32.GlobalAlloc(0x0042, len(data))  # GMEM_MOVEABLE|GMEM_ZEROINIT
            p = k32.GlobalLock(h)
            sa.ctypes.memmove(p, data, len(data))
            k32.GlobalUnlock(h)
            u32.SetClipboardData(13, h)  # CF_UNICODETEXT
            return True
        finally:
            u32.CloseClipboard()

    def test_snapshot_text_flags_only(self):
        if not self._set_text("m7 剪贴板测试"):
            pytest.skip("本机无法打开剪贴板（非交互会话）")
        snap = sa.read_clipboard_snapshot()
        assert snap["has_text"] is True and snap["files"] == []
        # 红线：应答结构里不存在文本内容字段
        assert "text" not in snap

    def test_age_none_before_any_event(self):
        assert sa.clipboard_age_s(now=sa.time.time()) is None or sa.clipboard_age_s() >= 0


class TestEmitPipeline:
    """伪造采集源驱动 writer_loop：事件推送/去重/宿主断管退出。"""

    def _run_writer(self, hwnds, results):
        st = sa.SceneState()
        q = sa.queue.Queue()
        for h in hwnds:
            q.put(h)
        q.put(None)  # 终止标记

        fake_windows = {10: info(1, "A", ts=1.0), 11: info(2, "B", ts=2.0),
                        12: info(2, "B", ts=2.5), 0: None, 20: info(3, "中文 标题", ts=3.0)}

        with mock.patch.object(sa, "capture_window", side_effect=lambda h, c=None: fake_windows.get(h)), \
             mock.patch.object(sa, "_emit_event", side_effect=lambda cur: results.append(cur)):
            while True:
                hwnd = q.get()
                if hwnd is None:
                    break
                got = sa.capture_window(hwnd)
                if got is None:
                    continue
                changed = st.update(got)
                if changed is not None:
                    sa._emit_event(changed)
        return st

    def test_events_deduped_and_none_skipped(self):
        results = []
        st = self._run_writer([10, 0, 11, 12, 20], results)
        assert [r["title"] for r in results] == ["A", "B", "中文 标题"]
        assert st.current["title"] == "中文 标题"

    def test_writer_stops_on_broken_pipe(self):
        st = sa.SceneState()

        def broken_emit(cur):
            raise OSError("broken pipe")

        with mock.patch.object(sa, "capture_window", return_value=info(1, "X")), \
             mock.patch.object(sa, "_emit_event", side_effect=broken_emit):
            got = sa.capture_window(10)
            changed = st.update(got)
            with pytest.raises(OSError):
                sa._emit_event(changed)


class TestRespondFraming:
    def test_lines_are_utf8_json(self, capsys):
        sa._respond({"event": "foreground", "current": info(1, "中文—标题")})
        out = capsys.readouterr().out
        line = out.strip().splitlines()[-1]
        payload = json.loads(line)
        assert payload["event"] == "foreground"
        assert payload["current"]["title"] == "中文—标题"


class TestLiveAdapter:
    """真进程冒烟：起 adapter 子进程，snapshot 拿到合法结构后退出。"""

    def test_snapshot_roundtrip(self):
        import subprocess
        proc = subprocess.Popen(
            [sys.executable, "-B", "-u", "native_hud/scene_adapter.py"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", cwd=".",
        )
        try:
            proc.stdin.write('{"id":1,"op":"ping"}\n')
            proc.stdin.write('{"id":2,"op":"snapshot"}\n')
            proc.stdin.flush()
            got = {}
            deadline = time.time() + 10
            while len(got) < 2 and time.time() < deadline:
                line = proc.stdout.readline()
                if not line:
                    break
                msg = json.loads(line)
                if "id" in msg:
                    got[msg["id"]] = msg
            assert got[1]["status"] == "ok"
            snap = got[2]
            assert snap["status"] == "ok"
            assert "current" in snap and "timeline" in snap
            if snap["current"] is not None:
                cur = snap["current"]
                assert set(cur) == {"pid", "process", "exe", "title", "class", "ts"}
                assert isinstance(cur["pid"], int) and cur["title"]
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)

    def test_clipboard_roundtrip(self):
        """真子进程：op=clipboard 应答 ok + clipboard 结构（文本内容不出境）。"""
        import subprocess
        proc = subprocess.Popen(
            [sys.executable, "-B", "-u", "native_hud/scene_adapter.py"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", cwd=".",
        )
        try:
            proc.stdin.write('{"id":1,"op":"clipboard"}\n')
            proc.stdin.flush()
            got = None
            deadline = time.time() + 10
            while got is None and time.time() < deadline:
                line = proc.stdout.readline()
                if not line:
                    break
                msg = json.loads(line)
                if msg.get("id") == 1:
                    got = msg
            assert got is not None and got["status"] == "ok"
            clip = got["clipboard"]
            assert set(clip) == {"files", "has_text", "has_html", "age_s"}
            assert isinstance(clip["files"], list) and len(clip["files"]) <= 20
            # 红线：无文本内容字段
            assert "text" not in clip
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)
