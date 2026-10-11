"""进程内副驾运行时宿主：懒构造、全局单例。

数据服务进程内 panel 与 server 共用同一个 CopilotController 实例。
未启用/依赖缺失时返回明确原因，不抛异常、不影响既有服务启动。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from . import config as copilot_config
from .controller import CopilotController, ControllerError
from .store import CopilotStore

_lock = threading.Lock()
_state: dict = {"controller": None, "error": None, "started": False}
_hub_state: dict = {"hub": None}

REPO_ROOT = Path(__file__).resolve().parent.parent


def _mcp_command() -> str:
    """MCP 子进程用显式绝对 Python 路径（不依赖 PATH）。"""
    import sys

    return sys.executable


def get_controller() -> tuple[Optional[CopilotController], Optional[str]]:
    """返回 (controller, None) 或 (None, 面向用户的原因)。

    首次调用时读取配置并构造；配置损坏/依赖缺失只报告原因，绝不冒泡。
    """
    with _lock:
        if _state["started"]:
            return _state["controller"], _state["error"]
        _state["started"] = True
        try:
            cfg, cfg_err = copilot_config.load_config()
            reasons = copilot_config.readiness(cfg, cfg_err)
            if cfg is None or not cfg.enabled or reasons:
                _state["error"] = "；".join(reasons) or "copilot 未启用"
                return None, _state["error"]
            from . import acp_adapter  # 触发 SDK 可用性检查

            data_dir = copilot_config.default_data_dir()
            store = CopilotStore(data_dir / "copilot.sqlite3")
            stderr_dir = data_dir / "logs"

            def mcp_servers_provider() -> list:
                # G1 默认 []；mcp_enabled 时挂载只读桌面工具（07 票）
                if not cfg.mcp_enabled:
                    return []
                from acp.schema import EnvVariable, McpServerStdio

                return [
                    McpServerStdio(
                        name="desktop-deck",
                        command=_mcp_command(),
                        args=["-m", "copilot.mcp_server"],
                        env=[
                            EnvVariable(name="QD_COPILOT_DIR", value=str(data_dir)),
                            EnvVariable(name="PYTHONPATH", value=str(REPO_ROOT)),
                        ],
                    )
                ]

            controller = CopilotController(
                executable=cfg.runtime.executable,
                runtime_args=list(cfg.runtime.args),
                dsh_home=cfg.runtime.dsh_home,
                launch_cwd=cfg.runtime.launch_cwd or str(data_dir),
                store=store,
                idle_shutdown_seconds=cfg.idle_shutdown_seconds,
                initialize_timeout=cfg.initialize_timeout_seconds,
                cancel_warning_seconds=cfg.cancel_warning_seconds,
                stderr_dir=stderr_dir,
                mcp_servers_provider=mcp_servers_provider,
            )
            _state["controller"] = controller
            return controller, None
        except ImportError as exc:
            _state["error"] = f"副驾依赖未安装: {exc}"
            return None, _state["error"]
        except Exception as exc:
            _state["error"] = f"副驾初始化失败: {exc}"
            return None, _state["error"]


def get_hub():
    """统一任务中枢（一期）惰性单例：controller 旁挂 TaskHub。

    controller 不可用时返回 None；hub 构造失败不影响 controller。
    """
    controller, _err = get_controller()
    if controller is None:
        return None
    if _hub_state["hub"] is None:
        try:
            from .task_hub import TaskHub

            _hub_state["hub"] = TaskHub(
                controller, data_dir=copilot_config.default_data_dir()
            )
        except Exception:
            return None
    return _hub_state["hub"]


def panel_mode() -> str:
    """面板显示模式：copilot 配置存在且 enabled 时返回其 panel_mode，否则 auto。"""
    try:
        cfg, _ = copilot_config.load_config()
        if cfg is not None and cfg.enabled and cfg.panel_mode in copilot_config.PANEL_MODES:
            return cfg.panel_mode
    except Exception:
        pass
    return "auto"


def copilot_tab_available() -> tuple[bool, str]:
    """副驾 tab 是否显示；返回 (可用, 不可用原因)。enabled=false 不显示 tab。"""
    controller, error = get_controller()
    if controller is None:
        return False, error or "copilot 未启用"
    return True, ""


def shutdown() -> None:
    """服务退出时调用（幂等）。"""
    with _lock:
        controller = _state["controller"]
    if controller is not None:
        try:
            controller.shutdown()
        except Exception:
            pass
