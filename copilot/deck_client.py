"""本机数据服务的受限 GET 客户端（MCP 与会话快照共用）。

- 只读 GET，有限超时与响应长度上限；绝不 import server.py（避免第二份
  UI/采集服务或令牌生成副作用）。
- 令牌只从 QD_API_TOKEN 环境变量或既有 api-token 文件**读取**，缺失时按
  未认证访问（得到裁剪快照），不生成新令牌。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

DEFAULT_TIMEOUT = 3.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 响应上限：防无界读取


class DeckUnavailable(Exception):
    """数据服务不可达/响应异常（调用方如实报告，不伪装空数据）。"""


def read_api_token() -> Optional[str]:
    """读取既有令牌：环境变量 > api-token 文件；只读，绝不生成。"""
    env = os.environ.get("QD_API_TOKEN")
    if env:
        return env
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    token_path = Path(base) / "qoder-deck" / "api-token"
    try:
        return token_path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def get_json(base_url: str, path: str, *, timeout: float = DEFAULT_TIMEOUT,
             with_token: bool = True) -> Any:
    url = base_url.rstrip("/") + path
    if with_token:
        token = read_api_token()
        if token:
            url += ("&" if "?" in url else "?") + "token=" + urllib.parse.quote(token)
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise DeckUnavailable("响应超过长度上限")
            return json.loads(data.decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DeckUnavailable(f"{type(exc).__name__}: {exc}") from exc


def fetch_deck(base_url: str, *, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    payload = get_json(base_url, "/deck", timeout=timeout)
    if not isinstance(payload, dict) or "sessions" not in payload:
        raise DeckUnavailable("/deck 响应缺少 sessions 字段")
    return payload


def format_sessions_snapshot(deck: dict[str, Any], max_sessions: int = 12) -> str:
    """会话观察快照文本：工具/标题/状态/预览/年龄 + 采样时间。

    标明这是观察数据（不宣称已获得外部工具控制权）；未认证裁剪字段
    （标题/预览为 null）如实显示为「不可见」。
    """
    ts = deck.get("ts")
    ts_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "未知时间"
    lines = [f"[桌面会话观察快照 采样 {ts_text}（观察数据，不代表可控）]"]
    sessions = deck.get("sessions") or []
    if not sessions:
        lines.append("（当前未观察到会话）")
        return "\n".join(lines)
    for s in sessions[:max_sessions]:
        tool = s.get("tool") or "?"
        state = s.get("state") or "?"
        title = s.get("title")
        title_text = title if title else "标题不可见"
        preview = s.get("preview")
        preview_text = f"｜{preview[:40]}" if preview else ""
        age = s.get("age")
        age_text = f"｜{age}" if age is not None else ""
        lines.append(f"- {tool}｜{title_text}｜{state}{age_text}{preview_text}")
    if len(sessions) > max_sessions:
        lines.append(f"…另有 {len(sessions) - max_sessions} 条未列出")
    return "\n".join(lines)
