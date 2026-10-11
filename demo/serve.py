"""Loopback-only, stdlib-only preview of the real HUD UI with synthetic data."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
ROUTES = {
    "/": ("demo/index.html", "text/html"),
    "/demo.css": ("demo/demo.css", "text/css"),
    "/bridge.js": ("demo/bridge.js", "application/javascript"),
    "/styles.css": ("native_hud/ui/styles.css", "text/css"),
    "/app.js": ("native_hud/ui/app.js", "application/javascript"),
    "/hud.html": ("native_hud/ui/index.html", "text/html"),
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        route = ROUTES.get(path)
        if not route:
            self.send_error(404)
            return
        file, mime = route
        content = (ROOT / file).read_bytes()
        if path == "/hud.html":
            content = content.replace(b'<script src="app.js"></script>', b'<script src="bridge.js"></script>\n<script src="app.js"></script>')
        self.send_response(200)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *_args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18766)
    args = parser.parse_args()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        parser.exit(1, f"Cannot bind loopback port {args.port}: {exc}. Try another --port.\n")
    print(f"Synthetic preview: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
