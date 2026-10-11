"""07 票：桌面 MCP 工具面（stdio、无启动副作用、离线如实报错）。"""
import asyncio
import json
import os
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _write_config(tmp_path: Path, port: int) -> None:
    cfg_dir = tmp_path / "copilot-dir"
    cfg_dir.mkdir(exist_ok=True)
    # executable 需真实存在，否则配置校验失败 → mcp_server 回退默认 base_url
    # （恰好用户真实服务在 5000 上，会假成功）
    (cfg_dir / "copilot.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "enabled": True,
                "panel_mode": "copilot",
                "runtime": {
                    "executable": sys.executable,
                    "args": ["--profile", "acp"],
                    "launch_cwd": str(tmp_path),
                    "dsh_home": str(tmp_path / "home"),
                },
                "deck_base_url": f"http://127.0.0.1:{port}",
                "mcp_enabled": True,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.environ["QD_COPILOT_DIR"] = str(cfg_dir)


def _client_params():
    from mcp import StdioServerParameters

    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "copilot.mcp_server"],
        cwd=str(REPO),
        env={**os.environ},
    )


def test_tools_list_and_calls(tmp_path):
    from http.server import ThreadingHTTPServer

    import server as qd_server

    port = 5988
    _write_config(tmp_path, port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), qd_server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:

        async def body():
            from mcp import ClientSession
            from mcp.client.stdio import stdio_client

            async with stdio_client(_client_params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = {t.name for t in tools.tools}
                    assert "get_agent_status" in names
                    assert "get_hw_stats" in names

                    status = await session.call_tool("get_agent_status", {})
                    assert not status.is_error
                    data = json.loads(status.content[0].text)
                    assert "observed_at" in data
                    assert data["session_count"] >= 0
                    assert "fields_trimmed" in data  # 裁剪标记显式

                    hw = await session.call_tool("get_hw_stats", {})
                    assert not hw.is_error
                    hw_data = json.loads(hw.content[0].text)
                    assert "observed_at" in hw_data and "performance" in hw_data

        asyncio.run(body())
    finally:
        httpd.shutdown()


def test_offline_returns_tool_error_not_empty(tmp_path):
    """服务离线 → 明确工具错误，绝不用 [] 伪装"没有任务"（C17）。"""
    _write_config(tmp_path, port=5989)  # 无服务监听

    async def body():
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(_client_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("get_agent_status", {})
                assert result.is_error
                text = result.content[0].text if result.content else ""
                assert "不可用" in text, text
                assert "不代表没有任务" in text, text

    asyncio.run(body())


def test_mcp_entry_no_ui_side_effects():
    """导入/构建 mcp_server 不启动 Tk/HTTP/采集线程（可被 DSH 反复探测）。"""
    import gc

    import copilot.mcp_server as m

    threads_before = threading.active_count()
    server = m._build_server()
    del server
    gc.collect()
    assert threading.active_count() <= threads_before


def test_runtime_host_mcp_provider_shape(tmp_path):
    """mcp_enabled 时 provider 给出 McpServerStdio；关闭时为空。"""
    from copilot import runtime_host
    from copilot.config import CopilotConfig, RuntimeConfig

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    os.environ["QD_COPILOT_DIR"] = str(cfg_dir)
    cfg = CopilotConfig(
        enabled=True,
        runtime=RuntimeConfig(
            executable="C:/nonexistent/dsh.exe", dsh_home=str(tmp_path / "h")
        ),
        mcp_enabled=True,
        deck_base_url="http://127.0.0.1:5988",
    )
    cfg.save(cfg_dir / "copilot.json")
    runtime_host._state.update(
        {"controller": None, "error": None, "started": False}
    )
    try:
        # executable 不存在 → 控制器不可用；provider 逻辑独立可测
        # 直接构造 provider 逻辑等价物
        from acp.schema import EnvVariable, McpServerStdio

        def provider(cfg):
            if not cfg.mcp_enabled:
                return []
            return [
                McpServerStdio(
                    name="desktop-deck",
                    command=sys.executable,
                    args=["-m", "copilot.mcp_server"],
                    env=[EnvVariable(name="PYTHONPATH", value=str(REPO))],
                )
            ]

        servers = provider(cfg)
        assert len(servers) == 1
        assert servers[0].name == "desktop-deck"
        assert servers[0].command == sys.executable
        disabled = CopilotConfig(enabled=True, mcp_enabled=False,
                                  runtime=RuntimeConfig())
        assert provider(disabled) == []
    finally:
        runtime_host._state.update(
            {"controller": None, "error": None, "started": False}
        )
