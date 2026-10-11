"""桌面只读工具面：DSH 可调用的最小 MCP（07 票，stdio）。

入口 ``python -m copilot.mcp_server``：
- stdout 只有 MCP 协议帧；诊断一律走 stderr；
- 无 Tk/HTTP server/图标编排启动副作用（DSH 探测可反复启停本进程）；
- 数据经现有本机 GET 服务读取（deck_client），令牌只读不生成；
- 工具返回带 observed_at 采样时间与来源/裁剪标记；服务离线抛明确
  工具错误，不用空数组伪装"没有任务"。
"""
from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

from . import deck_client
from .config import load_config


def _base_url() -> str:
    import os

    override = os.environ.get("QD_DECK_URL")
    if override:
        return override
    cfg, _ = load_config()
    return cfg.deck_base_url if cfg else "http://127.0.0.1:5000"


def _build_server():
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("desktop-deck")

    @server.tool(
        name="get_agent_status",
        title="桌面 Agent 会话状态",
        description=(
            "观察本机各编码工具的会话状态（工具/标题/状态/预览/年龄）"
            "与桌面副驾派出的 DSH 任务。返回为观察快照：带采样时间，"
            "未知或被令牌裁剪的字段会显式标注；这是观察数据，不代表"
            "可控制这些工具。"
        ),
    )
    def get_agent_status() -> dict[str, Any]:
        from mcp.server.mcpserver.exceptions import ToolError

        base = _base_url()
        try:
            deck = deck_client.fetch_deck(base)
        except deck_client.DeckUnavailable as exc:
            # 明确工具错误（普通异常会被 SDK 脱敏成通用文案）；
            # 绝不用空数组伪装"没有任务"
            raise ToolError(
                f"agent-deck 数据服务不可用（{base}）：{exc}。"
                "本工具只读取快照，无法确认任务状态，不代表没有任务。"
            ) from exc
        sessions = deck.get("sessions") or []
        trimmed = any(s.get("title") is None for s in sessions)
        return {
            "observed_at": deck.get("ts"),
            "observed_at_text": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(deck.get("ts") or time.time())
            ),
            "source": "agent-deck /deck",
            "session_count": len(sessions),
            "fields_trimmed": trimmed,  # 未认证访问时标题/预览被裁剪
            "sessions": [
                {
                    "tool": s.get("tool"),
                    "title": s.get("title"),
                    "state": s.get("state"),
                    "run_state": s.get("run_state"),  # 副驾细态（如无则为 None）
                    "age_seconds": s.get("age"),
                    "preview": s.get("preview"),
                    "source": s.get("source", "scanner"),
                }
                for s in sessions
            ],
            "note": "标题/预览为 null 且 fields_trimmed=true 时表示本机服务"
                    "按未认证访问裁剪了敏感字段，不代表字段为空。",
        }

    @server.tool(
        name="get_hw_stats",
        title="本机性能快照",
        description="CPU/内存/网络等本机性能快照（采样时间随返回给出）。",
    )
    def get_hw_stats() -> dict[str, Any]:
        from mcp.server.mcpserver.exceptions import ToolError

        base = _base_url()
        try:
            perf = deck_client.get_json(base, "/performance")
        except deck_client.DeckUnavailable as exc:
            raise ToolError(f"agent-deck 数据服务不可用（{base}）：{exc}") from exc
        return {
            "observed_at": time.time(),
            "observed_at_text": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "agent-deck /performance",
            "performance": perf,
        }

    @server.tool(
        name="context_search",
        title="检索任务档案",
        description=(
            "在已归标的任务档案中按关键词检索（匹配用户请求、助手结论或会话"
            "标题）。返回 run_id/摘要/完整性判定。注意：空结果不代表相关任务"
            "不存在——未归档的在途/新任务不在此列。"
        ),
    )
    def context_search(query: str, limit: int = 10) -> dict[str, Any]:
        return _archive_call("context_search", query=query, limit=limit)

    @server.tool(
        name="context_get",
        title="读取档案任务全记录",
        description=(
            "按 run_id 读取一条已归档任务的自包含记录：用户请求、助手输出"
            "（预算内）、状态、完整性判定、现场包关联与人审反馈。"
        ),
    )
    def context_get(run_id: str) -> dict[str, Any]:
        from mcp.server.mcpserver.exceptions import ToolError

        out = _archive_call("context_get", run_id=run_id)
        if "error" in out:
            raise ToolError(out["error"]["message"])
        return out

    @server.tool(
        name="context_evidence",
        title="读取档案任务证据",
        description=(
            "按 run_id 读取任务的证据面：现场材料（种类/出处/哈希/摘要）、"
            "工具调用清单与完整性判定——用于核对结论依据，而不是重读全文。"
        ),
    )
    def context_evidence(run_id: str) -> dict[str, Any]:
        from mcp.server.mcpserver.exceptions import ToolError

        out = _archive_call("context_evidence", run_id=run_id)
        if "error" in out:
            raise ToolError(out["error"]["message"])
        return out

    return server


def _archive_dir():
    """档案目录：QD_COPILOT_DIR（runtime_host 注入）> 配置默认。"""
    import os

    from . import config as copilot_config
    from pathlib import Path

    env = os.environ.get("QD_COPILOT_DIR")
    if env:
        return Path(env)
    return copilot_config.default_data_dir()


def _archive_call(func: str, **kwargs) -> dict[str, Any]:
    from mcp.server.mcpserver.exceptions import ToolError

    from . import archive

    try:
        return getattr(archive, func)(_archive_dir(), **kwargs)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 档案读失败给明确错误，不装空
        raise ToolError(f"任务档案读取失败：{exc}") from exc


def main(argv: list[str] | None = None) -> int:
    server = _build_server()
    try:
        asyncio.run(server.run_stdio_async())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
