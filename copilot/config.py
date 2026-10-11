"""副驾配置与就绪诊断。

导入本模块不启动任何进程、不连接网络。配置目录默认
``%LOCALAPPDATA%\\qoder-deck\\copilot``，可用 ``QD_COPILOT_DIR``
覆盖（开发/测试用独立副本）。仓库只提交示例 ``copilot.example.json``，
不提交真实配置、任务库或凭据。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 1

# 面板显示模式：auto=沿用经典壁纸显隐；copilot=宿主无关常驻；hidden=不显示
PANEL_MODES = ("auto", "copilot", "hidden")


def default_data_dir() -> Path:
    override = os.environ.get("QD_COPILOT_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "qoder-deck" / "copilot"


@dataclass
class RuntimeConfig:
    """DSH ACP 子进程的启动描述。executable 必须是绝对路径。"""

    executable: str = ""
    args: list[str] = field(default_factory=lambda: ["--profile", "acp"])
    launch_cwd: str = ""
    dsh_home: str = ""

    def resolved_env(self) -> dict[str, str]:
        """子进程环境：仅注入显式配置的 DSH_HOME，不携带其余覆盖。"""
        env = dict(os.environ)
        if self.dsh_home:
            env["DSH_HOME"] = str(Path(self.dsh_home))
        return env


@dataclass
class CopilotConfig:
    schema_version: int = SCHEMA_VERSION
    enabled: bool = False
    panel_mode: str = "copilot"
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    default_workspace: Optional[str] = None
    idle_shutdown_seconds: int = 600
    initialize_timeout_seconds: int = 30
    cancel_warning_seconds: int = 10
    deck_base_url: str = "http://127.0.0.1:5000"
    mcp_enabled: bool = False

    # ---- 序列化 ----
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["runtime"] = asdict(self.runtime)
        return data

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> "CopilotConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        runtime_raw = raw.pop("runtime", {}) or {}
        cfg = cls(**{k: v for k, v in raw.items() if k in _CONFIG_FIELDS})
        if cfg.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"copilot config schema_version {cfg.schema_version} != {SCHEMA_VERSION}"
            )
        if cfg.panel_mode not in PANEL_MODES:
            raise ValueError(f"panel_mode must be one of {PANEL_MODES}")
        cfg.runtime = RuntimeConfig(
            **{k: v for k, v in runtime_raw.items() if k in _RUNTIME_FIELDS}
        )
        return cfg

    # ---- 校验 ----
    def validation_errors(self) -> list[str]:
        errors: list[str] = []
        if self.panel_mode not in PANEL_MODES:
            errors.append(f"panel_mode 非法: {self.panel_mode}")
        rt = self.runtime
        if rt.executable:
            exe = Path(rt.executable)
            if not exe.is_absolute():
                errors.append("runtime.executable 必须是绝对路径")
            elif not exe.is_file():
                errors.append(f"runtime.executable 不存在: {rt.executable}")
        if rt.dsh_home and not Path(rt.dsh_home).is_absolute():
            errors.append("runtime.dsh_home 必须是绝对路径")
        if rt.launch_cwd and not Path(rt.launch_cwd).is_absolute():
            errors.append("runtime.launch_cwd 必须是绝对路径")
        if self.default_workspace and not Path(self.default_workspace).is_absolute():
            errors.append("default_workspace 必须是绝对路径")
        return errors


_CONFIG_FIELDS = {f for f in CopilotConfig.__dataclass_fields__ if f != "runtime"}
_RUNTIME_FIELDS = set(RuntimeConfig.__dataclass_fields__)


def config_path(data_dir: Path | None = None) -> Path:
    return (data_dir or default_data_dir()) / "copilot.json"


def load_config(data_dir: Path | None = None) -> tuple[Optional[CopilotConfig], Optional[str]]:
    """读取配置；不存在时返回 (None, None)。解析/校验失败返回 (None, 原因)。"""
    path = config_path(data_dir)
    if not path.is_file():
        return None, None
    try:
        cfg = CopilotConfig.load(path)
    except Exception as exc:  # 配置损坏不应阻断主服务
        return None, f"copilot.json 解析失败: {exc}"
    errors = cfg.validation_errors()
    if errors:
        return None, "copilot.json 校验失败: " + "; ".join(errors)
    return cfg, None


def sdk_available() -> tuple[bool, str]:
    """检查可选依赖 agent-client-protocol 是否可导入；返回 (可用, 版本或原因)。"""
    try:
        import acp  # noqa: F401
        from importlib.metadata import version
        return True, version("agent-client-protocol")
    except ImportError as exc:
        return False, f"agent-client-protocol 未安装: {exc}"
    except Exception as exc:
        return False, str(exc)


def readiness(cfg: Optional[CopilotConfig], cfg_error: Optional[str]) -> list[str]:
    """诊断副驾为何未就绪；返回面向用户的简短原因列表（不含秘密）。"""
    reasons: list[str] = []
    if cfg is None:
        reasons.append(cfg_error or "copilot.json 不存在（未配置）")
        return reasons
    if not cfg.enabled:
        reasons.append("copilot.enabled=false")
    if not cfg.runtime.executable:
        reasons.append("runtime.executable 未配置")
    ok, info = sdk_available()
    if not ok:
        reasons.append(info)
    reasons.extend(cfg.validation_errors())
    return reasons
