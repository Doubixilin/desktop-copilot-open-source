"""原生 HUD 搜索适配：宿主管理的 stdio 子进程（JSON Lines）。

协议（stdin 每行一个请求，stdout 每行一个响应）：
  请求：{"id": <int>, "op": "search", "query": <str>, "limit": <int>, "offset": <int>}
        {"id": <int>, "op": "ping"}
        {"id": <int>, "op": "engine"}
  响应：{"id": <int>, "status": "ok"|"empty"|"unavailable"|"error",
        "total": <int>, "items": [...], "error": <str|null>, "engine": <str>}
  item：{"path", "name", "kind", "size_bytes", "modified_at"}

复用仓库 listary_engine 的引擎选择、响应解析与错误分类；自身不做防抖与代次
（代次/迟到丢弃由宿主与前端负责）。不读写查询日志，不启动 server.py 或真实
DSH。退出方式：宿主关闭 stdin 或终止进程；本进程不派生其他子进程。
"""

import json
import os
import sys
import threading

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import listary_engine  # noqa: E402

# 与宿主的 stdin/stdout 固定 UTF-8（Windows 控制台默认 ANSI 代码页会毁掉中文查询）
for _stream in (sys.stdin, sys.stdout):
    if hasattr(_stream, "reconfigure"):  # pytest 等环境替换的流无此方法
        _stream.reconfigure(encoding="utf-8", errors="replace")

MAX_QUERY_LEN = 256
MAX_LIMIT = 32

_write_lock = threading.Lock()


def _engine_name():
    try:
        return listary_engine._choose_engine()  # 只读探测结果，不改动其状态机语义
    except Exception:
        return ""


def _do_search(req):
    query = req["query"]
    limit = req["limit"]
    offset = req["offset"]
    base = {"id": req["id"], "total": 0, "items": [], "error": None,
            "engine": _engine_name()}
    try:
        payload = listary_engine.http_search(query, limit=limit, offset=offset)
    except OSError:
        return {**base, "status": "unavailable", "error": "engine_offline"}
    except Exception as exc:  # 引擎在线但行为异常（非法 JSON 等）
        return {**base, "status": "error", "error": f"engine_error:{type(exc).__name__}"}
    failure = listary_engine.classify_failure(payload)
    if failure == "offline":
        return {**base, "status": "unavailable", "error": "engine_offline"}
    if failure == "rate_limited":
        return {**base, "status": "unavailable", "error": "rate_limited"}
    if failure is not None:
        return {**base, "status": "error", "error": "engine_error"}
    results = listary_engine.parse_response(payload)
    items = [{
        "path": it.path,
        "name": it.name,
        "kind": it.type,
        "size_bytes": it.size_bytes,
        "modified_at": it.modified_at,
    } for it in results.items]
    return {**base, "status": "ok" if items else "empty",
            "total": results.total, "items": items}


def _validate(raw):
    """一行输入 → 请求 dict；非法输入返回 (None, 错误说明)。仅接受结构化字段。"""
    try:
        req = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None, "bad_json"
    if not isinstance(req, dict):
        return None, "bad_json"
    rid = req.get("id")
    if not isinstance(rid, int) or isinstance(rid, bool):
        return None, "bad_id"
    op = req.get("op")
    if op not in ("search", "ping", "engine"):
        return None, "bad_op"
    if op == "search":
        query = req.get("query")
        if not isinstance(query, str) or not query or len(query) > MAX_QUERY_LEN:
            return None, "bad_query"
        limit = req.get("limit", 8)
        offset = req.get("offset", 0)
        if not isinstance(limit, int) or isinstance(limit, bool):
            return None, "bad_limit"
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            return None, "bad_offset"
        req = {"id": rid, "op": op, "query": query,
               "limit": max(1, min(limit, MAX_LIMIT)), "offset": offset}
    else:
        req = {"id": rid, "op": op}
    return req, None


def _respond(resp):
    line = json.dumps(resp, ensure_ascii=False)
    with _write_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _handle(raw):
    req, err = _validate(raw)
    if req is None:
        _respond({"id": -1, "status": "error", "total": 0, "items": [],
                  "error": err, "engine": ""})
        return
    if req["op"] == "ping":
        _respond({"id": req["id"], "status": "ok", "total": 0, "items": [],
                  "error": None, "engine": _engine_name()})
        return
    if req["op"] == "engine":
        _respond({"id": req["id"], "status": "ok", "total": 0, "items": [],
                  "error": None, "engine": _engine_name()})
        return
    _respond(_do_search(req))


def main():
    workers = []
    for raw in sys.stdin:
        raw = raw.strip().lstrip("\ufeff")  # 容忍宿主端首行 BOM
        if not raw:
            continue
        # 每请求独立线程：慢请求（超时/离线探测）不阻塞后续请求
        t = threading.Thread(target=_handle, args=(raw,), daemon=True)
        t.start()
        workers.append(t)
    # stdin 关闭（宿主退出）：等在途请求写完响应，避免静默丢失
    for t in workers:
        t.join(timeout=12)


if __name__ == "__main__":
    main()
