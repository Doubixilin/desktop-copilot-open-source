"""01 票：fake ACP agent 可被官方 Python 客户端驱动（不依赖 key/网络）。

覆盖：initialize/new/两轮 prompt 会话复用/取消/许可/断管/终止原因——
这些场景供 02/03 票的控制器测试复用。
"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from copilot import acp_adapter  # noqa: E402

PYTHON = sys.executable


def _write_script(tmp_path: Path, script: dict) -> Path:
    p = tmp_path / "fake_script.json"
    p.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    return p


def _spawn_fake(script_path: Path):
    """直接用 asyncio subprocess + 官方 connection（与产品路径一致）。"""
    return acp_adapter.spawn_agent_subprocess(
        PYTHON,
        ["-m", "copilot.fake_agent", "--script", str(script_path)],
        launch_cwd=str(REPO_ROOT),
        initialize_timeout=15.0,
    )


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_control_plane_against_fake(tmp_path):
    script = _write_script(
        tmp_path,
        {
            "turns": [
                {"updates": [{"kind": "agent_text", "text": "hi"}],
                 "stop_reason": "end_turn"}
            ]
        },
    )

    async def body():
        collected = []

        async def on_update(session_id, update):
            collected.append(getattr(update, "session_update", "?"))

        spawned = await acp_adapter.spawn_agent_subprocess(
            PYTHON,
            ["-m", "copilot.fake_agent", "--script", str(script)],
            launch_cwd=str(REPO_ROOT),
            on_session_update=on_update,
            initialize_timeout=15.0,
        )
        conn = spawned.connection
        assert spawned.initialize_response.agent_info.name == "fake-acp-agent"
        new = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
        listing = await conn.list_sessions()
        assert any(
            s.session_id == new.session_id for s in listing.sessions
        )
        await conn.close_session(new.session_id)
        await conn.resume_session(
            session_id=new.session_id, cwd=str(tmp_path), mcp_servers=[]
        )
        await conn.close_session(new.session_id)
        await spawned.close()
        return collected

    assert run(body()) == []


def test_two_turns_same_session_and_stop_reasons(tmp_path):
    script = _write_script(
        tmp_path,
        {
            "turns": [
                {
                    "updates": [
                        {"kind": "user_echo"},
                        {"kind": "agent_text", "text": "第一轮回复"},
                    ],
                    "stop_reason": "end_turn",
                },
                {
                    "match": "第二",
                    "updates": [{"kind": "agent_text", "text": "第二轮回复"}],
                    "stop_reason": "max_tokens",
                },
            ]
        },
    )

    async def body():
        from acp.schema import TextContentBlock

        texts: list[str] = []

        async def on_update(session_id, update):
            if getattr(update, "session_update", "") == "agent_message_chunk":
                texts.append(update.content.text)

        spawned = await acp_adapter.spawn_agent_subprocess(
            PYTHON,
            ["-m", "copilot.fake_agent", "--script", str(script)],
            launch_cwd=str(REPO_ROOT),
            on_session_update=on_update,
            initialize_timeout=15.0,
        )
        conn = spawned.connection
        new = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
        sid = new.session_id

        r1 = await conn.prompt(sid, [TextContentBlock(type="text", text="第一问")])
        assert r1.stop_reason == "end_turn"
        r2 = await conn.prompt(sid, [TextContentBlock(type="text", text="第二问")])
        assert r2.stop_reason == "max_tokens"
        await spawned.close()
        return texts

    texts = run(body())
    assert "第一轮回复" in texts and "第二轮回复" in texts


def test_cancel_settles_as_cancelled(tmp_path):
    script = _write_script(
        tmp_path,
        {
            "turns": [
                {
                    "updates": [
                        {"kind": "agent_text", "text": "开始"},
                        {"kind": "silent", "seconds": 30},
                        {"kind": "agent_text", "text": "不应到达"},
                    ],
                    "stop_reason": "end_turn",
                }
            ],
            "on_cancel": {"settle_after": 0.1, "stop_reason": "cancelled"},
        },
    )

    async def body():
        from acp.schema import TextContentBlock

        spawned = await _spawn_fake(script)
        conn = spawned.connection
        new = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])

        async def prompt_task():
            return await conn.prompt(
                new.session_id, [TextContentBlock(type="text", text="长任务")]
            )

        task = asyncio.get_running_loop().create_task(prompt_task())
        await asyncio.sleep(0.5)
        await conn.cancel(new.session_id)
        resp = await asyncio.wait_for(task, timeout=10)
        await spawned.close()
        return resp.stop_reason

    assert run(body()) == "cancelled"


def test_permission_roundtrip(tmp_path):
    script = _write_script(
        tmp_path,
        {
            "turns": [
                {
                    "updates": [
                        {"kind": "tool_start", "id": "t1", "title": "写文件"},
                        {
                            "kind": "permission",
                            "tool_call_id": "t1",
                            "title": "写入 config.toml",
                            "options": [
                                {"optionId": "allow", "name": "允许", "kind": "allow_once"},
                                {"optionId": "reject", "name": "拒绝", "kind": "reject_once"},
                            ],
                            "on_selected": "已允许",
                        },
                        {"kind": "agent_text", "text": "完成"},
                    ],
                    "stop_reason": "end_turn",
                }
            ]
        },
    )

    async def body():
        from acp.schema import (
            AllowedOutcome,
            RequestPermissionResponse,
            TextContentBlock,
        )

        seen: list[tuple] = []

        async def on_permission(session_id, tool_call, options):
            seen.append((tool_call.tool_call_id, tuple(o.option_id for o in options)))
            return RequestPermissionResponse(
                outcome=AllowedOutcome(outcome="selected", option_id="allow")
            )

        spawned = await acp_adapter.spawn_agent_subprocess(
            PYTHON,
            ["-m", "copilot.fake_agent", "--script", str(script)],
            launch_cwd=str(REPO_ROOT),
            on_request_permission=on_permission,
            initialize_timeout=15.0,
        )
        conn = spawned.connection
        new = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
        resp = await conn.prompt(
            new.session_id, [TextContentBlock(type="text", text="写一下")]
        )
        await spawned.close()
        return resp.stop_reason, seen

    stop_reason, seen = run(body())
    assert stop_reason == "end_turn"
    assert seen == [("t1", ("allow", "reject"))]


def test_pipe_break_mid_prompt(tmp_path):
    script = _write_script(
        tmp_path,
        {
            "turns": [
                {
                    "updates": [
                        {"kind": "agent_text", "text": "部分输出"},
                        {"kind": "exit", "code": 1},
                    ],
                    "stop_reason": "end_turn",
                }
            ]
        },
    )

    async def body():
        from acp.schema import TextContentBlock

        spawned = await _spawn_fake(script)
        conn = spawned.connection
        new = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
        with pytest.raises((ConnectionError, asyncio.IncompleteReadError)):
            await asyncio.wait_for(
                conn.prompt(
                    new.session_id,
                    [TextContentBlock(type="text", text="断管测试")],
                ),
                timeout=15,
            )
        await spawned.close()
        return spawned.process.returncode

    assert run(body()) == 1


def test_default_deny_without_permission_handler(tmp_path):
    """无许可处理器时必须默认拒绝（不默认允许）。"""
    script = _write_script(
        tmp_path,
        {
            "turns": [
                {
                    "updates": [
                        {
                            "kind": "permission",
                            "tool_call_id": "t1",
                            "options": [
                                {"optionId": "allow", "name": "允许", "kind": "allow_once"},
                                {"optionId": "reject", "name": "拒绝", "kind": "reject_once"},
                            ],
                            "on_selected": "已允许",
                        },
                        {"kind": "agent_text", "text": "完成"},
                    ],
                    "stop_reason": "end_turn",
                }
            ]
        },
    )

    async def body():
        from acp.schema import TextContentBlock

        spawned = await _spawn_fake(script)  # 不传 on_request_permission
        conn = spawned.connection
        new = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
        resp = await conn.prompt(
            new.session_id, [TextContentBlock(type="text", text="需许可")]
        )
        await spawned.close()
        return resp.stop_reason

    assert run(body()) == "end_turn"  # 默认拒绝后轮次正常结束
