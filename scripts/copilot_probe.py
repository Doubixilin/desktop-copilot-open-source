"""副驾 ACP 控制面探针（01 票）。

默认只做无模型控制面验证：initialize -> session/new -> session/list ->
session/close -> session/resume -> close，全程不发送 prompt，不需要凭据。
真实模型冒烟必须显式 ``--real-smoke``（需要已配置的模型路由）。

用法示例::

    python scripts/copilot_probe.py --control \\
        --exe "D:/.../deepseek-harness-sdk-runtime-win-x64.exe" \\
        --dsh-home "%LOCALAPPDATA%/qoder-deck/copilot/probe-dsh-home" \\
        --cwd "D:/临时 目录/中文" --keep

    python scripts/copilot_probe.py --fake --fake-script tests/fixtures/fake_echo.json

错误输出不含环境变量全文或凭据。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from copilot import acp_adapter  # noqa: E402


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _mask(value: str) -> str:
    return value if len(value) < 8 else value[:3] + "..." + value[-2:]


class ProbeReport:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, key: str, value, status: str = "info") -> None:
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        self.lines.append(f"[{status}] {key}: {value}")

    def dump(self) -> str:
        return "\n".join(self.lines)


async def control_plane(
    exe: str,
    args: list[str],
    dsh_home: Path,
    cwd: Path,
    report: ProbeReport,
    stderr_path: Path | None = None,
    initialize_timeout: float = 60.0,
    launch_cwd: Path | None = None,
) -> tuple[bool, list[str]]:
    """返回 (成功, 不支持项列表)。"""
    ok = True
    unsupported: list[str] = []
    env = dict(os.environ)
    env["DSH_HOME"] = str(dsh_home)

    received_updates: list[str] = []

    async def on_update(session_id, update):
        received_updates.append(getattr(update, "session_update", "?"))

    spawned = await acp_adapter.spawn_agent_subprocess(
        exe,
        args,
        env=env,
        launch_cwd=str(launch_cwd) if launch_cwd else str(dsh_home),
        stderr_path=stderr_path,
        on_session_update=on_update,
        initialize_timeout=initialize_timeout,
    )
    report.add("pid", spawned.pid)
    init = spawned.initialize_response
    report.add(
        "initialize",
        {
            "protocolVersion": init.protocol_version,
            "agentInfo": {
                "name": init.agent_info.name if init.agent_info else None,
                "version": init.agent_info.version if init.agent_info else None,
            },
        },
    )
    caps = init.agent_capabilities
    if caps is not None:
        cap_dict = caps.model_dump(by_alias=True, exclude_none=True)
        report.add("agentCapabilities", cap_dict)

    conn = spawned.connection

    # new_session（中文+空格 cwd）
    t0 = time.monotonic()
    new_resp = await conn.new_session(cwd=str(cwd), mcp_servers=[])
    report.add(
        "session/new",
        {
            "sessionId": _mask(new_resp.session_id),
            "configOptions": [
                {
                    "id": o.id,
                    "type": o.type,
                    "options": len(getattr(o, "options", []) or []),
                    "currentValue": getattr(o, "current_value", None),
                }
                for o in (new_resp.config_options or [])
            ],
            "elapsed_s": round(time.monotonic() - t0, 2),
        },
    )
    session_id = new_resp.session_id

    # list_sessions（新建会话可能要到持久化 flush 后才可见）
    try:
        listing = await conn.list_sessions()
        found = any(
            getattr(s, "session_id", None) == session_id for s in (listing.sessions or [])
        )
        report.add("session/list", {"count": len(listing.sessions or []), "newVisible": found})
    except Exception as exc:
        unsupported.append("list")
        ok = False
        report.add("session/list", f"error: {type(exc).__name__}", "FAIL")

    # close + resume + close
    try:
        await conn.close_session(session_id)
        report.add("session/close", "ok")
    except Exception as exc:
        unsupported.append("close")
        ok = False
        report.add("session/close", f"error: {type(exc).__name__}", "FAIL")
    # close 应触发持久化 flush；此时 list 应能看到该会话
    try:
        listing2 = await conn.list_sessions()
        found2 = any(
            getattr(s, "session_id", None) == session_id for s in (listing2.sessions or [])
        )
        report.add(
            "session/list-after-close",
            {"count": len(listing2.sessions or []), "newVisible": found2},
            "PASS" if found2 else "WARN",
        )
        if not found2:
            unsupported.append("list-visibility-after-close")
    except Exception as exc:
        unsupported.append("list(2)")
        report.add("session/list-after-close", f"error: {type(exc).__name__}", "WARN")
    try:
        resume_resp = await conn.resume_session(
            session_id=session_id, cwd=str(cwd), mcp_servers=[]
        )
        report.add("session/resume", "ok")
    except Exception as exc:
        unsupported.append("resume")
        report.add("session/resume", f"error: {type(exc).__name__}: {exc}", "WARN")
    try:
        await conn.close_session(session_id)
        report.add("session/close(2)", "ok")
    except Exception as exc:
        unsupported.append("close(2)")
        report.add("session/close(2)", f"error: {type(exc).__name__}", "WARN")

    report.add("updates.received", received_updates)

    await spawned.close()
    spawned.close_stderr_file()
    report.add("process.returncode", spawned.process.returncode)
    return ok, unsupported


async def real_smoke(
    exe: str,
    args: list[str],
    dsh_home: Path,
    cwd: Path,
    prompt_text: str,
    report: ProbeReport,
    stderr_path: Path | None = None,
    followup_text: str | None = None,
    allow_permission: bool = False,
) -> bool:
    """显式真实模型冒烟：一次 prompt（可选同会话续问），等待终态。"""
    env = dict(os.environ)
    env["DSH_HOME"] = str(dsh_home)

    events: list[str] = []
    agent_chars = [0]

    async def on_update(session_id, update):
        kind = getattr(update, "session_update", "?")
        events.append(kind)
        if kind == "agent_message_chunk":
            content = getattr(update, "content", None)
            text = getattr(content, "text", "")
            agent_chars[0] += len(text)
            report.add("update.agent_text", text[:200])
        elif kind in ("tool_call", "tool_call_update"):
            report.add(
                "update.tool",
                {
                    "title": getattr(update, "title", None),
                    "status": getattr(update, "status", None),
                },
            )

    async def on_permission(session_id, tool_call, options):
        report.add(
            "permission.request",
            {
                "toolCallId": getattr(tool_call, "tool_call_id", None),
                "title": getattr(tool_call, "title", None),
                "options": [o.option_id for o in options],
            },
        )
        # 真实冒烟默认拒绝，避免未预期文件操作；--smoke-allow-permission
        # 显式选择 allow-once（用于 allow 分支的受控验收）
        from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

        if allow_permission:
            allow_ids = [o.option_id for o in options if "allow" in o.option_id]
            if allow_ids:
                return RequestPermissionResponse(
                    outcome=AllowedOutcome(outcome="selected", option_id=allow_ids[0])
                )
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    spawned = await acp_adapter.spawn_agent_subprocess(
        exe,
        args,
        env=env,
        launch_cwd=str(dsh_home),
        stderr_path=stderr_path,
        on_session_update=on_update,
        on_request_permission=on_permission,
    )
    conn = spawned.connection
    from acp.schema import TextContentBlock

    new_resp = await conn.new_session(cwd=str(cwd), mcp_servers=[])
    session_id = new_resp.session_id
    report.add("smoke.sessionId", _mask(session_id))
    t0 = time.monotonic()
    resp = await conn.prompt(
        session_id,
        [TextContentBlock(type="text", text=prompt_text)],
    )
    report.add(
        "smoke.result",
        {
            "stopReason": resp.stop_reason,
            "elapsed_s": round(time.monotonic() - t0, 1),
            "eventCount": len(events),
            "assistantChars": agent_chars[0],
        },
    )
    ok = resp.stop_reason == "end_turn"
    if followup_text and ok:
        events.clear()
        agent_chars[0] = 0
        t1 = time.monotonic()
        resp2 = await conn.prompt(
            session_id,  # 同会话续问（不重新 new_session）
            [TextContentBlock(type="text", text=followup_text)],
        )
        report.add(
            "smoke.followup",
            {
                "stopReason": resp2.stop_reason,
                "elapsed_s": round(time.monotonic() - t1, 1),
                "assistantChars": agent_chars[0],
            },
        )
        ok = resp2.stop_reason == "end_turn"
    await conn.close_session(session_id)
    await spawned.close()
    spawned.close_stderr_file()
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ACP control-plane probe")
    parser.add_argument("--exe", help="DSH runtime 可执行文件绝对路径")
    parser.add_argument("--runtime-args", default="--profile acp",
                        help="runtime 参数，空格分隔")
    parser.add_argument("--dsh-home", help="隔离的 DSH_HOME 绝对路径")
    parser.add_argument("--cwd", help="会话 cwd 绝对路径（默认临时中文目录）")
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    parser.add_argument("--stderr", help="子进程 stderr 落盘路径")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--fake", action="store_true", help="探测 fake agent（无需 DSH）")
    parser.add_argument("--fake-script", help="fake agent JSON 脚本")
    parser.add_argument("--real-smoke", action="store_true",
                        help="显式触发一次真实模型 prompt（需要凭据/路由）")
    parser.add_argument("--smoke-prompt", default="读取此目录下的 README 并用一句话概括。")
    parser.add_argument("--smoke-followup", default=None,
                        help="真实冒烟的续问文本（同一会话第二轮）")
    parser.add_argument("--smoke-allow-permission", action="store_true",
                        help="真实许可请求选择 allow-once（默认拒绝）")
    args = parser.parse_args(argv)

    report = ProbeReport()
    report.add("acp_sdk_version", acp_adapter.acp_sdk_version())
    report.add("python", sys.version.split()[0])

    tmp_roots: list[Path] = []
    try:
        if args.fake:
            exe = sys.executable
            rt_args = ["-m", "copilot.fake_agent"]
            if args.fake_script:
                rt_args += ["--script", str(Path(args.fake_script).resolve())]
            dsh_home = Path.cwd() / ".scratch-fake-home"
        else:
            if not args.exe:
                print("需要 --exe（DSH runtime 可执行文件）或 --fake", file=sys.stderr)
                return 2
            exe = str(Path(args.exe).resolve())
            rt_args = args.runtime_args.split()
            report.add("runtime.sha256", _sha256(Path(exe)))
            report.add("runtime.size", Path(exe).stat().st_size)
            if args.dsh_home:
                dsh_home = Path(args.dsh_home)
            else:
                dsh_home = Path.home() / ".qoder-deck-probe" / "dsh-home"

        dsh_home.mkdir(parents=True, exist_ok=True)
        if args.cwd:
            cwd = Path(args.cwd)
        else:
            cwd = dsh_home / "工作 目录 中文"
            cwd.mkdir(parents=True, exist_ok=True)
            readme = cwd / "README.md"
            if not readme.exists():
                readme.write_text(
                    "# 探针测试目录\n\n用于验证中文与空格路径下的 ACP 会话。\n",
                    encoding="utf-8",
                )
        report.add("probe.exe", exe if args.fake else exe)
        report.add("probe.cwd", str(cwd))
        report.add("probe.dsh_home", str(dsh_home))

        stderr_path = Path(args.stderr) if args.stderr else dsh_home / "runtime-stderr.log"

        if args.real_smoke and not args.fake:
            success = asyncio.run(
                real_smoke(exe, rt_args, dsh_home, cwd, args.smoke_prompt, report,
                           stderr_path, followup_text=args.smoke_followup,
                           allow_permission=args.smoke_allow_permission)
            )
            report.add("real_smoke", "PASS" if success else "FAIL",
                       "PASS" if success else "FAIL")
            rc = 0 if success else 1
        else:
            fake_cwd = REPO_ROOT if args.fake else None
            ok, unsupported = asyncio.run(
                control_plane(exe, rt_args, dsh_home, cwd, report, stderr_path,
                              initialize_timeout=args.timeout, launch_cwd=fake_cwd)
            )
            report.add("control_plane", "PASS" if ok else "FAIL", "PASS" if ok else "FAIL")
            report.add("unsupported", unsupported or "none")
            rc = 0 if ok else 1
    finally:
        for root in tmp_roots:
            shutil.rmtree(root, ignore_errors=True)

    print(report.dump())
    return rc


if __name__ == "__main__":
    sys.exit(main())
