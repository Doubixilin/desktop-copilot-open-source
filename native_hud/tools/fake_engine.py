# 验收辅助：Listary 兼容假引擎（S03 乱序/离线复现用）。仅监听 127.0.0.1:39999。
# 查询含 "slow" 时延迟 1.5s，用于制造乱序响应；返回可识别的合成结果。
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        q = str(body.get("query", ""))
        time.sleep(1.5 if "slow" in q else 0.05)
        # fast 查询返回真实存在的夹具路径，供动作链验证；slow 返回不存在路径
        if "fast" in q:
            path = ("C:\\example\\fixtures"
                    "\\20260927\\search-fixtures\\HUD验收-测试文件 alpha.txt")
        else:
            path = f"D:\\fake\\{q}.txt"
        payload = {"ok": True, "data": {"total": 1, "results": [
            {"path": path, "name": path.split("\\")[-1], "type": "file", "size_bytes": 1}]}}
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print("fake engine on 127.0.0.1:39999", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 39999), Handler).serve_forever()
