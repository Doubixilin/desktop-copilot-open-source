"""CLI agent 配置加载（copilot_agents.json）。

schema 承自蓝包桌宠 ``lanbao_agents.json``（M-A1），字段含义不变：

    {"name": "my-agent", "display": "显示名", "enabled": true,
     "program": "可执行文件", "args": ["--flag", "{prompt}"],
     "cwd": "工作目录（可选）", "env": {"K": "V"}（可选）,
     "json_lines": false, "needs_input_patterns": []}

纪律：坏条目跳过并记录原因，不整体失败；``args`` 必须恰含一个 ``{prompt}``
占位（列表传参不经 shell，``{prompt}`` 逐元素字符串替换——空格引号天然安全）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
PROMPT_PLACEHOLDER = "{prompt}"
DEFAULT_CONFIG_FILENAME = "copilot_agents.json"


class AgentConfigError(ValueError):
    """单条配置不合法（附人话原因）。"""


@dataclass
class AgentConfig:
    name: str
    program: str
    args: list[str]
    display: str = ""
    enabled: bool = True
    cwd: Optional[str] = None
    env: dict[str, str] = field(default_factory=dict)
    json_lines: bool = False
    needs_input_patterns: list[str] = field(default_factory=list)

    def build_argv(self, prompt: str) -> list[str]:
        """[program] + args，``{prompt}`` 逐元素替换（不经 shell）。"""
        return [self.program] + [a.replace(PROMPT_PLACEHOLDER, prompt) for a in self.args]


def parse_agent(entry: object, index: int) -> AgentConfig:
    if not isinstance(entry, dict):
        raise AgentConfigError(f"第 {index} 条不是对象")
    name = str(entry.get("name", "")).strip()
    if not NAME_RE.match(name):
        raise AgentConfigError(f"第 {index} 条 name 非法: {name!r}（小写字母/数字/-/_）")
    program = str(entry.get("program", "")).strip()
    if not program:
        raise AgentConfigError(f"{name}: 缺 program")
    args = entry.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise AgentConfigError(f"{name}: args 必须是字符串数组")
    if sum(1 for a in args if PROMPT_PLACEHOLDER in a) != 1:
        raise AgentConfigError(f"{name}: args 必须恰含一个 {PROMPT_PLACEHOLDER}")
    patterns = entry.get("needs_input_patterns", [])
    if not isinstance(patterns, list) or not all(
        isinstance(p, str) and p for p in patterns
    ):
        raise AgentConfigError(f"{name}: needs_input_patterns 必须是非空字符串数组")
    for p in patterns:
        try:
            re.compile(p)
        except re.error as e:
            raise AgentConfigError(f"{name}: 正则不合法 {p!r}: {e}") from e
    env = entry.get("env", {})
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        raise AgentConfigError(f"{name}: env 必须是 str->str")
    cwd = entry.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise AgentConfigError(f"{name}: cwd 必须是字符串")
    return AgentConfig(
        name=name,
        program=program,
        args=list(args),
        display=str(entry.get("display", "")).strip() or name,
        enabled=bool(entry.get("enabled", True)),
        cwd=cwd or None,
        env=dict(env),
        json_lines=bool(entry.get("json_lines", False)),
        needs_input_patterns=list(patterns),
    )


@dataclass
class AgentsFile:
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)  # 跳过原因（人话）


def load_agents(path: Path) -> AgentsFile:
    """读配置文件；文件缺失 = 空集（L0 是可选项，不强制配置）。"""
    out = AgentsFile()
    if not path.exists():
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        out.skipped.append(f"配置文件不可读: {e}")
        return out
    entries = data.get("agents") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        out.skipped.append("配置根必须是数组或 {\"agents\": [...]}")
        return out
    for i, entry in enumerate(entries):
        try:
            cfg = parse_agent(entry, i)
        except AgentConfigError as e:
            out.skipped.append(str(e))
            continue
        if not cfg.enabled:
            out.skipped.append(f"{cfg.name}: 已停用（enabled=false）")
            continue
        out.agents[cfg.name] = cfg
    return out
