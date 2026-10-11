"""Listary 引擎接入（ticket 02）：防抖、请求构造、响应解析、错误映射、退避。

纯逻辑部分不依赖 GUI 与真实网络（离线可测）；`http_search` 是唯一 I/O，
由数据服务的工作线程调用——Listary API 仅监听 127.0.0.1、只读、无
keep-alive，因此每次请求新建连接、用完即关。契约见
.scratch/listary-search/spec.md（规格源：Listary 7 应用内 HTTP API 对话框）。
"""

import http.client
import json
import os
import urllib.parse
from dataclasses import dataclass, field
from pathlib import PureWindowsPath

DEFAULT_DEBOUNCE_S = 0.2
DEFAULT_LIMIT = 8
MAX_BACKOFF_S = 5.0
OFFLINE_RETRY_S = 3.0

BASE_HOST = "127.0.0.1"
BASE_PORT = 38431
SEARCH_PATH = "/api/v1/search"
HTTP_TIMEOUT_S = 3.0

# Everything 1.5a + http_server 插件（voidtools/http_server）：GET JSON 搜索。
# 端口默认 80，QD_EVERYTHING_PORT 可改；引擎选择 QD_SEARCH_ENGINE 显式指定，
# 未指定时自动探测（Everything 可达优先，否则 Listary）——两台机器零配置各用各的。
EVERYTHING_PORT_ENV = "QD_EVERYTHING_PORT"
EVERYTHING_DEFAULT_PORT = 80
EVERYTHING_TIMEOUT_S = 2.0

_engine_choice = None
_engine_failures = 0
_env_warned = False
_REPROBE_AFTER = 5   # 连续 offline 失败达此次数后重新探测引擎（无 env 钉死时）

ERR_RATE_LIMITED = "TOO_MANY_REQUESTS"
ERR_UNAVAILABLE = "SEARCH_UNAVAILABLE"


# ---------- 纯逻辑：防抖 ----------

class Debouncer:
    """时间可注入的输入防抖：窗口内连续变更只放行最后一次，且不重发同词。"""

    def __init__(self, window_s=DEFAULT_DEBOUNCE_S):
        self.window_s = window_s
        self._pending = None   # (query, changed_at)
        self._last_sent = None

    def feed(self, query, now):
        if not query:
            # 清空即重置去重：清掉再重输同一个词必须重新搜索（审计 B03）
            self._pending = None
            self._last_sent = None
            return
        self._pending = (query, now)

    def due(self, now):
        if not self._pending:
            return None
        query, changed_at = self._pending
        # 浮点时钟差加微小容差，避免 0.30-0.10=0.199… 这类边界永不达标
        if now - changed_at + 1e-9 < self.window_s or query == self._last_sent:
            return None
        self._pending = None
        self._last_sent = query
        return query

    def feed_due(self, query):
        """立即到期且允许重发同词（引擎离线/限流重试用）。

        绕过防抖窗口，并清除同词去重记录——重试的目的就是重发同一个词。
        """
        if not query:
            return
        self._pending = (query, -1e18)
        self._last_sent = None

    def cancel(self):
        self._pending = None

    def reset(self):
        """退待机时调用：下次激活允许重发同一个词。"""
        self._pending = None
        self._last_sent = None


# ---------- 纯逻辑：请求与响应 ----------

def build_request(query, limit=DEFAULT_LIMIT, offset=0):
    return {"query": query, "limit": limit, "offset": offset}


@dataclass
class ResultItem:
    path: str
    name: str
    type: str
    size_bytes: int = 0
    modified_at: str = ""
    score: int = 0


@dataclass
class SearchResults:
    ok: bool
    total: int = 0
    items: list = field(default_factory=list)


def parse_response(payload):
    """ok 载荷 → 结果模型；异常结构一律退化为空结果而非崩溃。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return SearchResults(ok=True, total=0, items=[])
    items = []
    for row in data.get("results") or []:
        if not isinstance(row, dict):
            continue
        items.append(ResultItem(
            path=str(row.get("path", "")),
            name=str(row.get("name", "")),
            type=str(row.get("type", "file")),
            size_bytes=int(row.get("size_bytes") or 0),
            modified_at=str(row.get("modified_at") or ""),
            score=int(row.get("score") or 0),
        ))
    return SearchResults(ok=True, total=int(data.get("total") or 0), items=items)


def classify_failure(failure):
    """连接异常 / 错误载荷 → offline | rate_limited | error；ok 载荷返回 None。

    offline 只表示「引擎不可达或未就绪」（连接失败 / SEARCH_UNAVAILABLE）；
    引擎在线但行为异常（非法 JSON、未知错误码）归 error，不冒充离线。
    """
    if isinstance(failure, dict):
        if failure.get("ok"):
            return None
        err = failure.get("error")
        if err == ERR_RATE_LIMITED:
            return "rate_limited"
        if err == ERR_UNAVAILABLE:
            return "offline"
        return "error"
    if isinstance(failure, OSError):
        return "offline"
    return "error"


def backoff_delay(attempt):
    """限流退避：0.5s 起步倍增，封顶 5s。attempt 从 1 计。"""
    return min(0.5 * (2 ** (attempt - 1)), MAX_BACKOFF_S)


# ---------- 纯逻辑：结果行显示 ----------

def display_parts(path):
    """完整路径 → (名称, 父目录) 两段展示。"""
    p = PureWindowsPath(path)
    return p.name, str(p.parent)


def elide_left(s, max_chars):
    """超长时保留尾部（路径的辨识段在结尾），前缀以省略号替代。"""
    if len(s) <= max_chars:
        return s
    return "…" + s[-(max_chars - 1):]


def elide_right(s, max_chars):
    """超长时保留头部（文件名的辨识段在开头），后缀以省略号替代。"""
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1] + "…"


# ---------- 纯逻辑：键→动作与选中（ticket 03）----------

def decide_action(key, ctrl=False):
    """面板按键 → 动作：prev / next / open / reveal；其余键 None。"""
    if key == "up":
        return "prev"
    if key == "down":
        return "next"
    if key == "return":
        return "reveal" if ctrl else "open"
    return None


class SelectionModel:
    """结果选中项：首尾 clamp 不环绕；结果刷新后重置回首项；空列表无选中。"""

    def __init__(self):
        self.count = 0
        self.index = -1

    def set_count(self, n):
        self.count = n
        self.index = 0 if n else -1

    def move(self, delta):
        if self.count == 0:
            return -1
        self.index = max(0, min(self.count - 1, self.index + delta))
        return self.index


# ---------- I/O：Listary 本地 HTTP API（工作线程调用） ----------

def http_search(query, limit=DEFAULT_LIMIT, offset=0):
    """引擎接缝：按选择分发到 Listary 或 Everything，返回统一 ok 载荷。

    连接失败/超时抛 OSError；服务端错误载荷原样返回（由 classify_failure
    分类）。验收可用 QD_LISTARY_PORT 指向假端口复现引擎离线。
    连续 offline 失败达 _REPROBE_AFTER 且未被 env 钉死时清选择缓存，
    下次调用重新探测（引擎后启动/退出都能跟上）。
    """
    global _engine_failures, _engine_choice
    engine = _choose_engine()
    try:
        if engine == "everything":
            result = everything_http_search(query, limit, offset)
        else:
            result = listary_http_search(query, limit, offset)
    except OSError:
        _engine_failures += 1
        if not os.environ.get("QD_SEARCH_ENGINE") and _engine_failures >= _REPROBE_AFTER:
            _engine_failures = 0
            _engine_choice = None
        raise
    _engine_failures = 0
    return result


def listary_http_search(query, limit=DEFAULT_LIMIT, offset=0):
    """每次调用新建连接（API 无 keep-alive），返回 ok 载荷 dict。"""
    port = int(os.environ.get("QD_LISTARY_PORT", str(BASE_PORT)))
    body = json.dumps(build_request(query, limit, offset))
    conn = http.client.HTTPConnection(BASE_HOST, port, timeout=HTTP_TIMEOUT_S)
    try:
        conn.request("POST", SEARCH_PATH, body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        conn.close()
    payload = json.loads(raw.decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise ValueError(f"unexpected payload type: {type(payload)!r}")
    return payload


# ---------- 引擎选择：显式 env > 自动探测（Everything 可达优先） ----------

def _everything_reachable():
    port = int(os.environ.get(EVERYTHING_PORT_ENV, str(EVERYTHING_DEFAULT_PORT)))
    try:
        conn = http.client.HTTPConnection(BASE_HOST, port, timeout=EVERYTHING_TIMEOUT_S)
        try:
            conn.request("GET", "/?json=1&count=1&search=test")
            resp = conn.getresponse()
            resp.read()
            return resp.status == 200
        finally:
            conn.close()
    except OSError:
        return False


def _choose_engine():
    global _engine_choice, _env_warned
    env = os.environ.get("QD_SEARCH_ENGINE")
    if env:
        if env in ("listary", "everything"):
            return env
        if not _env_warned:
            _env_warned = True
            print(f"search-engine: QD_SEARCH_ENGINE={env!r} 非法"
                  "（应为 listary/everything），回退自动探测", flush=True)
    if _engine_choice is None:
        _engine_choice = "everything" if _everything_reachable() else "listary"
    return _engine_choice


# ---------- Everything：GET JSON → 统一 ok 载荷 ----------

def everything_http_search(query, limit=DEFAULT_LIMIT, offset=0):
    port = int(os.environ.get(EVERYTHING_PORT_ENV, str(EVERYTHING_DEFAULT_PORT)))
    params = urllib.parse.urlencode({
        "search": query, "json": 1, "count": limit, "offset": offset,
        "path_column": 1, "size_column": 1,
    })
    conn = http.client.HTTPConnection(BASE_HOST, port, timeout=EVERYTHING_TIMEOUT_S)
    try:
        conn.request("GET", f"/?{params}")
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        conn.close()
    return normalize_everything(json.loads(raw.decode("utf-8", errors="replace")))


def normalize_everything(payload):
    """Everything JSON → Listary 形 ok 载荷（parse_response 无改动可用）。"""
    if not isinstance(payload, dict):
        raise ValueError(f"unexpected payload type: {type(payload)!r}")
    results = []
    for row in payload.get("results") or []:
        if not isinstance(row, dict):
            continue
        path = str(row.get("path", ""))
        name = str(row.get("name", ""))
        full = f"{path}\\{name}" if path and name else path or name
        rtype = str(row.get("type", "file"))
        results.append({
            "path": full,
            "name": name,
            "type": "folder" if rtype == "folder" else "file",
            "size_bytes": int(str(row.get("size") or 0) or 0),
        })
    return {"ok": True, "data": {"total": int(payload.get("totalResults") or 0),
                                 "results": results}}
