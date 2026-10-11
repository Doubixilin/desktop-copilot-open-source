"""本地接口边界回归（审计 R05，票 audit-remediation 06）。

进程内临时回环服务（ephemeral 端口）+ 桩数据函数：验证 Host 校验、
CORS 白名单回显与令牌分级。不触碰生产 5000 端口、不启动真实采集。
"""
import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import server


class BoundaryTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_log = server.Handler.log_message
        server.Handler.log_message = lambda self, fmt, *args: None
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.Handler.log_message = cls._orig_log

    def setUp(self):
        stubs = {
            "deck_state": lambda: {"sessions": [{
                "tool": "zcode", "state": "RUN", "title": "秘密标题",
                "project": "proj", "preview": "正文预览", "preview_role": "user",
                "tasks": [{"subject": "t"}],
            }], "ts": 1.0},
            "build_payload": lambda: {"psutil": {"cpu": 1}, "qoder": {
                "active_sessions": 1,
                "session": {"project": "proj", "running": True,
                            "tasks_done": 2, "tasks_total": 3, "current_task": "doing"}}},
            "usage_state": lambda: {"per_agent": {"zcode": {"today_tokens": 5}},
                                    "quota": {}, "ts": 1.0},
        }
        self._orig = {name: getattr(server, name) for name in stubs}
        for name, stub in stubs.items():
            setattr(server, name, stub)
        self._orig_token = server._configured_token
        server._configured_token = lambda: None

    def tearDown(self):
        for name, orig in self._orig.items():
            setattr(server, name, orig)
        server._configured_token = self._orig_token

    def fetch(self, path, host="127.0.0.1", origin=None):
        headers = {"Host": host}
        if origin is not None:
            headers["Origin"] = origin
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
            out = {k.lower(): v for k, v in resp.getheaders()}
            data = json.loads(body) if body and "json" in out.get("content-type", "") else None
            return resp.status, out, data
        finally:
            conn.close()


class CorsAndHostBoundaryTest(BoundaryTestBase):
    def test_foreign_origin_gets_no_acao_header(self):
        status, headers, body = self.fetch("/deck", origin="https://evil.com")
        self.assertEqual(status, 200)
        self.assertNotIn("access-control-allow-origin", headers)
        self.assertEqual(body["sessions"][0]["tool"], "zcode")  # 服务端照常响应

    def test_spoofed_local_subdomain_gets_no_acao_header(self):
        _, headers, _ = self.fetch("/deck", origin="http://127.0.0.1.evil.com")
        self.assertNotIn("access-control-allow-origin", headers)

    def test_null_and_loopback_origins_are_echoed(self):
        _, h1, _ = self.fetch("/deck", origin="null")   # file:// 壁纸页面
        self.assertEqual(h1.get("access-control-allow-origin"), "null")
        _, h2, _ = self.fetch("/deck", origin="http://127.0.0.1:5000")
        self.assertEqual(h2.get("access-control-allow-origin"), "http://127.0.0.1:5000")
        _, h3, _ = self.fetch("/deck", origin="http://localhost:5001")
        self.assertEqual(h3.get("access-control-allow-origin"), "http://localhost:5001")

    def test_rebinding_host_is_rejected(self):
        status, _, _ = self.fetch("/deck", host="rebind.attacker.com")
        self.assertEqual(status, 403)

    def test_loopback_host_names_accepted(self):
        for host in ("127.0.0.1", "localhost", "[::1]"):
            status, _, _ = self.fetch("/deck", host=host)
            self.assertEqual(status, 200)


class TokenGateTest(BoundaryTestBase):
    def setUp(self):
        super().setUp()
        server._configured_token = lambda: "s3cret"

    def test_deck_without_token_strips_sensitive_fields(self):
        _, _, body = self.fetch("/deck")
        s = body["sessions"][0]
        self.assertEqual(s["tool"], "zcode")     # 基础状态保留
        self.assertEqual(s["state"], "RUN")
        for key in ("title", "project", "preview", "preview_role", "tasks"):
            self.assertNotIn(key, s)

    def test_deck_with_token_returns_full_payload(self):
        _, _, body = self.fetch("/deck?token=s3cret")
        self.assertEqual(body["sessions"][0]["title"], "秘密标题")

    def test_wrong_token_strips_like_no_token(self):
        _, _, body = self.fetch("/deck?token=wrong")
        self.assertNotIn("title", body["sessions"][0])

    def test_deck_payload_reports_trusted_flag(self):
        # 前端凭 trusted 区分"服务离线"与"令牌失效"（AUTH 角标）
        _, _, body = self.fetch("/deck")
        self.assertIs(body["trusted"], False)
        _, _, body2 = self.fetch("/deck?token=s3cret")
        self.assertIs(body2["trusted"], True)

    def test_performance_public_view_keeps_counts_not_content(self):
        _, _, body = self.fetch("/performance")
        s = body["qoder"]["session"]
        self.assertNotIn("project", s)
        self.assertNotIn("current_task", s)
        self.assertEqual(s["tasks_done"], 2)
        _, _, full = self.fetch("/performance?token=s3cret")
        self.assertEqual(full["qoder"]["session"]["project"], "proj")

    def test_usage_aggregates_not_token_gated(self):
        _, _, body = self.fetch("/usage")
        self.assertEqual(body["per_agent"]["zcode"]["today_tokens"], 5)


class DefaultStrippedTest(BoundaryTestBase):
    """F01：敏感内容默认裁剪——未配置令牌/Origin null 都拿不到内容字段。"""

    def test_unconfigured_token_strips_by_default(self):
        _, _, body = self.fetch("/deck")
        s = body["sessions"][0]
        self.assertEqual(s["tool"], "zcode")
        for key in ("title", "project", "preview", "preview_role", "tasks"):
            self.assertNotIn(key, s)

    def test_null_origin_without_token_gets_no_content(self):
        # codex 复核 F01 的精确场景：Origin null、无令牌、未配置 → 200 + ACAO
        # 回显可以保留（CORS 层），但敏感字段必须不在
        status, headers, body = self.fetch("/deck", origin="null")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("access-control-allow-origin"), "null")
        self.assertNotIn("preview", body["sessions"][0])
        self.assertNotIn("title", body["sessions"][0])


class LogRedactionTest(BoundaryTestBase):
    """F02：请求日志不得落 token 值。"""

    def setUp(self):
        super().setUp()
        server._configured_token = lambda: "s3cret"
        self._lines = []
        self._orig_msg = server.Handler.log_message
        server.Handler.log_message = lambda self_h, fmt, *args: self._lines.append(fmt % args)

    def tearDown(self):
        server.Handler.log_message = self._orig_msg
        super().tearDown()

    def test_token_value_never_logged(self):
        self.fetch("/deck?token=s3cret")
        self.fetch("/deck?token=WRONG&x=1")
        self.fetch("/nope?token=s3cret")
        joined = "\n".join(self._lines)
        self.assertTrue(joined)                        # 日志确实在写
        self.assertNotIn("s3cret", joined)             # 有效令牌不落日志
        self.assertNotIn("WRONG", joined)              # 错误令牌同样不落
        self.assertNotIn("?token", joined)             # 查询串整段不落日志
        self.assertIn("GET /deck", joined)             # 路由路径保留


class ConfiguredTokenLifecycleTest(unittest.TestCase):
    """_configured_token：env 优先 > 文件；缺失自动生成；失败返回 None。"""

    def test_env_token_wins(self):
        with mock.patch.dict(os.environ, {"QD_API_TOKEN": "env-token"}, clear=False):
            self.assertEqual(server._configured_token(), "env-token")

    def test_file_autogenerated_and_stable(self):
        with tempfile.TemporaryDirectory() as td:
            env = {"LOCALAPPDATA": td}
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop("QD_API_TOKEN", None)
                first = server._configured_token()
                self.assertTrue(first)
                self.assertTrue((Path(td) / "qoder-deck" / "api-token").is_file())
                # 再读：从文件取出同一值，不重复生成
                self.assertEqual(server._configured_token(), first)

    def test_unwritable_location_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            blocker = Path(td) / "blocker"
            blocker.write_text("x", encoding="utf-8")
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(blocker)}, clear=False):
                os.environ.pop("QD_API_TOKEN", None)
                self.assertIsNone(server._configured_token())

    def test_corrupt_token_file_returns_none_without_raising(self):
        # R2-02：非 UTF-8 的既有令牌文件 → UnicodeDecodeError 必须被降级为 None
        with tempfile.TemporaryDirectory() as td:
            token_path = Path(td) / "api-token"
            token_path.write_bytes(b"\xff\xfe")
            with mock.patch.dict(os.environ, {"QD_API_TOKEN": ""}, clear=False):
                with mock.patch.object(server, "_token_path", return_value=token_path):
                    self.assertIsNone(server._configured_token())

    def test_corrupt_token_file_is_not_silently_replaced(self):
        # R2-02：已存在的损坏凭据不覆盖（区分"文件缺失→生成"与"文件损坏→等人工"）
        with tempfile.TemporaryDirectory() as td:
            token_path = Path(td) / "api-token"
            token_path.write_bytes(b"\xff\xfe")
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("QD_API_TOKEN", None)
                with mock.patch.object(server, "_token_path", return_value=token_path):
                    self.assertIsNone(server._configured_token())
            self.assertEqual(token_path.read_bytes(), b"\xff\xfe")


class CorruptTokenAvailabilityTest(BoundaryTestBase):
    """R2-02：令牌文件损坏时服务不 500——基础视图可用、敏感字段照常裁剪。"""

    def setUp(self):
        super().setUp()
        server._configured_token = self._orig_token  # 恢复真实实现
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        token_path = Path(self._tmp.name) / "api-token"
        token_path.write_bytes(b"\xff\xfe")
        patcher = mock.patch.object(server, "_token_path", return_value=token_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("QD_API_TOKEN", None)

    def test_basic_view_available_and_content_stripped(self):
        status, _, body = self.fetch("/deck")
        self.assertEqual(status, 200)
        self.assertEqual(body["sessions"][0]["tool"], "zcode")
        self.assertNotIn("preview", body["sessions"][0])
        self.assertNotIn("title", body["sessions"][0])
        # 任何令牌都无效（文件损坏）：带错令牌也不放开
        status2, _, body2 = self.fetch("/deck?token=whatever")
        self.assertEqual(status2, 200)
        self.assertNotIn("preview", body2["sessions"][0])


if __name__ == "__main__":
    unittest.main()
