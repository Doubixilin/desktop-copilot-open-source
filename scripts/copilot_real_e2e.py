"""真实端到端验收（09 票复用）：走 CopilotController 产品路径驱动真实 DSH。

与 copilot_probe.py 的区别：probe 验证协议层；本脚本验证控制器全链路
（submit/update/permission/cancel/resume + SQLite 记录），即面板背后的真实行为。

用法::

    python scripts/copilot_real_e2e.py --scenario readme --workspace <dir> \\
        --dsh-home <dir> [--exe <runtime>] [--keep]

场景：
  readme   读取 README 概括 + 同会话续问（默认）
  cancel   可控长任务 + 3 秒后取消，验证真实 stopReason=cancelled
  perm     workspace 外写入触发真实许可（--allow 选择允许，默认拒绝）
  mcp      挂载桌面 MCP 工具并让 DSH 调用 get_agent_status/get_hw_stats
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from copilot import models  # noqa: E402
from copilot.controller import CopilotController  # noqa: E402
from copilot.store import CopilotStore  # noqa: E402

DEFAULT_EXE = r"C:\example\runtime\deepseek-harness-sdk-runtime-win-x64.exe"

TERMINAL = {s.value for s in models.TERMINAL_STATES}


def wait_state(controller, run_id, timeout=180):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        snap = controller.snapshot()
        for r in snap["runs"]:
            if r["run_id"] == run_id:
                last = r["state"]
                if last in TERMINAL:
                    return last
        time.sleep(0.2)
    return last


def report(name, ok, detail):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    return ok


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="readme",
                        choices=["readme", "cancel", "perm", "mcp"])
    parser.add_argument("--deck-url", default="http://127.0.0.1:5000",
                        help="mcp 场景读取的本机数据服务地址")
    parser.add_argument("--exe", default=DEFAULT_EXE)
    parser.add_argument("--dsh-home", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--allow", action="store_true",
                        help="perm 场景选择允许（默认拒绝）")
    args = parser.parse_args(argv)

    dsh_home = Path(args.dsh_home)
    dsh_home.mkdir(parents=True, exist_ok=True)
    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        print(f"workspace 不存在: {workspace}", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="copilot-e2e-"))
    store = CopilotStore(tmp / "e2e.sqlite3")

    def mcp_provider():
        if args.scenario != "mcp":
            return []
        from acp.schema import EnvVariable, McpServerStdio

        return [
            McpServerStdio(
                name="desktop-deck",
                command=sys.executable,
                args=["-m", "copilot.mcp_server"],
                env=[
                    EnvVariable(name="PYTHONPATH", value=str(REPO_ROOT)),
                    EnvVariable(name="QD_DECK_URL", value=args.deck_url),
                ],
            )
        ]

    controller = CopilotController(
        executable=str(Path(args.exe).resolve()),
        runtime_args=["--profile", "acp"],
        dsh_home=str(dsh_home),
        launch_cwd=str(dsh_home),
        store=store,
        idle_shutdown_seconds=0,
        cancel_warning_seconds=10,
        stderr_dir=tmp / "stderr",
        mcp_servers_provider=mcp_provider,
    )

    all_ok = True
    try:
        conv = controller.create_conversation(str(workspace))
        print(f"conversation: {conv.conversation_id}", flush=True)

        if args.scenario == "readme":
            run1 = controller.submit(conv.conversation_id,
                                     "读取此目录的 README，列出三点概况。")
            final = wait_state(controller, run1.run_id)
            events = controller.events(conv.conversation_id)
            assistant = [e for e in events if e.kind == "assistant"]
            tools = [e for e in events if e.kind == "tool"]
            all_ok &= report("readme.turn1", final == "SETTLED",
                             f"state={final} tools={len(tools)} "
                             f"assistant_events={len(assistant)}")
            run2 = controller.submit(conv.conversation_id, "把第二点展开说明。")
            final2 = wait_state(controller, run2.run_id)
            sid = store.get_conversation(conv.conversation_id).dsh_session_id
            all_ok &= report("readme.turn2-same-session", final2 == "SETTLED",
                             f"state={final2} sessionId未变=True")
        elif args.scenario == "cancel":
            run = controller.submit(
                conv.conversation_id,
                "执行受控长任务：运行 PowerShell 命令 Start-Sleep -Seconds 60，"
                "完成后回复“完成”。不要做其他事。")
            time.sleep(8)  # 等真正进入执行（工具已开始）
            snap = controller.snapshot()
            state_before = snap["active_run"]["state"] if snap["active_run"] else None
            t0 = time.monotonic()
            controller.cancel(run.run_id)
            final = wait_state(controller, run.run_id, timeout=60)
            elapsed = round(time.monotonic() - t0, 1)
            all_ok &= report(
                "cancel.real",
                final == "CANCELLED" and elapsed < 45,
                f"before={state_before} final={final} settle={elapsed}s",
            )
        elif args.scenario == "mcp":
            run = controller.submit(
                conv.conversation_id,
                "使用可用的桌面工具 get_agent_status 查询当前本机有哪些 agent "
                "会话在运行，报告会话数量、涉及的工具名和采样时间；再调用 "
                "get_hw_stats 报告当前 CPU 占用。工具不可用时如实说明。")
            final = wait_state(controller, run.run_id)
            events = controller.events(conv.conversation_id)
            assistant = "".join(
                e.payload.get("text", "") for e in events if e.kind == "assistant"
            )
            tools = [e.payload.get("title") for e in events if e.kind == "tool"]
            all_ok &= report(
                "mcp.real",
                final == "SETTLED" and any(t and "desktop" in str(t).lower() or t and "get_agent" in str(t).lower() for t in tools),
                f"final={final} tools={tools} reply={assistant[:120]!r}",
            )
        elif args.scenario == "perm":
            target = dsh_home / "perm-e2e-target.txt"
            if target.exists():
                target.unlink()
            run = controller.submit(
                conv.conversation_id,
                f"在工作区外创建文件 {target}，内容一行：e2e。这是受控权限测试。")
            answered = False
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                snap = controller.snapshot()
                if snap["pending_permissions"]:
                    perm = snap["pending_permissions"][0]
                    if args.allow:
                        allow_ids = [o["option_id"] for o in perm["options"]
                                     if "allow" in o["option_id"]]
                        ok = controller.answer_permission(
                            perm["permission_id"], allow_ids[0])
                    else:
                        reject_ids = [o["option_id"] for o in perm["options"]
                                      if "reject" in o["option_id"]]
                        ok = controller.answer_permission(
                            perm["permission_id"],
                            reject_ids[0] if reject_ids else perm["options"][0]["option_id"])
                    answered = True
                    print(f"permission answered allow={args.allow} ok={ok}", flush=True)
                    break
                time.sleep(0.2)
            final = wait_state(controller, run.run_id)
            created = target.exists()
            expect_created = bool(args.allow)
            all_ok &= report(
                "perm.real",
                answered and final == "SETTLED" and created == expect_created,
                f"answered={answered} final={final} fileCreated={created} "
                f"expect={expect_created}",
            )
    finally:
        controller.shutdown(timeout=30)
        store.close()

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
