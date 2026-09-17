"""Check the Notion half of the setup before a backup run (stdlib only).

Answers what first-time users get wrong most often, with a Korean hint
instead of an API error: is the token right, can the integration see the
page (was it connected), and does NOTION_DATABASE_ID point at a Notion page
or database at all. A page is fine: the backup creates its database inside
it on the first run.
"""
from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request

from secret_input import clean_secret, normalize_notion_id, normalize_token

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
REQUEST_TIMEOUT = 20  # seconds, per attempt
RETRY_DELAYS = (3, 10)  # waits between the 3 attempts at a transient failure
_sleep = time.sleep  # swapped out in tests

# Single line each: they are written to $GITHUB_OUTPUT.
HINTS = {
    "notion_missing": "노션 설정이 비어 있습니다. NOTION_TOKEN 과 NOTION_DATABASE_ID 시크릿을 등록해 주세요.",
    "notion_bad_id": "NOTION_DATABASE_ID 에서 노션 주소를 찾지 못했습니다. "
                     "노션 페이지의 링크(공유 → 링크 복사)를 통째로 다시 붙여넣어 주세요.",
    "notion_token_invalid": "노션 토큰(NOTION_TOKEN)이 올바르지 않습니다. "
                            "노션 연결(통합) 설정 화면에서 토큰을 다시 복사해 시크릿을 수정해 주세요.",
    "notion_no_access": "노션 페이지에 접근할 수 없습니다. 그 페이지에 연결(통합)을 추가했는지, "
                        "NOTION_DATABASE_ID 가 그 페이지의 링크인지 확인해 주세요.",
    "notion_rate_limited": "노션에 요청이 너무 많아 잠시 막혔습니다. 다음 실행에서 자동으로 다시 시도합니다.",
    "notion_server_error": "노션 서버에 일시적인 문제가 있습니다. 다음 실행에서 자동으로 다시 시도합니다.",
    "notion_network": "노션 서버에 접속하지 못했습니다. 일시적인 문제라면 다음 실행에서 자동으로 다시 시도합니다.",
    "notion_unexpected": "노션 응답이 예상과 다릅니다. 잠시 후 다시 실행해 보고, 계속되면 이슈로 알려 주세요.",
}


class NotionCheckError(RuntimeError):
    """The Notion setup can't work. ``reason`` is one of the HINTS keys."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _get(path: str, token: str) -> tuple[int, str]:
    """GET a Notion API path, retrying network errors, 5xx and 429. Returns (status, body)."""
    attempts = len(RETRY_DELAYS) + 1
    for attempt in range(attempts):
        last = attempt == attempts - 1
        req = urllib.request.Request(NOTION_API + path, headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "User-Agent": "kidsnote-backup-preflight",
        })
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                body = e.read().decode("utf-8", "replace")
            except (OSError, http.client.HTTPException):
                body = ""
            finally:
                e.close()
            if (status == 429 or status >= 500) and not last:
                _sleep(RETRY_DELAYS[attempt])
                continue
            return status, body
        except (OSError, http.client.HTTPException) as e:
            if last:
                raise NotionCheckError("notion_network", f"api.notion.com unreachable: {e}") from e
            _sleep(RETRY_DELAYS[attempt])
    raise AssertionError("unreachable")


def _status_error(status: int, where: str) -> NotionCheckError:
    if status == 429:
        return NotionCheckError("notion_rate_limited", f"HTTP 429 on {where}")
    if status >= 500:
        return NotionCheckError("notion_server_error", f"HTTP {status} on {where}")
    return NotionCheckError("notion_unexpected", f"HTTP {status} on {where}")


def _page_kind(page_id: str, token: str) -> str:
    """"page_with_database" if the page already holds an inline database (first 100 blocks), else "page"."""
    try:
        status, body = _get(f"/blocks/{page_id}/children?page_size=100", token)
        blocks = json.loads(body).get("results") or [] if status == 200 else []
    except (NotionCheckError, ValueError, AttributeError):
        return "page"
    return "page_with_database" if any(b.get("type") == "child_database" for b in blocks) else "page"


def check_notion(token_value: str | None, target_value: str | None) -> str:
    """Verify the token and target; raises NotionCheckError.

    Returns "database", "page_with_database" (a page already holding the
    backup table) or "page" (the backup creates its database there).
    """
    token = normalize_token(token_value)
    if not token or not clean_secret(target_value):
        raise NotionCheckError("notion_missing", "NOTION_TOKEN or NOTION_DATABASE_ID is empty")
    target = normalize_notion_id(target_value)
    if not target:
        raise NotionCheckError("notion_bad_id", "no Notion id found in NOTION_DATABASE_ID")
    if not token.isascii() or any(ch.isspace() for ch in token):
        raise NotionCheckError("notion_token_invalid", "token contains characters a Notion token never has")

    status, _ = _get("/users/me", token)
    if status in (401, 403):
        raise NotionCheckError("notion_token_invalid", f"HTTP {status} on /users/me")
    if status != 200:
        raise _status_error(status, "/users/me")

    status, body = _get(f"/databases/{target}", token)
    if status == 200:
        return "database"
    if status == 400 and ("is a page" in body or "page, not a database" in body):
        status, body = _get(f"/pages/{target}", token)
        if status == 200:
            return _page_kind(target, token)
    if status in (403, 404):
        raise NotionCheckError("notion_no_access", f"HTTP {status}: the integration can't see it")
    if status == 400:
        raise NotionCheckError("notion_bad_id", "HTTP 400: not a Notion page or database id")
    if status == 401:
        raise NotionCheckError("notion_token_invalid", "HTTP 401 on id lookup")
    raise _status_error(status, "id lookup")
