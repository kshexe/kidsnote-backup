"""Tiny in-process HTTP server for tests (stdlib only).

Each test passes a handler ``(Request) -> (status, headers, body)``; the
server records every request so tests can assert on what was sent.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

Body = bytes | str | dict | list


@dataclass
class Request:
    method: str
    target: str
    headers: dict[str, str]  # lower-cased names
    body: bytes = b""
    path: str = field(init=False)
    query: dict[str, list[str]] = field(init=False)

    def __post_init__(self) -> None:
        parts = urlsplit(self.target)
        self.path = parts.path
        self.query = parse_qs(parts.query)

    def json(self) -> dict:
        return json.loads(self.body.decode("utf-8"))


Handler = Callable[[Request], tuple[int, dict, Body]]


class FakeServer:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[Request] = []
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                req = Request(self.command, self.path,
                              {k.lower(): v for k, v in self.headers.items()}, body)
                outer.requests.append(req)
                status, headers, payload = outer.handler(req)
                headers = dict(headers)
                if isinstance(payload, (dict, list)):
                    payload = json.dumps(payload).encode("utf-8")
                    headers.setdefault("Content-Type", "application/json")
                elif isinstance(payload, str):
                    payload = payload.encode("utf-8")
                try:
                    self.send_response(status)
                    for name, value in headers.items():
                        for item in value if isinstance(value, list) else [value]:
                            self.send_header(name, item)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except ConnectionError:  # client gave up first (timeout tests)
                    pass

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

            def log_message(self, *args) -> None:  # keep test output clean
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> FakeServer:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def paths(self) -> list[str]:
        return [f"{r.method} {r.path}" for r in self.requests]
