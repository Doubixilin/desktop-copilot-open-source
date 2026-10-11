# -*- coding: utf-8 -*-
"""dida_mcp + server today 段：fake 传输层纯逻辑（不触网）。

事实底座（2026-10-06 真机冒烟核实）：官方 MCP 应答走 structuredContent.result，
任务字段 snake_case（id/project_id/start_date/is_all_day/priority…），
complete_task 需 project_id+task_id，查询参数名 query_command。
"""
import json
import urllib.error

import pytest

import dida_mcp
import server


def make_transport(handlers, captured=None, sse=False):
    """handlers: dict 或函数，(method, tool_name|None) → (status, headers, payload|None|Exception)。"""
    def transport(url, headers, body, timeout):
        msg = json.loads(body.decode("utf-8"))
        if captured is not None:
            captured.append({"headers": dict(headers), "msg": msg})
        method = msg["method"]
        tool = ((msg.get("params") or {}).get("name")) if method == "tools/call" else None
        key = (method, tool)
        status, resp_headers, payload = handlers(key) if callable(handlers) else handlers[key]
        if isinstance(payload, Exception):
            raise payload
        resp_headers = dict(resp_headers)
        raw = b"" if payload is None else json.dumps(payload).encode("utf-8")
        if sse and raw:
            resp_headers["Content-Type"] = "text/event-stream"
            raw = b"event: message\ndata: " + raw + b"\n\n"
        return status, resp_headers, raw
    return transport


def base_handlers(today=None, nxt=None):
    return {
        ("initialize", None): (200, {"Mcp-Session-Id": "s1"}, {"jsonrpc": "2.0", "id": 1, "result": {}}),
        ("notifications/initialized", None): (202, {}, None),
        ("tools/call", "list_projects"): (200, {}, {"jsonrpc": "2.0", "id": 2, "result": {
            "content": [], "structuredContent": {"result": [
                {"id": "p1", "name": "工作"}, {"id": "inbox", "name": "Inbox"}]}}}),
        ("tools/call", "list_undone_tasks_by_time_query"): (200, {}, None),  # 占位，测试内替换
    }


def tool_result(items):
    return (200, {}, {"jsonrpc": "2.0", "id": 3, "result": {
        "content": [], "isError": False, "structuredContent": {"result": items}}})


def _task(tid, day_offset, *, all_day=True, title="任务", priority=0, project_id="inbox123"):
    import datetime
    d = datetime.date.today() + datetime.timedelta(days=day_offset)
    hm = "T15:00:00" if not all_day else "T00:00:00"
    return {"id": tid, "project_id": project_id, "title": title,
            "start_date": f"{d.isoformat()}{hm}+0800",
            "due_date": f"{d.isoformat()}{hm}+0800",
            "is_all_day": all_day, "priority": priority, "status": 0}


class TestTodaySnapshot:
    def _client(self, captured=None, sse=False):
        h = base_handlers()

        queries = {
            "last7day": [_task("t-over", -5, title="逾期合同梳理"),
                         _task("t1", 0, title="全天待办", priority=5)],
            "tomorrow": [_task("t3", 1, title="明天全天"),
                         _task("t4", 1, all_day=False, title="明天定时")],
            "next24hour": [_task("t2", 0, all_day=False, title="下午评审", project_id="p1"),
                           _task("t4", 1, all_day=False, title="明天定时")],
        }

        def handlers(key, msg_args=None):
            if key == ("tools/call", "list_undone_tasks_by_time_query"):
                return tool_result(queries[msg_args.get("query_command")])
            return h[key]

        def transport(url, headers, body, timeout):
            msg = json.loads(body.decode("utf-8"))
            if captured is not None:
                captured.append({"headers": dict(headers), "msg": msg})
            key = (msg["method"], (msg.get("params") or {}).get("name"))
            status, resp_headers, payload = handlers(
                key, (msg.get("params") or {}).get("arguments") or {})
            raw = json.dumps(payload).encode("utf-8")
            if sse:
                resp_headers = {**resp_headers, "Content-Type": "text/event-stream"}
                raw = b"event: message\ndata: " + raw + b"\n\n"
            return status, resp_headers, raw

        return dida_mcp.DidaClient("tok", transport=transport)

    def test_splits_overdue_today_tomorrow_and_events(self):
        snap = dida_mcp.today_snapshot(self._client())
        assert snap["ok"] is True
        ids = [t["id"] for t in snap["tasks"]]
        assert ids == ["t-over", "t1", "t3"]  # 逾期在前，明天全天进待办；timed 不进
        assert [e["id"] for e in snap["events"]] == ["t2", "t4"]
        assert snap["events"][0]["list"] == "工作"  # list_projects 映射
        assert snap["tasks"][1]["list"] == "Inbox"  # inbox 前缀兜底
        assert snap["tasks"][1]["priority"] == 5

    def test_query_command_arg_name(self):
        captured = []
        dida_mcp.today_snapshot(self._client(captured))
        queries = [c["msg"]["params"]["arguments"]["query_command"]
                   for c in captured if c["msg"]["method"] == "tools/call"
                   and c["msg"]["params"]["name"] == "list_undone_tasks_by_time_query"]
        assert queries == ["last7day", "tomorrow", "next24hour"]

    def test_session_id_and_bearer_echoed(self):
        captured = []
        dida_mcp.today_snapshot(self._client(captured))
        calls = [c for c in captured if c["msg"]["method"] == "tools/call"]
        assert all(c["headers"]["Mcp-Session-Id"] == "s1" for c in calls)
        assert all(c["headers"]["Authorization"] == "Bearer tok" for c in calls)

    def test_sse_response_parsed(self):
        snap = dida_mcp.today_snapshot(self._client(sse=True))
        assert snap["ok"] is True and len(snap["tasks"]) == 3

    def test_query_failure_degrades(self):
        h = base_handlers()
        h[("tools/call", "list_projects")] = tool_result([])
        h[("tools/call", "list_undone_tasks_by_time_query")] = (
            200, {}, urllib.error.HTTPError("u", 500, "x", {}, None))
        snap = dida_mcp.today_snapshot(dida_mcp.DidaClient("tok", transport=make_transport(h)))
        assert snap["ok"] is False and snap["reason"]


class TestCallToolErrors:
    def test_401_reason(self):
        h = base_handlers()
        h[("initialize", None)] = (200, {}, urllib.error.HTTPError("u", 401, "x", {}, None))
        resp = dida_mcp.DidaClient("tok", transport=make_transport(h)).call_tool("list_projects")
        assert resp["ok"] is False and "口令" in resp["reason"]

    def test_session_rebuild_on_404(self):
        h = base_handlers()
        h[("tools/call", "list_projects")] = tool_result([{"id": "p1", "name": "工作"}])
        counts = {"init": 0, "call": 0}
        base = make_transport(h)

        def transport(url, headers, body, timeout):
            msg = json.loads(body.decode("utf-8"))
            if msg["method"] == "initialize":
                counts["init"] += 1
            if msg["method"] == "tools/call":
                counts["call"] += 1
                if counts["call"] == 1:
                    raise urllib.error.HTTPError("u", 404, "x", {}, None)
            return base(url, headers, body, timeout)

        resp = dida_mcp.DidaClient("tok", transport=transport).call_tool("list_projects")
        assert resp["ok"] is True and counts["init"] == 2  # 重建握手后重试一次


class TestCompleteTask:
    def test_args_and_guard(self):
        captured = []
        h = base_handlers()
        h[("tools/call", "complete_task")] = tool_result({"id": "t1", "status": 2})
        c = dida_mcp.DidaClient("tok", transport=make_transport(h, captured))
        assert dida_mcp.complete_task(c, "inbox123", "t1") == {"ok": True}
        msg = [x["msg"] for x in captured if x["msg"]["method"] == "tools/call"][0]
        assert msg["params"]["arguments"] == {"project_id": "inbox123", "task_id": "t1"}
        assert dida_mcp.complete_task(c, "", "t1")["ok"] is False
        assert dida_mcp.complete_task(c, "p", "x" * 90)["ok"] is False


class TestServerToday:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        with server._today_lock:
            server._today_state.update(payload=None, ts=0.0, next_retry=0.0, building=False)
        yield

    def test_cache_single_flight_and_negative(self, monkeypatch):
        builds = {"n": 0}

        def fake_build():
            builds["n"] += 1
            return {"tasks": [], "events": [], "fetched_at": 1.0} if builds["n"] == 1 else None

        monkeypatch.setattr(server, "_build_today", fake_build)
        assert server.today_payload()["tasks"] == []
        assert server.today_payload()["tasks"] == []  # 缓存命中
        assert builds["n"] == 1
        # 过期后重建失败 → 负缓存期内给旧值、不重复拉取
        with server._today_lock:
            server._today_state["ts"] = 0.0
        assert server.today_payload()["tasks"] == []  # 旧值兜底
        assert server.today_payload()["tasks"] == []
        assert builds["n"] == 2

    def test_complete_invalidates_cache(self, monkeypatch):
        calls = {"n": 0}

        def fake_complete(client, pid, tid):
            calls["n"] += 1
            return {"ok": True}

        monkeypatch.setattr(server, "_build_today", lambda: {"tasks": [], "events": [], "fetched_at": 1.0})
        monkeypatch.setattr(dida_mcp, "load_token", lambda: "tok")
        monkeypatch.setattr(dida_mcp, "complete_task", fake_complete)
        server.today_payload()
        assert server.today_complete("p1", "t1") == {"ok": True}
        with server._today_lock:
            assert server._today_state["ts"] == 0.0  # 下次轮询强制重拉
        assert calls["n"] == 1

    def test_complete_without_token(self, monkeypatch):
        monkeypatch.setattr(dida_mcp, "load_token", lambda: None)
        r = server.today_complete("p1", "t1")
        assert r["ok"] is False and "口令" in r["reason"]
