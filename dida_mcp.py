"""滴答清单官方 MCP 客户端（最小 Streamable HTTP 实现，stdlib 无新依赖）。

事实底座（官方文档核实，2026-10）：
- 端点 https://mcp.dida365.com，仅 Streamable HTTP（不支持 SSE 传输）；
  授权用 Bearer Token（网页版 头像→设置→账户与安全→API 口令）。
- 应答 Content-Type 可能是 application/json 或 text/event-stream（SSE 帧）；
  initialize 应答可能带 Mcp-Session-Id 头，后续请求须回显。

红线：token 是凭证——存 %LOCALAPPDATA%\\qoder-deck\\dida.json（不进 git、
不进日志）；一切失败静默降级（返回 ok=False + 人话 reason），不拖垮宿主。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

ENDPOINT = "https://mcp.dida365.com"
PROTOCOL_VERSION = "2025-03-26"
CLIENT_INFO = {"name": "agent-deck", "version": "1.0.0"}
DEFAULT_TIMEOUT = 15.0

_TOKEN_ENV = "QD_DIDA_TOKEN"


def _token_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "qoder-deck", "dida.json")


def load_token() -> str | None:
    """环境变量优先（诊断用），否则 %LOCALAPPDATA%\\qoder-deck\\dida.json。"""
    tok = (os.environ.get(_TOKEN_ENV) or "").strip()
    if tok:
        return tok
    try:
        with open(_token_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        tok = str(data.get("token", "")).strip()
        return tok or None
    except (OSError, ValueError):
        return None


def _http_post(url: str, headers: dict, body: bytes, timeout: float):
    """可注入的传输层（测试替换）。返回 (status, headers, body_bytes)。"""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, dict(resp.headers), resp.read()


def _parse_payload(status: int, headers: dict, raw: bytes) -> dict:
    """Streamable HTTP 应答 → 第一个 JSON-RPC 消息（SSE 帧取 data 行拼接）。"""
    ctype = ""
    for k, v in (headers or {}).items():
        if k.lower() == "content-type":
            ctype = v.lower()
            break
    text = raw.decode("utf-8", errors="replace")
    if "text/event-stream" in ctype:
        for block in text.split("\n\n"):
            data = "".join(
                line[5:].lstrip() for line in block.splitlines()
                if line.startswith("data:")
            )
            if data.strip():
                return json.loads(data)
        raise ValueError("SSE 应答无 data 帧")
    return json.loads(text)


class DidaClient:
    """会话级 MCP 客户端：initialize 一次，握手失败/会话失效自动重建一次。"""

    def __init__(self, token: str, *, endpoint: str = ENDPOINT,
                 timeout: float = DEFAULT_TIMEOUT, transport=_http_post):
        self._token = token
        self._endpoint = endpoint
        self._timeout = timeout
        self._transport = transport
        self._session_id: str | None = None
        self._next_id = 0

    def _rpc(self, method: str, params: dict | None, *, with_id: bool = True) -> dict | None:
        self._next_id += 1
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if with_id:
            msg["id"] = self._next_id
        if params is not None:
            msg["params"] = params
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self._token}",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        status, resp_headers, raw = self._transport(
            self._endpoint, headers, json.dumps(msg).encode("utf-8"), self._timeout)
        sid = None
        for k, v in (resp_headers or {}).items():
            if k.lower() == "mcp-session-id":
                sid = v
                break
        if sid:
            self._session_id = sid
        if status == 202 or not raw:
            return None  # 通知类应答
        return _parse_payload(status, resp_headers, raw)

    def _initialize(self) -> None:
        resp = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        })
        if not isinstance(resp, dict) or "result" not in resp:
            raise RuntimeError("MCP 握手失败")
        self._rpc("notifications/initialized", None, with_id=False)

    def call_tool(self, name: str, arguments: dict | None = None,
                  *, _retried: bool = False) -> dict:
        """tools/call → {"ok": True, "content": [...]} 或 {"ok": False, "reason": 人话}。"""
        try:
            if self._session_id is None:
                self._initialize()
            resp = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return {"ok": False, "reason": "API 口令无效或已失效（网页版可重新生成）"}
            if e.code in (404, 400) and not _retried:
                # 会话失效：重建握手重试一次
                self._session_id = None
                return self.call_tool(name, arguments, _retried=True)
            return {"ok": False, "reason": f"MCP 服务返回 HTTP {e.code}"}
        except (urllib.error.URLError, TimeoutError, OSError):
            return {"ok": False, "reason": "MCP 服务不可达（网络/超时）"}
        except (ValueError, RuntimeError) as e:
            return {"ok": False, "reason": f"MCP 应答异常（{e}）"}
        if not isinstance(resp, dict):
            return {"ok": False, "reason": "MCP 应答异常（空）"}
        if "error" in resp:
            err = resp["error"] if isinstance(resp["error"], dict) else {}
            return {"ok": False, "reason": f"工具调用被拒（{err.get('message', '未知')}）"}
        result = resp.get("result") or {}
        if result.get("isError"):
            return {"ok": False, "reason": "工具执行失败"}
        return {"ok": True, "content": result.get("content") or [],
                "structured": result.get("structuredContent")}


def tool_json(resp: dict):
    """tools/call 应答 → 结构化数据：优先 structuredContent（官方服务走这里），
    退回 content[0].text 内嵌 JSON；再退直返文本。"""
    if not resp.get("ok"):
        return None
    structured = resp.get("structured")
    if structured is not None:
        return structured.get("result") if isinstance(structured, dict) and "result" in structured else structured
    for item in resp.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text") or ""
            try:
                return json.loads(text)
            except ValueError:
                return text
    return None


# ---- TODAY 块语义（server.py 消费；字段名以官方真实应答为准：snake_case） ----

def _norm_task(raw: dict, project_names: dict | None = None) -> dict:
    """MCP 任务对象 → TODAY 条目。"""
    pid = str(raw.get("project_id") or "")
    list_name = (project_names or {}).get(pid, "")
    if not list_name and pid.startswith("inbox"):
        list_name = "Inbox"
    return {
        "id": str(raw.get("id") or ""),
        "project_id": pid,
        "title": str(raw.get("title") or "").strip(),
        "list": list_name,
        "start": str(raw.get("start_date") or ""),
        "due": str(raw.get("due_date") or ""),
        "all_day": bool(raw.get("is_all_day")),
        "priority": int(raw.get("priority") or 0),
    }


def _query(client: DidaClient, command: str) -> tuple[list | None, str]:
    resp = client.call_tool("list_undone_tasks_by_time_query",
                            {"query_command": command, "client_timezone": "Asia/Shanghai"})
    if not resp.get("ok"):
        return None, resp.get("reason", "查询失败")
    data = tool_json(resp)
    return (data if isinstance(data, list) else []), ""


def today_snapshot(client: DidaClient) -> dict:
    """今日待办（含逾期）+ 未来 24h 日程（timed=日程 / 全天或无时间=待办）。

    真机核实（2026-10-06）：官方 MCP 的 today 查询**不含已过期任务**（与 App
    "今天"智能清单语义不同）——待办须用 last7day 捞逾期段 + tomorrow 捞明天段，
    按 id 去重。任一查询失败 → ok=False（调用方静默降级）。
    """
    out = {"ok": False, "reason": "", "tasks": [], "events": [],
           "fetched_at": time.time()}
    # 清单 id → 名称（失败容忍：清单名只是展示增强）
    project_names: dict = {}
    presp = client.call_tool("list_projects", {})
    pdata = tool_json(presp) if presp.get("ok") else None
    if isinstance(pdata, list):
        project_names = {str(p.get("id")): str(p.get("name") or "")
                         for p in pdata if isinstance(p, dict) and p.get("id")}

    today_str = time.strftime("%Y-%m-%d")
    last7, reason = _query(client, "last7day")
    if last7 is None:
        out["reason"] = reason
        return out
    tomorrow, reason = _query(client, "tomorrow")
    if tomorrow is None:
        out["reason"] = reason
        return out
    nxt, reason = _query(client, "next24hour")
    if nxt is None:
        out["reason"] = reason
        return out

    norm = lambda items: [_norm_task(t, project_names) for t in items
                          if isinstance(t, dict) and t.get("title")]
    day_of = lambda t: (t["start"] or "")[:10]
    seen: set = set()
    tasks: list = []
    # 逾期 + 今天：last7day 中日期 ≤ 今天的全部
    for t in norm(last7):
        if t["id"] in seen or (day_of(t) and day_of(t) > today_str):
            continue
        seen.add(t["id"])
        tasks.append(t)
    # 明天的全天任务进待办（标"明天"由页面按日期渲染）；timed 的由日程段覆盖
    for t in norm(tomorrow):
        if t["id"] in seen or not t["all_day"]:
            continue
        seen.add(t["id"])
        tasks.append(t)
    tasks.sort(key=lambda t: (day_of(t) or "9999", -t["priority"]))
    out["tasks"] = tasks
    out["events"] = sorted(
        (t for t in norm(nxt) if not t["all_day"] and t["start"]),
        key=lambda t: t["start"])
    out["ok"] = True
    return out


def complete_task(client: DidaClient, project_id: str, task_id: str) -> dict:
    """勾选写回。project_id/task_id 只接受 today_snapshot 出境的标识。"""
    if not task_id or len(task_id) > 80 or not project_id or len(project_id) > 80:
        return {"ok": False, "reason": "bad_task_id"}
    resp = client.call_tool("complete_task",
                            {"project_id": project_id, "task_id": task_id})
    if not resp.get("ok"):
        return {"ok": False, "reason": resp.get("reason", "写回失败")}
    return {"ok": True}
