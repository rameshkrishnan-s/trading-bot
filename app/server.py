from __future__ import annotations

import hmac
import json
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from app.config import Config
from app.engine import Engine
from app.store import Store
from app.runtime import engine_locks


ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "static"


def load_env(path: Path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class Handler(BaseHTTPRequestHandler):
    engine: Engine
    config: Config

    def log_message(self, format, *args):
        # Access logs intentionally omit request headers and credentials.
        if args and str(args[0]).startswith('GET /api/state '):
            return
        print("[http]", format % args)

    def _send(self, status: int, data: bytes, mime: str):
        try:
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # A closed browser tab or cancelled poll must not fail the server request.
            self.close_connection = True


    def _json(self, status: int, payload):
        self._send(status, json.dumps(payload, separators=(",", ":"), default=str).encode(), "application/json; charset=utf-8")

    def _local_request(self):
        allowed = {f'127.0.0.1:{self.config.port}', f'localhost:{self.config.port}'}
        host = self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        if host not in allowed or (origin and origin not in {'http://' + name for name in allowed}):
            self._json(403, {'error': 'Use the local dashboard or its SSH tunnel'})
            return False
        return True

    def do_GET(self):
        if not self._local_request():
            return
        path = urlsplit(self.path).path
        if path == "/api/state":
            self._json(200, self.engine.view())
            return
        if path == "/api/health":
            health = self.engine.health()
            self._json(200 if health["ok"] else 503, health)
            return
        files = {"/": "index.html", "/static/app.css": "app.css", "/static/app.js": "app.js", "/static/favicon.svg": "favicon.svg"}
        name = files.get(path)
        if not name:
            self._json(404, {"error": "Not found"})
            return
        mime = "text/html; charset=utf-8" if name.endswith("html") else "text/css; charset=utf-8" if name.endswith("css") else "image/svg+xml" if name.endswith("svg") else "text/javascript; charset=utf-8"
        self._send(200, (PUBLIC / name).read_bytes(), mime)

    def do_POST(self):
        if not self._local_request():
            return
        if urlsplit(self.path).path != "/api/control":
            self._json(404, {"error": "Not found"})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            self._json(415, {"error": "JSON required"})
            return
        # Constant-time comparison; bytes so a non-ASCII header is rejected instead of raising.
        provided = self.headers.get("X-Control-Token", "").encode()
        if self.config.control_token and not hmac.compare_digest(provided, self.config.control_token.encode()):
            self._json(401, {"error": "Control token required"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 2048:
                raise ValueError("Invalid request length")
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError("JSON object required")
            result = self.engine.control(request.get("action", ""))
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, result)


def main():
    load_env(Path(os.getenv("BOT_ENV_FILE", str(ROOT / ".env"))))
    config = Config.from_env()
    with engine_locks(config):
        serve(config)


def serve(config):
    # Reserve the dashboard port before creating/mutating a database or engine.
    # A second launch must fail without affecting the existing experiment.
    server = ThreadingHTTPServer((config.host, config.port), Handler)
    try:
        store = Store(config.db_path)
        engine = Engine(config, store)
    except Exception:
        server.server_close()
        raise
    Handler.engine = engine
    Handler.config = config
    engine.start()
    print(f"Options Desk {config.mode.upper()} listening on http://{config.host}:{config.port}", flush=True)

    def close(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, close)
    signal.signal(signal.SIGINT, close)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        engine.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
