"""quota / usage 接缝测试：fixture 数据 + mock HTTP，不碰真实网络与真实 key。"""
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import quota
import usage


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    t = time.time()
    os.utime(path, (t, t))


def _codex_file(root, rel, lines):
    jf = Path(root) / "sessions" / rel
    _write(jf, "\n".join(lines) + "\n")
    return jf


class CodexQuotaTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_snapshot_parsed_from_tail(self):
        snap = {"rateLimits": {"primary": {
            "usedPercent": 19, "windowDurationMins": 10080, "resetsAt": 1790662039}}}
        outer = {"type": "response_item", "payload": {
            "type": "custom_tool_call_output", "output": [
                {"type": "input_text",
                 "text": "Output:\n" + json.dumps(snap).replace('"', '\\"') + "\n"}]}}
        _codex_file(self.root, "2026/09/25/rollout-a.jsonl", [json.dumps(outer)])
        got = quota.codex_quota(self.root)
        self.assertEqual(got["window"], "weekly")
        self.assertEqual(got["remaining_pct"], 81)
        self.assertEqual(got["resets_at"], 1790662039)

    def test_5h_window_label(self):
        snap = '{"usedPercent":40,"windowDurationMins":300,"resetsAt":1790662039}'
        _codex_file(self.root, "2026/09/25/rollout-b.jsonl", ['x' + snap])
        self.assertEqual(quota.codex_quota(self.root)["window"], "5h")

    def test_no_snapshot_yields_none(self):
        _codex_file(self.root, "2026/09/25/rollout-c.jsonl", ['{"type":"response_item"}'])
        self.assertIsNone(quota.codex_quota(self.root))

    def test_latest_snapshot_wins(self):
        _codex_file(self.root, "2026/09/25/rollout-d.jsonl", [
            '{"usedPercent":10,"windowDurationMins":10080,"resetsAt":1}',
            '{"usedPercent":25,"windowDurationMins":10080,"resetsAt":2}',
        ])
        self.assertEqual(quota.codex_quota(self.root)["remaining_pct"], 75)

    def test_empty_root_none(self):
        self.assertIsNone(quota.codex_quota(Path(self._tmp.name) / "nope"))


class BalanceTest(unittest.TestCase):
    def test_balance_parsed(self):
        orig_get, orig_key = quota._get_json, quota._dsh_key
        quota._get_json = lambda url, token, timeout=4: {
            "is_available": True,
            "balance_infos": [{"currency": "CNY", "total_balance": "12.34"}],
        }
        quota._dsh_key = lambda: "sk-test"
        try:
            got = quota.dsh_balance()
        finally:
            quota._get_json, quota._dsh_key = orig_get, orig_key
        self.assertEqual(got["window"], "balance")
        self.assertEqual(got["detail"]["total_balance"], "12.34")

    def test_http_failure_silent_none(self):
        def boom(url, token, timeout=4):
            raise OSError("net down")
        orig = quota._get_json
        quota._get_json = boom
        try:
            self.assertIsNone(quota.dsh_balance())
        finally:
            quota._get_json = orig

    def test_no_key_none(self):
        env = os.environ.pop("QD_DEEPSEEK_API_KEY", None)
        home = Path.home
        try:
            Path.home = lambda: Path(tempfile.gettempdir())  # 无 .credentials.yaml
            self.assertIsNone(quota.dsh_balance())
        finally:
            Path.home = home
            if env:
                os.environ["QD_DEEPSEEK_API_KEY"] = env


class SubscriptionTest(unittest.TestCase):
    def test_unconfigured_none(self):
        orig = quota._subscription_config
        quota._subscription_config = lambda: None
        try:
            self.assertIsNone(quota.dsh_subscription())
        finally:
            quota._subscription_config = orig

    def test_configured_percent_and_fraction_unit(self):
        # 单位由字段名/remaining_unit 显式声明（复核 F03 后的契约）
        orig_cfg, orig_get, orig_key = quota._subscription_config, quota._get_json, quota._dsh_key
        quota._subscription_config = lambda: {"url": "https://x/quota", "window": "5h"}
        quota._get_json = lambda url, token, timeout=4: {"remaining_pct": 62}
        quota._dsh_key = lambda: "sk-test"
        try:
            self.assertEqual(quota.dsh_subscription()["remaining_pct"], 62.0)
            quota._subscription_config = lambda: {
                "url": "https://x/quota", "window": "5h",
                "json_path_remaining": "quota.left", "remaining_unit": "fraction"}
            quota._get_json = lambda url, token, timeout=4: {"quota": {"left": 0.35}}
            self.assertEqual(quota.dsh_subscription()["remaining_pct"], 35.0)
        finally:
            quota._subscription_config, quota._get_json, quota._dsh_key = orig_cfg, orig_get, orig_key


class ExtractPctContractTest(unittest.TestCase):
    """B06+F03：单位由字段名/配置显式声明，禁止按数值大小猜测。

    *_pct/*_percent = 百分数（0.5 就是 0.5%）；*_fraction = 比例（×100）；
    裸 remaining/used/left 单位不可知 → None；越界/非有限值 → None。
    """

    def test_percent_keys_mean_percent(self):
        # 复核 F03 反例：remaining_pct=0.5 是剩余 0.5%，不是 50%
        self.assertEqual(quota._extract_pct({"remaining_pct": 0.5}, None), 0.5)
        self.assertEqual(quota._extract_pct({"remaining_pct": 62}, None), 62.0)
        self.assertEqual(quota._extract_pct({"left_pct": 0.5}, None), 0.5)

    def test_used_percent_keys(self):
        for used, want in ((98, 2.0), (99, 1.0), (100, 0.0), (0.5, 99.5), (0, 100.0)):
            self.assertEqual(quota._extract_pct({"usedPercent": used}, None), want)
            self.assertEqual(quota._extract_pct({"used_percent": used}, None), want)

    def test_fraction_keys_are_scaled(self):
        self.assertEqual(quota._extract_pct({"remaining_fraction": 0.35}, None), 35.0)
        self.assertEqual(quota._extract_pct({"remaining_fraction": 1.0}, None), 100.0)
        self.assertEqual(quota._extract_pct({"used_fraction": 0.99}, None), 1.0)

    def test_ambiguous_bare_keys_are_unknown(self):
        for payload in ({"remaining": 0.35}, {"remaining": 62}, {"left": 1}, {"used": 99}):
            self.assertIsNone(quota._extract_pct(payload, None))

    def test_path_unit_from_config(self):
        data = {"quota": {"left": 0.5}}
        self.assertEqual(quota._extract_pct(data, "quota.left", unit="fraction"), 50.0)
        self.assertEqual(quota._extract_pct(data, "quota.left"), 0.5)  # 默认 percent
        self.assertEqual(quota._extract_pct(data, "quota.left", unit="percent"), 0.5)

    def test_out_of_range_and_nonfinite_are_unknown(self):
        for bad in (-5, 101, float("nan"), float("inf")):
            self.assertIsNone(quota._extract_pct({"remaining_pct": bad}, None))
        self.assertIsNone(quota._extract_pct({"remaining_pct": True}, None))
        self.assertIsNone(quota._extract_pct({"remaining_pct": "62"}, None))


class QuotaSingleFlightTest(unittest.TestCase):
    """B10：缓存过期瞬间的并发调用只触发一次真实采集，后到者共享首个结果。"""

    def test_concurrent_refresh_collects_once(self):
        calls = []

        def slow_collect(_root=None):
            calls.append(1)
            time.sleep(0.05)
            return {"window": "weekly", "remaining_pct": 50, "source": "mock"}

        orig = (quota.codex_quota, quota.dsh_subscription, quota.dsh_balance)
        quota.codex_quota = slow_collect
        quota.dsh_subscription = lambda: None
        quota.dsh_balance = lambda: None
        quota._cache.update(ts=-1e9, data={})  # 强制过期
        try:
            barrier = threading.Barrier(8)
            results = []

            def worker():
                barrier.wait()
                results.append(quota.quota_state())

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(len(calls), 1)
            self.assertEqual(results, [results[0]] * 8)
        finally:
            quota.codex_quota, quota.dsh_subscription, quota.dsh_balance = orig
            quota._cache.update(ts=0.0, data={})

    def test_fresh_cache_skips_collection(self):
        calls = []
        orig = (quota.codex_quota, quota.dsh_subscription, quota.dsh_balance)
        quota.codex_quota = lambda root=None: calls.append(1) or {"source": "mock"}
        quota.dsh_subscription = lambda: None
        quota.dsh_balance = lambda: None
        try:
            first = quota.quota_state()
            second = quota.quota_state()
            self.assertIs(first, second)
            self.assertEqual(len(calls), 1)
        finally:
            quota.codex_quota, quota.dsh_subscription, quota.dsh_balance = orig
            quota._cache.update(ts=0.0, data={})


class UsageAggregateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.now = time.time()
        self.day_start = datetime.fromtimestamp(self.now).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()

    def _in_day(self, mtime=None):
        return self.day_start + 60

    def _iso(self, ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

    def _codex_token_line(self, ts, total):
        return json.dumps({"timestamp": self._iso(ts),
                           "payload": {"type": "token_count",
                                       "info": {"total_token_usage": {"total_tokens": total}}}})

    def test_zcode_models_aggregate(self):
        db = self.root / "zcode" / "cli" / "db" / "db.sqlite"
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE model_usage (model_id TEXT, started_at INTEGER, computed_total_tokens INTEGER)")
        con.executemany("INSERT INTO model_usage VALUES (?,?,?)", [
            ("glm", int((self.day_start + 100) * 1000), 100),
            ("glm", int((self.day_start + 200) * 1000), 50),
            ("ds", int((self.day_start + 300) * 1000), 30),
            ("old", int((self.day_start - 1000) * 1000), 999),   # 昨日排除
        ])
        con.commit(); con.close()
        got = usage.zcode_usage(self.root / "zcode", self.day_start)
        self.assertEqual(got["today_tokens"], 180)
        self.assertEqual(got["models"], {"glm": 150, "ds": 30})

    def test_codex_today_files_summed(self):
        day_dir = "2026/09/" + f"{datetime.fromtimestamp(self.now):%d}"
        _codex_file(self.root / "codex", f"{day_dir}/rollout-a.jsonl",
                    [self._codex_token_line(self.day_start + 60, 500)])
        got = usage.codex_usage(self.root / "codex", self.day_start)
        self.assertEqual(got["today_tokens"], 500)
        self.assertEqual(got["basis"], "exact")

    def test_codex_cross_day_differential(self):
        # 审计 B07 反例：昨天累计 1,000,000 + 今天累计 1,000,100 → 今日只算 100
        day_dir = "2026/09/" + f"{datetime.fromtimestamp(self.now):%d}"
        _codex_file(self.root / "codex", f"{day_dir}/rollout-x.jsonl", [
            self._codex_token_line(self.day_start - 3600, 1_000_000),
            self._codex_token_line(self.day_start + 60, 1_000_050),
            self._codex_token_line(self.day_start + 120, 1_000_100),
        ])
        got = usage.codex_usage(self.root / "codex", self.day_start)
        self.assertEqual(got["today_tokens"], 100)

    def test_codex_session_started_today_counts_full_total(self):
        # 今天新建的会话没有日切基线：两次累计 40→60，今日 = 60
        day_dir = "2026/09/" + f"{datetime.fromtimestamp(self.now):%d}"
        _codex_file(self.root / "codex", f"{day_dir}/rollout-y.jsonl", [
            self._codex_token_line(self.day_start + 10, 40),
            self._codex_token_line(self.day_start + 20, 60),
        ])
        got = usage.codex_usage(self.root / "codex", self.day_start)
        self.assertEqual(got["today_tokens"], 60)

    def test_codex_stale_file_excluded(self):
        jf = _codex_file(self.root / "codex2", "2026/09/25/rollout-a.jsonl",
                         [self._codex_token_line(self.day_start + 60, 700)])
        t = self.day_start - 5000
        os.utime(jf, (t, t))
        self.assertIsNone(usage.codex_usage(self.root / "codex2", self.day_start))

    def test_kimicode_turn_tokens_summed(self):
        wire = self.root / "kc" / "sessions" / "wd" / "s1" / "agents" / "main" / "wire.jsonl"
        now_ms = int((self.day_start + 600) * 1000)
        _write(wire, "\n".join([
            json.dumps({"type": "token_counting.turn_recorded", "time": now_ms, "tokens": 120}),
            json.dumps({"type": "token_counting.measured", "time": now_ms, "tokens": 7}),
            json.dumps({"type": "token_counting.turn_recorded", "time": now_ms, "tokens": 80}),
        ]))
        got = usage.kimicode_usage(self.root / "kc", self.day_start)
        self.assertEqual(got["today_tokens"], 200)
        self.assertEqual(got["basis"], "exact")

    def test_kimicode_cross_day_turns_excluded(self):
        # 行级 time 过滤：昨天的 turn 不计入今天（审计 B07）
        wire = self.root / "kc2" / "sessions" / "wd" / "s1" / "agents" / "main" / "wire.jsonl"
        _write(wire, "\n".join([
            json.dumps({"type": "token_counting.turn_recorded",
                        "time": int((self.day_start - 3600) * 1000), "tokens": 5000}),
            json.dumps({"type": "token_counting.turn_recorded",
                        "time": int((self.day_start + 60) * 1000), "tokens": 120}),
            json.dumps({"type": "token_counting.turn_recorded",
                        "time": int((self.day_start + 90) * 1000), "tokens": 80}),
        ]))
        got = usage.kimicode_usage(self.root / "kc2", self.day_start)
        self.assertEqual(got["today_tokens"], 200)

    def test_codex_unreadable_sources_not_exact_zero(self):
        # 复核 F06：读不了的源不能冒充精确零；唯一源不可读 → None
        day_dir = "2026/09/" + f"{datetime.fromtimestamp(self.now):%d}"
        _codex_file(self.root / "codex3", f"{day_dir}/rollout-a.jsonl", ["{}\n"])
        with mock.patch.object(Path, "read_text",
                               side_effect=PermissionError("synthetic inaccessible")):
            self.assertIsNone(usage.codex_usage(self.root / "codex3", self.day_start))

    def test_codex_mixed_readable_and_unreadable_is_partial(self):
        # 目录名占位 = read_text 抛 OSError 的不可读源；可读部分照算但降级
        day_dir = "2026/09/" + f"{datetime.fromtimestamp(self.now):%d}"
        _codex_file(self.root / "codex4", f"{day_dir}/rollout-good.jsonl",
                    [self._codex_token_line(self.day_start + 60, 500)])
        bad = self.root / "codex4" / "sessions" / day_dir / "rollout-bad.jsonl"
        bad.mkdir()
        t = self.day_start + 120
        os.utime(bad, (t, t))
        got = usage.codex_usage(self.root / "codex4", self.day_start)
        self.assertEqual(got["today_tokens"], 500)
        self.assertEqual(got["basis"], "partial")

    def test_codex_bad_token_line_excludes_session(self):
        # R2-01：候选行损坏 → 该会话整体退出聚合（差分依赖基线，坏基线会高估）；
        # 唯一会话不可用 → None，不再给 500/partial
        day_dir = "2026/09/" + f"{datetime.fromtimestamp(self.now):%d}"
        _codex_file(self.root / "codex5", f"{day_dir}/rollout-a.jsonl", [
            self._codex_token_line(self.day_start + 60, 500),
            '{"timestamp":"not-json-anymore "token_count": 1}',
        ])
        self.assertIsNone(usage.codex_usage(self.root / "codex5", self.day_start))

    def test_codex_missing_timestamp_session_excluded(self):
        # R2-01 复核场景：有累计值但缺时间戳 → 无法归属日期，会话退出聚合
        day_dir = "2026/09/" + f"{datetime.fromtimestamp(self.now):%d}"
        line = json.dumps({"payload": {"type": "token_count",
                           "info": {"total_token_usage": {"total_tokens": 100}}}})
        _codex_file(self.root / "codex6", f"{day_dir}/rollout-a.jsonl", [line])
        self.assertIsNone(usage.codex_usage(self.root / "codex6", self.day_start))

    def test_codex_invalid_total_session_excluded(self):
        # R2-01 复核场景：total_tokens 非数字 → 会话退出聚合
        day_dir = "2026/09/" + f"{datetime.fromtimestamp(self.now):%d}"
        _codex_file(self.root / "codex7", f"{day_dir}/rollout-a.jsonl", [
            json.dumps({"timestamp": self._iso(self.day_start + 60),
                        "payload": {"type": "token_count",
                                    "info": {"total_token_usage": {"total_tokens": "broken"}}}}),
        ])
        self.assertIsNone(usage.codex_usage(self.root / "codex7", self.day_start))

    def test_codex_damaged_baseline_does_not_overcount(self):
        # R2-01 复核场景：昨日累计 1000 的记录截断、今日累计 1100（真实增量 100）
        # ——坏基线从 0 相减会算成 1100；修复后该会话退出聚合
        day_dir = "2026/09/" + f"{datetime.fromtimestamp(self.now):%d}"
        _codex_file(self.root / "codex8", f"{day_dir}/rollout-a.jsonl", [
            self._codex_token_line(self.day_start - 3600, 1000)[:-1],   # 截断最后一个字符
            self._codex_token_line(self.day_start + 60, 1100),
        ])
        self.assertIsNone(usage.codex_usage(self.root / "codex8", self.day_start))

    def test_codex_damaged_session_keeps_lower_bound(self):
        # 坏会话退出聚合、好会话照算：partial 数字恒为下限
        day_dir = "2026/09/" + f"{datetime.fromtimestamp(self.now):%d}"
        _codex_file(self.root / "codex9", f"{day_dir}/rollout-good.jsonl",
                    [self._codex_token_line(self.day_start + 60, 300)])
        _codex_file(self.root / "codex9", f"{day_dir}/rollout-bad.jsonl", [
            self._codex_token_line(self.day_start - 3600, 1000)[:-1],
            self._codex_token_line(self.day_start + 60, 1100),
        ])
        got = usage.codex_usage(self.root / "codex9", self.day_start)
        self.assertEqual(got["today_tokens"], 300)   # 只含好会话，绝不高估
        self.assertEqual(got["basis"], "partial")

    def _codex_with_lines(self, lines):
        day_dir = "2026/09/" + f"{datetime.fromtimestamp(self.now):%d}"
        _codex_file(self.root / "cx", f"{day_dir}/rollout-x.jsonl", lines)
        return usage.codex_usage(self.root / "cx", self.day_start)

    def _codex_raw_count(self, raw):
        return json.dumps({"timestamp": self._iso(self.day_start + 60),
                           "payload": {"type": "token_count",
                                       "info": {"total_token_usage": {"total_tokens": raw}}}})

    def test_codex_non_object_candidate_excluded_without_exception(self):
        # R3-01：顶层是数组（含标记子串）→ 结构无效，会话隔离，不得抛 AttributeError
        self.assertIsNone(self._codex_with_lines(['["token_count"]']))

    def test_codex_bool_counter_excluded(self):
        # R3-01：true 是 int 子类，不是有效计数；int() 强转会算成 1
        self.assertIsNone(self._codex_with_lines([self._codex_raw_count(True)]))

    def test_codex_fractional_counter_excluded(self):
        # R3-01：小数计数不得静默截断后宣称精确
        self.assertIsNone(self._codex_with_lines([self._codex_raw_count(1.5)]))

    def test_codex_negative_counter_excluded(self):
        # R3-01：负计数是坏数据，不得伪装成精确零
        self.assertIsNone(self._codex_with_lines([self._codex_raw_count(-10)]))

    def test_codex_zero_counter_is_exact_zero(self):
        # 合法 0 的成功路径：真实今日零用量仍是 exact
        got = self._codex_with_lines([self._codex_raw_count(0)])
        self.assertEqual(got, {"today_tokens": 0, "models": {}, "basis": "exact"})

    def test_codex_bad_source_does_not_interrupt_others(self):
        # R3-01：单个坏来源（顶层数组）不得中断其他来源的聚合
        day_dir = "2026/09/" + f"{datetime.fromtimestamp(self.now):%d}"
        _codex_file(self.root / "codex10", f"{day_dir}/rollout-good.jsonl",
                    [self._codex_token_line(self.day_start + 60, 300)])
        _codex_file(self.root / "codex10", f"{day_dir}/rollout-array.jsonl",
                    ['["token_count"]'])
        got = usage.codex_usage(self.root / "codex10", self.day_start)
        self.assertEqual(got["today_tokens"], 300)
        self.assertEqual(got["basis"], "partial")

    def test_kimicode_negative_values_are_invalid(self):
        # R3-01：负 time / 负 tokens 均为无效记录，不计入并降级
        wire = self.root / "kc6" / "sessions" / "wd" / "s1" / "agents" / "main" / "wire.jsonl"
        _write(wire, "\n".join([
            json.dumps({"type": "token_counting.turn_recorded",
                        "time": int((self.day_start + 60) * 1000), "tokens": 50}),
            json.dumps({"type": "token_counting.turn_recorded",
                        "time": int((self.day_start + 90) * 1000), "tokens": -10}),
            json.dumps({"type": "token_counting.turn_recorded",
                        "time": -1, "tokens": 30}),
        ]))
        got = usage.kimicode_usage(self.root / "kc6", self.day_start)
        self.assertEqual(got["today_tokens"], 50)   # 只计合法行
        self.assertEqual(got["basis"], "partial")

    def test_kimicode_turn_without_time_is_partial(self):
        wire = self.root / "kc3" / "sessions" / "wd" / "s1" / "agents" / "main" / "wire.jsonl"
        _write(wire, "\n".join([
            json.dumps({"type": "token_counting.turn_recorded",
                        "time": int((self.day_start + 60) * 1000), "tokens": 120}),
            json.dumps({"type": "token_counting.turn_recorded", "tokens": 9999}),
        ]))
        got = usage.kimicode_usage(self.root / "kc3", self.day_start)
        self.assertEqual(got["today_tokens"], 120)
        self.assertEqual(got["basis"], "partial")

    def test_kimicode_truncated_turn_not_exact(self):
        # R2-01 复核场景：正则能命中 time/tokens，但 JSON 已截断 → 不计入且降级
        wire = self.root / "kc5" / "sessions" / "wd" / "s1" / "agents" / "main" / "wire.jsonl"
        truncated = (json.dumps({"type": "token_counting.turn_recorded",
                                 "time": int((self.day_start + 60) * 1000), "tokens": 20}))[:-1]
        _write(wire, truncated + "\n")
        got = usage.kimicode_usage(self.root / "kc5", self.day_start)
        self.assertEqual(got["today_tokens"], 0)
        self.assertEqual(got["basis"], "partial")

    def test_kimicode_all_unreadable_is_none(self):
        wire = self.root / "kc4" / "sessions" / "wd" / "s1" / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True)
        wire.mkdir()   # 目录名占位：read_text 抛 OSError
        t = self.day_start + 60
        os.utime(wire, (t, t))
        self.assertIsNone(usage.kimicode_usage(self.root / "kc4", self.day_start))

    def test_hermes_day_window_sum(self):
        db = self.root / "hermes" / "state.db"
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE messages (token_count INTEGER, timestamp REAL)")
        con.executemany("INSERT INTO messages VALUES (?,?)", [
            (10, self.day_start + 10), (5, self.day_start + 20), (99, self.day_start - 1),
        ])
        con.commit(); con.close()
        got = usage.hermes_usage(self.root / "hermes", self.day_start)
        self.assertEqual(got["today_tokens"], 15)

    def test_usage_state_shape(self):
        orig = quota.quota_state
        quota.quota_state = lambda codex_root=None: {"codex": None, "dsh": None}
        try:
            got = usage.usage_state({}, now=self.now)
        finally:
            quota.quota_state = orig
        self.assertEqual(set(got["per_agent"]),
                         {"zcode", "codex", "kimicode", "hermes", "qoder", "kimiwork", "dsh"})
        self.assertTrue(all(v is None for v in got["per_agent"].values()))


if __name__ == "__main__":
    unittest.main()
