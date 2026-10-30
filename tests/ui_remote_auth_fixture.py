"""外网登录页的本地浏览器验收夹具，不连接真实数据库或 115。"""
from __future__ import annotations

import argparse
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit


DIST_DIR = Path(__file__).parents[1] / "frontend" / "dist"
SESSION_COOKIE = "ph115_remote_session=ui-test-session"
EXPECTED_AUTH = "Basic " + base64.b64encode(
    b"demo:correct-password").decode("ascii")


class Handler(BaseHTTPRequestHandler):
    server_version = "PH115UiFixture/1.0"

    def log_message(self, *_args):
        return

    def _json(self, status: int, payload: dict | list, *headers: tuple[str, str]):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _inner_path(self) -> str | None:
        path = urlsplit(self.path).path
        if path == "/ph115":
            return ""
        if path.startswith("/ph115/"):
            return path[len("/ph115"):]
        return None

    def _authenticated(self) -> bool:
        return SESSION_COOKIE in self.headers.get("Cookie", "")

    def do_GET(self):
        inner = self._inner_path()
        if inner == "":
            self.send_response(302)
            self.send_header("Location", "/ph115/")
            self.end_headers()
            return
        if inner == "/api/remote-auth/status":
            authenticated = self._authenticated()
            self._json(200, {
                "required": True,
                "authenticated": authenticated,
                "expires_at": "2030-01-01T00:00:00+00:00" if authenticated else None,
                "remember_days": 30,
            })
            return
        if inner == "/api/115/status":
            self._json(200, {"logged_in": False})
            return
        if inner == "/api/models":
            self._json(200, {"models": []})
            return
        if inner == "/api/patrol/status":
            self._json(200, {
                "enabled": False,
                "running": False,
                "interval_hours": 6,
                "last_run_at": None,
                "next_run_at": None,
            })
            return
        if inner == "/ws":
            self.send_error(426, "WebSocket is not needed by this UI fixture")
            return
        self._serve_static(inner)

    def do_POST(self):
        inner = self._inner_path()
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        if content_length:
            self.rfile.read(content_length)
        if inner == "/api/remote-auth/login":
            if self.headers.get("Authorization") != EXPECTED_AUTH:
                # 故意不返回 WWW-Authenticate，模拟 Nginx 的无原生弹窗错误响应。
                self._json(403, {
                    "detail": "账号或密码不正确",
                    "code": "REMOTE_LOGIN_FAILED",
                })
                return
            self._json(200, {
                "required": True,
                "authenticated": True,
                "expires_at": "2030-01-01T00:00:00+00:00",
            }, ("Set-Cookie", SESSION_COOKIE + "; Path=/ph115; HttpOnly; SameSite=Lax"))
            return
        if inner == "/api/remote-auth/logout":
            self._json(200, {"authenticated": False}, (
                "Set-Cookie",
                "ph115_remote_session=; Path=/ph115; Max-Age=0; HttpOnly; SameSite=Lax",
            ))
            return
        self._json(404, {"detail": "fixture endpoint not found"})

    def _serve_static(self, inner: str | None):
        if inner is None:
            self.send_error(404)
            return
        relative = "index.html" if inner in {"", "/"} else inner.lstrip("/")
        target = (DIST_DIR / relative).resolve()
        try:
            target.relative_to(DIST_DIR.resolve())
        except ValueError:
            self.send_error(404)
            return
        if not target.is_file():
            self.send_error(404)
            return
        body = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4174)
    args = parser.parse_args()
    if not (DIST_DIR / "index.html").is_file():
        raise SystemExit("请先执行 frontend/npm run build")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"UI fixture: http://127.0.0.1:{args.port}/ph115/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
