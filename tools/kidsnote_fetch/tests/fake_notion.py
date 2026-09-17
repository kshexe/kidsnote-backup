"""Stateful stand-in for the handful of Notion endpoints the setup code touches."""
from __future__ import annotations

import re
import uuid

from fake_http import Request

NOTION_TOKEN = "ntn_test_token_0123456789abcdef"


def hexid(n: int) -> str:
    return f"{n:032x}"


def dashed(hex32: str) -> str:
    return str(uuid.UUID(hex32))


def child_database_block(db_id: str) -> dict:
    return {"object": "block", "id": dashed(db_id), "type": "child_database",
            "child_database": {"title": "키즈노트 백업"}}


def paragraph_block(n: int, text: str = "안녕하세요") -> dict:
    return {"object": "block", "id": dashed(hexid(9000 + n)), "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "plain_text": text, "text": {"content": text}}]}}


def _error(status: int, code: str, message: str):
    return status, {}, {"object": "error", "status": status, "code": code, "message": message}


class FakeNotion:
    def __init__(self, token: str = NOTION_TOKEN) -> None:
        self.token = token
        self.databases: dict[str, dict] = {}  # id -> {"properties": {name: {"type": ...}}}
        self.pages: dict[str, list[dict]] = {}  # id -> top-level child blocks
        self.scripts: list = []  # queued (status, headers, body) responses
        self.created: list[dict] = []  # POST /databases bodies
        self.patched: list[tuple[str, dict]] = []
        self.children_page_size = 100
        self.create_status = 200
        self.patch_status = 200
        self._next_id = 1000

    def add_page(self, page_id: str, blocks: list[dict] | None = None) -> None:
        self.pages[page_id] = list(blocks or [])

    def add_database(self, db_id: str, properties: dict | None = None, parent_page: str | None = None) -> None:
        if properties is None:
            properties = {"이름": {"type": "title"}, "날짜": {"type": "date"}, "Report ID": {"type": "number"}}
        self.databases[db_id] = {"properties": dict(properties)}
        if parent_page is not None:
            self.pages.setdefault(parent_page, []).append(child_database_block(db_id))

    def __call__(self, req: Request):
        if self.scripts:
            return self.scripts.pop(0)
        if req.headers.get("authorization") != f"Bearer {self.token}":
            return _error(401, "unauthorized", "API token is invalid.")
        path, method = req.path, req.method

        if path == "/users/me" and method == "GET":
            return 200, {}, {"object": "user", "type": "bot"}

        if path == "/databases" and method == "POST":
            body = req.json()
            self.created.append(body)
            if self.create_status != 200:
                return _error(self.create_status, "restricted_resource", "Insufficient permissions.")
            self._next_id += 1
            new_id = hexid(self._next_id)
            self.databases[new_id] = {"properties": {n: {"type": next(iter(cfg))}
                                                     for n, cfg in body["properties"].items()}}
            parent = body["parent"]["page_id"].replace("-", "")
            self.pages.setdefault(parent, []).append(child_database_block(new_id))
            return 200, {}, {"object": "database", "id": dashed(new_id)}

        m = re.fullmatch(r"/databases/([0-9a-f]{32})", path)
        if m:
            oid = m.group(1)
            if method == "GET":
                if oid in self.databases:
                    props = {n: {"id": n, "name": n, **meta}
                             for n, meta in self.databases[oid]["properties"].items()}
                    return 200, {}, {"object": "database", "id": dashed(oid), "properties": props}
                if oid in self.pages:
                    return _error(400, "validation_error",
                                  f"Provided ID {dashed(oid)} is a page, not a database. "
                                  "Use the retrieve page API instead.")
                return _error(404, "object_not_found", f"Could not find database with ID: {dashed(oid)}.")
            if method == "PATCH":
                body = req.json()
                self.patched.append((oid, body))
                if self.patch_status != 200:
                    return _error(self.patch_status, "restricted_resource", "Insufficient permissions.")
                for n, cfg in (body.get("properties") or {}).items():
                    self.databases[oid]["properties"][n] = {"type": next(iter(cfg))}
                return 200, {}, {"object": "database", "id": dashed(oid)}

        m = re.fullmatch(r"/pages/([0-9a-f]{32})", path)
        if m and method == "GET":
            if m.group(1) in self.pages:
                return 200, {}, {"object": "page", "id": dashed(m.group(1))}
            return _error(404, "object_not_found", "Could not find page.")

        m = re.fullmatch(r"/blocks/([0-9a-f]{32})/children", path)
        if m and method == "GET":
            if m.group(1) not in self.pages:
                return _error(404, "object_not_found", "Could not find block.")
            blocks = self.pages[m.group(1)]
            start = int((req.query.get("start_cursor") or ["0"])[0])
            size = min(int((req.query.get("page_size") or ["100"])[0]), self.children_page_size)
            more = start + size < len(blocks)
            return 200, {}, {"object": "list", "results": blocks[start:start + size], "has_more": more,
                             "next_cursor": str(start + size) if more else None}

        return _error(400, "invalid_request_url", "Invalid request URL.")
