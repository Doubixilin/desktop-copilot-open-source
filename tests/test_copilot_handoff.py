"""05 票：上下文交接（文件引用/缺失校验/会话快照/项目切换）。"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from copilot import deck_client  # noqa: E402
from copilot.controller import ControllerError  # noqa: E402

# 复用 02/03 票的 harness 基建
sys.path.insert(0, str(REPO / "tests"))
from test_copilot_controller import Harness, wait_until  # noqa: E402

ECHO_SCRIPT = {
    "turns": [
        {"updates": [{"kind": "user_echo"}, {"kind": "agent_text", "text": "已读附件"}],
         "stop_reason": "end_turn"}
    ]
}


def test_missing_context_ref_rejected(tmp_path):
    h = Harness(tmp_path, ECHO_SCRIPT)
    try:
        conv = h.new_conversation()
        with pytest.raises(ControllerError) as ei:
            h.submit(conv, "看看这个文件", context_refs=[str(tmp_path / "不存在 文件.md")])
        assert ei.value.code == "invalid_context_ref"
        # 未派单
        snap = h.controller.snapshot()
        assert snap["active_run"] is None
        assert h.controller.events(conv.conversation_id) == []
    finally:
        h.close()


def test_cjk_space_context_ref_dispatches(tmp_path):
    """中文+空格路径 → resource_link；发送成功后才清 chip（C13）。"""
    h = Harness(tmp_path, ECHO_SCRIPT)
    try:
        target = tmp_path / "报告 目录" / "会议 纪要.md"
        target.parent.mkdir(exist_ok=True)
        target.write_text("内容", encoding="utf-8")
        conv = h.new_conversation()
        run = h.submit(conv, "概括此文件", context_refs=[str(target)])
        assert h.wait_terminal(run.run_id) == "SETTLED"
        # store 里记录了附件
        stored = h.store.get_run(run.run_id)
        assert stored.context_refs == [str(target)]
        # fake 回显了用户消息（含文本），并完成一轮
        events = h.controller.events(conv.conversation_id)
        assert any(e.kind == "user" and "概括此文件" in e.payload.get("text", "")
                   for e in events)
    finally:
        h.close()


def test_project_switch_creates_new_conversation(tmp_path):
    """换项目即新对话：旧 session 不接收新项目指令。"""
    h = Harness(tmp_path, ECHO_SCRIPT)
    try:
        ws1 = h.workspace
        conv1 = h.controller.create_conversation(str(ws1))
        r1 = h.submit(conv1, "第一问")
        assert h.wait_terminal(r1.run_id) == "SETTLED"
        sid1 = h.store.get_conversation(conv1.conversation_id).dsh_session_id

        ws2 = tmp_path / "项目 二 目录"
        ws2.mkdir()
        conv2 = h.controller.create_conversation(str(ws2))  # 面板换项目走同一路径
        r2 = h.submit(conv2, "新项目第一问")
        assert h.wait_terminal(r2.run_id) == "SETTLED"
        sid2 = h.store.get_conversation(conv2.conversation_id).dsh_session_id
        assert sid1 != sid2
    finally:
        h.close()


# ---- deck_client / 会话快照 ----

DECK_FIXTURE = {
    "ts": 1790441408.9,
    "sessions": [
        {"tool": "codex", "title": "修登录", "state": "RUN", "age": "5m",
         "preview": "正在改 auth.py"},
        {"tool": "dsh", "title": None, "state": "CONFIRM", "age": "1m",
         "preview": None},
    ],
}


def test_snapshot_format():
    text = deck_client.format_sessions_snapshot(DECK_FIXTURE)
    assert "桌面会话观察快照" in text
    assert "观察数据" in text  # 明确是观察，不宣称可控
    assert "codex" in text and "RUN" in text
    assert "标题不可见" in text  # 裁剪字段如实标注
    assert "2026" in text  # 采样时间可读


def test_snapshot_truncates():
    many = {"ts": 1.0, "sessions": [
        {"tool": f"t{i}", "title": f"s{i}", "state": "RUN", "age": "1m"}
        for i in range(30)
    ]}
    text = deck_client.format_sessions_snapshot(many, max_sessions=5)
    assert "另有 25 条未列出" in text


def test_fetch_deck_unavailable():
    with pytest.raises(deck_client.DeckUnavailable):
        deck_client.fetch_deck("http://127.0.0.1:1", timeout=0.5)


def test_fetch_deck_with_local_server(tmp_path, monkeypatch):
    """起真实数据服务（独立端口/独立数据目录）验证快照读取与令牌边界。"""
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    monkeypatch.setenv("QD_COPILOT_DIR", str(tmp_path / "copilot"))
    # 直接复用 server 模块的 handler 构造（不 import 其全局副作用以外的新进程）
    import server as qd_server

    # The HTTP integration test must not scan the developer's real sessions or GPU.
    monkeypatch.setattr(qd_server, "deck_state", lambda: dict(DECK_FIXTURE))
    monkeypatch.setattr(qd_server, "_configured_token", lambda: "synthetic-test-token")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), qd_server.Handler)
    port = httpd.server_port
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        deck = deck_client.fetch_deck(f"http://127.0.0.1:{port}")
        assert isinstance(deck.get("sessions"), list)
        snap = deck_client.format_sessions_snapshot(deck)
        assert "观察数据" in snap
    finally:
        httpd.shutdown()
        httpd.server_close()
