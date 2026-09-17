"""Configurable stand-in for kidsnote's login and session endpoints."""
from __future__ import annotations

import time
from http.cookies import SimpleCookie

from fake_http import Request

SESSIONID = "k1d5n0te5e55i0n1dabcdef012345678"  # 32 alphanumerics, like a real one


class FakeKidsnote:
    """Behaves like the real API for known users; ``scripts`` override responses in order."""

    def __init__(self, users: dict[str, str] | None = None) -> None:
        self.users = dict(users or {})
        self.children: list[dict] = [{"id": 1, "name": "테스트아이"}]
        self.two_factor = False
        self.second_factors: list[dict] = [{"id": 7, "auth_type": 1}]
        self.issue_cookie = True
        self.cookie_value = SESSIONID
        self.valid_sessions: set[str] = set()
        self.delay = 0.0
        self.login_bodies: list[dict] = []
        # endpoint -> queued (status, headers, body) responses, consumed one per request
        self.scripts: dict[str, list[tuple[int, dict, object]]] = {
            "second_factors": [], "login": [], "children": [],
        }

    def __call__(self, req: Request):
        if self.delay:
            time.sleep(self.delay)
        if req.method == "GET" and req.path.startswith("/api/v1/second-factors/"):
            if self.scripts["second_factors"]:
                return self.scripts["second_factors"].pop(0)
            factors = self.second_factors if self.two_factor else []
            return 200, {}, {"is_enabled": self.two_factor, "second_factors": factors}

        if req.method == "POST" and req.path == "/api/web/login/":
            if self.scripts["login"]:
                return self.scripts["login"].pop(0)
            body = req.json()
            self.login_bodies.append(body)
            if body.get("username") in self.users and self.users[body["username"]] == body.get("password"):
                self.valid_sessions.add(self.cookie_value)
                headers = {}
                if self.issue_cookie:
                    headers["Set-Cookie"] = (f"sessionid={self.cookie_value}; HttpOnly; "
                                             "Max-Age=1209600; Path=/; SameSite=Lax")
                return 200, headers, {"session_id": "opaque-token-not-the-cookie"}
            return 400, {}, {"detail": "invalid credentials"}

        if req.method == "GET" and req.path == "/api/v1/me/children/":
            if self.scripts["children"]:
                return self.scripts["children"].pop(0)
            jar = SimpleCookie(req.headers.get("cookie", ""))
            sid = jar["sessionid"].value if "sessionid" in jar else ""
            if sid in self.valid_sessions:
                return 200, {}, {"count": len(self.children), "results": self.children}
            return 401, {}, {"detail": "Authentication credentials were not provided."}

        return 404, {}, {"detail": "Not found."}
