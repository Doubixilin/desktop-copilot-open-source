"""native_hud/search_adapter.py 协议测试：错误分类、并发代次 id 匹配、EOF 不丢响应、
结构校验、路径/查询特殊字符透传。listary_engine 以桩替代，不打真实引擎。"""

import importlib.util
import io
import json
import os
import sys
import threading
import time

import pytest

import listary_engine as _real_engine  # 必须在桩替换 sys.modules 之前绑定

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTER_PATH = os.path.join(REPO_ROOT, "native_hud", "search_adapter.py")


class StubEngine:
    """listary_engine 桩：记录查询，可控载荷/异常/延迟。"""

    def __init__(self):
        self.queries = []
        self.payloads = {}   # query -> payload dict
        self.fail = None     # OSError 等异常
        self.delay = {}      # query -> seconds

    def http_search(self, query, limit=8, offset=0):
        self.queries.append(query)
        if query in self.delay:
            time.sleep(self.delay[query])
        if self.fail:
            raise self.fail
        if query in self.payloads:
            return self.payloads[query]
        return {"ok": True, "data": {"total": 0, "results": []}}

    def parse_response(self, payload):
        return _real_engine.parse_response(payload)

    def classify_failure(self, failure):
        return _real_engine.classify_failure(failure)

    def _choose_engine(self):
        return "stub"


@pytest.fixture()
def adapter(monkeypatch):
    stub = StubEngine()
    monkeypatch.setitem(sys.modules, "listary_engine", stub)
    spec = importlib.util.spec_from_file_location("search_adapter_under_test", ADAPTER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, stub


def run_adapter(mod, lines):
    """以给定输入行驱动 main()，返回解析后的响应列表。"""
    out = io.StringIO()
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin = io.StringIO("\n".join(lines) + "\n")
    sys.stdout = out
    try:
        mod.main()
    finally:
        sys.stdin, sys.stdout = old_in, old_out
    return [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]


def by_id(resps, rid):
    return next(r for r in resps if r["id"] == rid)


def test_ok_and_empty(adapter):
    mod, stub = adapter
    stub.payloads["alpha"] = {"ok": True, "data": {"total": 1, "results": [
        {"path": "D:\\t\\a b.txt", "name": "a b.txt", "type": "file", "size_bytes": 3}]}}
    resps = run_adapter(mod, [
        json.dumps({"id": 1, "op": "search", "query": "alpha"}),
        json.dumps({"id": 2, "op": "search", "query": "nothing"}),
    ])
    r1 = by_id(resps, 1)
    assert r1["status"] == "ok" and r1["total"] == 1
    assert r1["items"][0]["path"] == "D:\\t\\a b.txt"
    assert by_id(resps, 2)["status"] == "empty"


def test_error_classification(adapter):
    mod, stub = adapter
    stub.payloads["unavail"] = {"ok": False, "error": "SEARCH_UNAVAILABLE"}
    stub.payloads["limited"] = {"ok": False, "error": "TOO_MANY_REQUESTS"}
    stub.payloads["weird"] = {"ok": False, "error": "SOMETHING_ELSE"}
    resps = run_adapter(mod, [
        json.dumps({"id": 1, "op": "search", "query": "unavail"}),
        json.dumps({"id": 2, "op": "search", "query": "limited"}),
        json.dumps({"id": 3, "op": "search", "query": "weird"}),
    ])
    assert by_id(resps, 1)["status"] == "unavailable"
    assert by_id(resps, 2)["status"] == "unavailable"
    assert by_id(resps, 2)["error"] == "rate_limited"
    assert by_id(resps, 3)["status"] == "error"


def test_oserror_is_unavailable(adapter):
    mod, stub = adapter
    stub.fail = OSError("connection refused")
    resps = run_adapter(mod, [json.dumps({"id": 9, "op": "search", "query": "x"})])
    assert by_id(resps, 9)["status"] == "unavailable"
    assert by_id(resps, 9)["error"] == "engine_offline"


def test_malformed_inputs(adapter):
    mod, _ = adapter
    resps = run_adapter(mod, [
        "not json",
        json.dumps(["array"]),
        json.dumps({"id": "x", "op": "search", "query": "a"}),
        json.dumps({"id": 1, "op": "eval", "query": "a"}),
        json.dumps({"id": 2, "op": "search", "query": ""}),
        json.dumps({"id": 3, "op": "search", "query": "a" * 300}),
    ])
    assert all(r["status"] == "error" and r["id"] == -1 for r in resps)
    assert len(resps) == 6


def test_query_and_path_special_chars_passthrough(adapter):
    mod, stub = adapter
    tricky = "带空格 & 引号' <b>&amp;</b>"
    stub.payloads[tricky] = {"ok": True, "data": {"total": 1, "results": [
        {"path": "D:\\t\\带 & ' <b>.txt", "name": "带 & ' <b>.txt",
         "type": "file", "size_bytes": 1}]}}
    resps = run_adapter(mod, [json.dumps({"id": 5, "op": "search", "query": tricky})])
    r = by_id(resps, 5)
    assert stub.queries == [tricky]                     # 查询原样到达引擎
    assert r["items"][0]["path"] == "D:\\t\\带 & ' <b>.txt"  # 路径不转义不损坏
    assert r["status"] == "ok"


def test_limit_clamped(adapter):
    mod, stub = adapter
    captured = {}

    def spy(query, limit=8, offset=0):
        captured["limit"] = limit
        return {"ok": True, "data": {"total": 0, "results": []}}

    stub.http_search = spy
    run_adapter(mod, [json.dumps({"id": 1, "op": "search", "query": "a", "limit": 500})])
    assert captured["limit"] == 32


def test_out_of_order_responses_keep_ids(adapter):
    """慢查询先发出、快查询后发出；响应乱序但 id 对应正确，且 EOF 不丢响应。"""
    mod, stub = adapter
    stub.delay["slow"] = 0.6
    stub.payloads["slow"] = {"ok": True, "data": {"total": 1, "results": [
        {"path": "D:\\t\\slow.txt", "name": "slow.txt", "type": "file"}]}}
    stub.payloads["fast"] = {"ok": True, "data": {"total": 1, "results": [
        {"path": "D:\\t\\fast.txt", "name": "fast.txt", "type": "file"}]}}
    resps = run_adapter(mod, [
        json.dumps({"id": 1, "op": "search", "query": "slow"}),
        json.dumps({"id": 2, "op": "search", "query": "fast"}),
    ])
    assert by_id(resps, 1)["items"][0]["name"] == "slow.txt"
    assert by_id(resps, 2)["items"][0]["name"] == "fast.txt"
    assert len(resps) == 2  # EOF 时两个响应都写出


def test_ping_and_engine(adapter):
    mod, _ = adapter
    resps = run_adapter(mod, [
        json.dumps({"id": 1, "op": "ping"}),
        json.dumps({"id": 2, "op": "engine"}),
    ])
    assert by_id(resps, 1)["status"] == "ok"
    assert by_id(resps, 2)["engine"] == "stub"


def test_concurrent_writers_single_line_json(adapter):
    """多请求并发响应：每行都是完整可解析 JSON（写锁生效）。"""
    mod, stub = adapter
    for i in range(20):
        stub.payloads[f"q{i}"] = {"ok": True, "data": {"total": 0, "results": []}}
    lines = [json.dumps({"id": i, "op": "search", "query": f"q{i}"}) for i in range(20)]
    resps = run_adapter(mod, lines)
    assert sorted(r["id"] for r in resps) == list(range(20))
