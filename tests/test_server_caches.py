"""server 侧 TTL 缓存的并发语义（审计 B10/N03，票 audit-remediation 04）。

只测 _TtlCache 纯语义（fresh/expired/single-flight），不启动服务、
不触碰真实采集函数。
"""
import threading
import time
import unittest

from server import _TtlCache


class TtlCacheTest(unittest.TestCase):
    def test_single_flight_under_concurrency(self):
        calls = []

        def build():
            calls.append(1)
            time.sleep(0.05)
            return {"v": len(calls)}

        cache = _TtlCache(60.0)
        results = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(cache.get(build))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(calls), 1)
        self.assertEqual(results, [{"v": 1}] * 8)

    def test_fresh_within_ttl_and_expired_after(self):
        clock = {"now": 100.0}
        orig = time.monotonic
        time.monotonic = lambda: clock["now"]
        try:
            cache = _TtlCache(10.0)
            self.assertEqual(cache.get(lambda: "a"), "a")
            clock["now"] = 105.0
            self.assertEqual(cache.get(lambda: "b"), "a")   # TTL 内共享缓存
            clock["now"] = 111.0
            self.assertEqual(cache.get(lambda: "c"), "c")   # 过期重建
        finally:
            time.monotonic = orig

    def test_init_data_not_none_does_not_skip_first_build(self):
        # gpu 缓存以 {} 初始化：ts=0 视为从未采集，首次仍要真实构建
        cache = _TtlCache(3.0, init={})
        clock = {"now": 500.0}
        orig = time.monotonic
        time.monotonic = lambda: clock["now"]
        try:
            self.assertEqual(cache.get(lambda: {"gpu_usage": 1}), {"gpu_usage": 1})
        finally:
            time.monotonic = orig


if __name__ == "__main__":
    unittest.main()
