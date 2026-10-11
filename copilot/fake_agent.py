"""Fake ACP agent：测试夹具，不依赖网络、凭据与真实模型。

以 ``python -m copilot.fake_agent --script <json>`` 运行；stdout 只有 ACP
JSON-RPC 帧。行为由 JSON 脚本描述，覆盖 01 票要求的场景：有序消息、
工具许可、取消延迟、断管（进程退出）、无更新长任务、多种终止原因。

脚本格式（顶层）::

    {
      "agent_info": {"name": "fake-acp-agent", "version": "0.1"},
      "new_session_error": "optional json-rpc error message",
      "config_options": [{"configId": "model", "type": "select",
                          "options": [{"id": "m1", "name": "Fake Model"}]}],
      "turns": [
        {"match": "hello",                    # 可选：prompt 文本需含此串
         "updates": [STEP, ...],
         "stop_reason": "end_turn",
         "prompt_error": "optional error message"},
        ...
      ],
      "on_cancel": {"settle_after": 0.3, "stop_reason": "cancelled"}
    }

STEP 取值::

    {"kind": "user_echo"}                       # 回显用户输入为 user_message_chunk
    {"kind": "agent_text", "text": "..."}       # agent_message_chunk
    {"kind": "thought_text", "text": "..."}     # agent_thought_chunk
    {"kind": "tool_start", "id": "t1", "title": "Read file", "tool_kind": "read",
     "raw_input": {...}}
    {"kind": "tool_update", "id": "t1", "status": "completed",
     "content_text": "..."}
    {"kind": "permission", "tool_call_id": "t1",
     "title": "Write file", "options": [{"optionId": "allow", "name": "允许",
     "kind": "allow_once"}, {"optionId": "reject", "name": "拒绝",
     "kind": "reject_once"}],
     "on_selected": "allow_text 或 reject_text 之后的 agent_text",
     "timeout_error_after": 30.0}   # 可选：超时后以 JSON-RPC error 拒答
    {"kind": "usage", "used": 100, "size": 200000}
    {"kind": "sleep", "seconds": 0.2}
    {"kind": "silent", "seconds": 300}          # 长时间无更新
    {"kind": "exit", "code": 1}                 # 断管：直接退出进程

默认脚本：单轮回显 + end_turn。运行期诊断写 stderr。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

DEFAULT_SCRIPT: dict[str, Any] = {
    "agent_info": {"name": "fake-acp-agent", "version": "0.1.0"},
    "turns": [
        {
            "updates": [
                {"kind": "user_echo"},
                {"kind": "agent_text", "text": "fake reply"},
            ],
            "stop_reason": "end_turn",
        }
    ],
    "on_cancel": {"settle_after": 0.2, "stop_reason": "cancelled"},
}


def _log(msg: str) -> None:
    print(f"[fake-agent] {msg}", file=sys.stderr, flush=True)


class FakeAgent:
    def __init__(self, script: dict[str, Any], state_path: str | None = None):
        self.script = script
        self.conn: Any = None
        self.sessions: dict[str, dict[str, Any]] = {}
        self._seq = 0
        self.cancel_event = asyncio.Event()
        self.closed_sessions: set[str] = set()
        self.state_path = state_path
        self._load_state()

    # ---- 跨进程会话持久化（模拟 DSH 的持久 session/list/resume）----
    def _load_state(self) -> None:
        if not self.state_path:
            return
        import json as _json

        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = _json.load(fh)
            self.sessions = data.get("sessions", {})
            max_seq = 0
            for sid in self.sessions:
                try:
                    max_seq = max(max_seq, int(sid.rsplit("-", 1)[-1]))
                except ValueError:
                    pass
            self._seq = max_seq
        except (OSError, ValueError):
            pass

    def _save_state(self) -> None:
        if not self.state_path:
            return
        import json as _json

        try:
            with open(self.state_path, "w", encoding="utf-8") as fh:
                _json.dump(
                    {"sessions": {k: {kk: vv for kk, vv in v.items()
                                      if kk != "last_prompt_text"}
                                  for k, v in self.sessions.items()}},
                    fh,
                    ensure_ascii=False,
                )
        except OSError:
            pass

    # ---- Agent 接口（由官方 SDK router 调度）----
    def on_connect(self, conn) -> None:
        self.conn = conn

    async def initialize(self, protocol_version: int, client_capabilities=None, client_info=None, **kw):
        from acp.schema import (
            AgentCapabilities,
            Implementation,
            InitializeResponse,
            SessionCapabilities,
        )

        info = self.script.get("agent_info", DEFAULT_SCRIPT["agent_info"])
        return InitializeResponse(
            protocol_version=min(int(protocol_version), 1) or 1,
            agent_capabilities=AgentCapabilities(
                load_session=False,
                session_capabilities=SessionCapabilities(
                    list={}, resume={}, close={}
                ),
            ),
            auth_methods=[],
            agent_info=Implementation(
                name=info.get("name", "fake-acp-agent"),
                version=info.get("version", "0.1.0"),
            ),
        )

    async def new_session(self, cwd: str, additional_directories=None, mcp_servers=None, **kw):
        from acp.schema import (
            NewSessionResponse,
            SessionConfigOptionSelect,
            SessionConfigSelectOption,
        )

        if "new_session_error" in self.script:
            raise RuntimeError(self.script["new_session_error"])
        self._seq += 1
        session_id = f"fake-s-{self._seq}"
        self.sessions[session_id] = {
            "cwd": cwd,
            "turn_index": 0,
            "title": f"Fake Session {self._seq}",
        }
        self._save_state()
        config_options = []
        for opt in self.script.get("config_options", []):
            config_options.append(
                SessionConfigOptionSelect(
                    config_id=opt.get("configId", "model"),
                    title=opt.get("title", "Model"),
                    options=[
                        SessionConfigSelectOption(id=o["id"], name=o.get("name", o["id"]))
                        for o in opt.get("options", [])
                    ],
                    default_option_id=opt.get("default"),
                )
            )
        return NewSessionResponse(
            session_id=session_id,
            config_options=config_options or None,
        )

    async def list_sessions(self, cwd: str | None = None, cursor: str | None = None, **kw):
        from acp.schema import ListSessionsResponse, SessionInfo

        infos = []
        for sid, meta in self.sessions.items():
            if sid in self.closed_sessions:
                continue
            if cwd is not None and meta["cwd"] != cwd:
                continue
            infos.append(
                SessionInfo(session_id=sid, cwd=meta["cwd"], title=meta["title"])
            )
        return ListSessionsResponse(sessions=infos)

    async def resume_session(self, session_id: str, cwd: str, additional_directories=None, mcp_servers=None, **kw):
        from acp.schema import ResumeSessionResponse

        meta = self.sessions.get(session_id)
        if meta is None:
            raise RuntimeError(f"unknown session {session_id}")
        # DSH 语义：close 释放活动 agent 但持久化保留，非活跃会话可 resume
        if meta["cwd"] != cwd:
            raise RuntimeError(f"cwd mismatch: {cwd!r} != {meta['cwd']!r}")
        self.closed_sessions.discard(session_id)
        self._save_state()
        return ResumeSessionResponse()

    async def close_session(self, session_id: str, **kw):
        from acp.schema import CloseSessionResponse

        self.closed_sessions.add(session_id)
        self._save_state()
        return CloseSessionResponse()

    async def set_config_option(self, config_id: str, session_id: str, value, **kw):
        meta = self.sessions.get(session_id)
        if meta is None:
            raise RuntimeError(f"unknown session {session_id}")
        meta.setdefault("config", {})[config_id] = value
        return None

    async def cancel(self, session_id: str, **kw) -> None:
        self.cancel_event.set()

    async def prompt(self, session_id: str, prompt, **kw):
        meta = self.sessions.get(session_id)
        if meta is None:
            raise RuntimeError(f"unknown session {session_id}")
        if session_id in self.closed_sessions:
            # DSH 语义：close 释放活动 agent；非活跃会话必须先 resume 才能
            # prompt（复核 S1：否则取消清理后误复用已关闭会话会假绿）
            raise RuntimeError("session is not active; resume required")
        turn = self._pick_turn(meta, prompt)
        self.cancel_event = asyncio.Event()
        if "prompt_error" in turn:
            raise RuntimeError(turn["prompt_error"])
        try:
            await self._run_steps(session_id, turn.get("updates", []))
        except _PromptError as exc:
            raise RuntimeError(str(exc)) from None
        if self.cancel_event.is_set():
            on_cancel = self.script.get("on_cancel", {})
            await asyncio.sleep(float(on_cancel.get("settle_after", 0.1)))
            from acp.schema import PromptResponse

            return PromptResponse(
                stop_reason=on_cancel.get("stop_reason", "cancelled")
            )
        from acp.schema import PromptResponse

        return PromptResponse(stop_reason=turn.get("stop_reason", "end_turn"))

    # ---- 内部 ----
    def _pick_turn(self, meta: dict, prompt_blocks) -> dict[str, Any]:
        turns = self.script.get("turns") or DEFAULT_SCRIPT["turns"]
        text = " ".join(
            getattr(b, "text", "") or "" for b in (prompt_blocks or [])
        )
        start = meta["turn_index"]
        tail = turns[start:]
        # 带 match 的轮次优先于无 match 的兜底轮；先看游标之后，再从头找
        for base, pool in ((start, tail), (0, turns)):
            for offset, turn in enumerate(pool, start=base):
                match = turn.get("match")
                if match is not None and match in text:
                    meta["turn_index"] = offset + 1
                    return turn
        for base, pool in ((start, tail), (0, turns)):
            for offset, turn in enumerate(pool, start=base):
                if turn.get("match") is None:
                    meta["turn_index"] = offset + 1
                    return turn
        return {"updates": [{"kind": "agent_text", "text": "no matching turn"}],
                "stop_reason": "end_turn"}

    async def _send_update(self, session_id: str, update) -> None:
        # SDK 的 camelCase 别名是生成包装器（签名不符）；必须用 snake_case
        await self.conn.session_update(session_id, update)

    async def _run_steps(self, session_id: str, steps: list[dict[str, Any]]) -> None:
        from acp.schema import (
            AgentMessageChunk,
            AgentThoughtChunk,
            ContentToolCallContent,
            TextContentBlock,
            ToolCallProgress,
            ToolCallStart,
            UsageUpdate,
            UserMessageChunk,
        )

        for step in steps:
            if self.cancel_event.is_set():
                return
            kind = step.get("kind")
            if kind == "user_echo":
                text = step.get("text") or self._last_user_text(session_id)
                await self._send_update(
                    session_id,
                    UserMessageChunk(
                        session_update="user_message_chunk",
                        content=TextContentBlock(type="text", text=text),
                    ),
                )
            elif kind == "agent_text":
                await self._send_update(
                    session_id,
                    AgentMessageChunk(
                        session_update="agent_message_chunk",
                        content=TextContentBlock(type="text", text=step.get("text", "")),
                    ),
                )
            elif kind == "thought_text":
                await self._send_update(
                    session_id,
                    AgentThoughtChunk(
                        session_update="agent_thought_chunk",
                        content=TextContentBlock(type="text", text=step.get("text", "")),
                    ),
                )
            elif kind == "tool_start":
                await self._send_update(
                    session_id,
                    ToolCallStart(
                        session_update="tool_call",
                        tool_call_id=step.get("id", "tool-1"),
                        kind=step.get("tool_kind", "other"),
                        title=step.get("title", "Tool"),
                        raw_input=step.get("raw_input"),
                    ),
                )
            elif kind == "tool_update":
                content = None
                if "content_text" in step:
                    content = [
                        ContentToolCallContent(
                            type="content",
                            content=TextContentBlock(
                                type="text", text=step["content_text"]
                            ),
                        )
                    ]
                await self._send_update(
                    session_id,
                    ToolCallProgress(
                        session_update="tool_call_update",
                        tool_call_id=step.get("id", "tool-1"),
                        status=step.get("status", "completed"),
                        title=step.get("title"),
                        content=content,
                    ),
                )
            elif kind == "permission":
                await self._handle_permission(session_id, step)
            elif kind == "usage":
                await self._send_update(
                    session_id,
                    UsageUpdate(
                        session_update="usage_update",
                        used=int(step.get("used", 0)),
                        size=int(step.get("size", 0)),
                    ),
                )
            elif kind == "sleep":
                await self._interruptible_sleep(float(step.get("seconds", 0)))
            elif kind == "silent":
                # 长时间无更新；仍可被取消打断
                await self._interruptible_sleep(float(step.get("seconds", 300)))
            elif kind == "exit":
                code = int(step.get("code", 1))
                _log(f"exit on request code={code}")
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(code)
            else:
                _log(f"unknown step kind: {kind}")

    def _last_user_text(self, session_id: str) -> str:
        return self.sessions.get(session_id, {}).get("last_prompt_text", "")

    async def _interruptible_sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.cancel_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _handle_permission(self, session_id: str, step: dict[str, Any]) -> None:
        from acp.schema import (
            AllowedOutcome,
            DeniedOutcome,
            PermissionOption,
            RequestPermissionResponse,
            ToolCallUpdate,
        )

        options = [
            PermissionOption(
                option_id=o.get("optionId", f"opt{i}"),
                name=o.get("name", f"Option {i}"),
                kind=o.get("kind", "allow_once"),
            )
            for i, o in enumerate(step.get("options", []))
        ]
        tool_call = ToolCallUpdate(
            tool_call_id=step.get("tool_call_id", "tool-1"),
            title=step.get("title", "Requesting permission"),
        )
        timeout = step.get("timeout_error_after")
        try:
            if timeout is not None:
                response = await asyncio.wait_for(
                    self.conn.request_permission(session_id, tool_call, options),
                    timeout=float(timeout),
                )
            else:
                response = await self.conn.request_permission(session_id, tool_call, options)
        except asyncio.TimeoutError:
            raise _PromptError(
                f"permission request timed out after {timeout}s"
            ) from None
        outcome = getattr(response, "outcome", None)
        outcome_kind = getattr(outcome, "outcome", None)
        selected = getattr(outcome, "option_id", None)
        _log(f"permission outcome={outcome_kind} selected={selected}")
        followup = step.get("on_selected")
        if followup:
            from acp.schema import AgentMessageChunk, TextContentBlock

            await self._send_update(
                session_id,
                AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=followup),
                ),
            )

    def remember_prompt(self, session_id: str, text: str) -> None:
        meta = self.sessions.get(session_id)
        if meta is not None:
            meta["last_prompt_text"] = text


class _PromptError(Exception):
    pass


async def _amain(script: dict[str, Any]) -> None:
    from acp.agent.connection import AgentSideConnection
    from acp.stdio import stdio_streams

    reader, writer = await stdio_streams()

    agent = FakeAgent(script, state_path=os.environ.get("FAKE_ACP_STATE"))

    # 在 prompt 进入前记录用户文本（供 user_echo）
    original_prompt = agent.prompt

    async def prompt_with_memory(session_id, prompt, **kw):
        text = " ".join(getattr(b, "text", "") or "" for b in (prompt or []))
        agent.remember_prompt(session_id, text)
        return await original_prompt(session_id, prompt, **kw)

    agent.prompt = prompt_with_memory  # type: ignore[method-assign]

    # session/resume、session/close 在 Python SDK 中标记 unstable；
    # fake agent 打开开关以便与真实 DSH 行为对齐。listening=False 避免
    # 构造器自动启动的接收循环与 listen() 再起一个循环双读 stdin。
    conn = AgentSideConnection(
        agent, writer, reader, use_unstable_protocol=True, listening=False
    )
    await conn.listen()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fake ACP agent for tests")
    parser.add_argument("--script", help="JSON 脚本路径；缺省读 FAKE_ACP_SCRIPT 环境变量")
    args = parser.parse_args(argv)

    script_path = args.script or os.environ.get("FAKE_ACP_SCRIPT")
    if script_path:
        with open(script_path, "r", encoding="utf-8") as fh:
            script = json.load(fh)
    else:
        script = DEFAULT_SCRIPT

    if os.environ.get("FAKE_ACP_DEBUG"):
        _log(f"script={json.dumps(script, ensure_ascii=False)[:400]}")

    try:
        asyncio.run(_amain(script))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
