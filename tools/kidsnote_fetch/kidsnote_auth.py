"""Kidsnote login helper — stdlib only.

Why this exists: kidsnote's `sessionid` cookie expires 14 days after login
(Set-Cookie header, verified 2026-09-14), so a cookie pasted into a repo
secret goes stale and every cron run fails until someone re-extracts it.
The web login page is a Next.js SPA, but underneath it calls a plain JSON
API that a headless client can drive, so each run can log in fresh.

Login flow, mirrored from the SPA's login chunk (2026-09-14):
  1. GET  /api/v1/second-factors/<username>/  -> {is_enabled, second_factors}
     With 2-step verification on, a phone/email code is required, which
     can't be automated -> fall back to KIDSNOTE_SESSION_COOKIE.
  2. POST /api/web/login/ {username, password, remember_me}
     -> 200 + Set-Cookie sessionid. No CSRF token needed. Logging in does
     not invalidate the account's other sessions (browser / app).

Transient failures (network, HTTP 5xx, 429) are retried a few times so a
short kidsnote hiccup doesn't become a failure email. Wrong credentials are
never retried: repeated failures can lock the account.

Stdlib-only on purpose: the workflow runs this before `pip install`, so a
failed login ends the run in seconds without installing anything.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import NamedTuple

KIDSNOTE_BASE = "https://www.kidsnote.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
REQUEST_TIMEOUT = 20  # seconds, per attempt
RETRY_DELAYS = (3, 10)  # waits between the 3 attempts at a transient failure
MAX_RETRY_AFTER = 30  # cap on a server-sent Retry-After, in seconds
_sleep = time.sleep  # swapped out in tests
# Waits before retrying a login that collided with another login for the
# same account (HTTP 409 "Already exists."), e.g. two children's jobs.
LOGIN_BUSY_DELAYS = (5, 10, 20)


def _jitter() -> float:
    """0-3 s spread so parallel jobs don't retry in lockstep (time-based, swapped out in tests)."""
    return (time.time_ns() % 3000) / 1000.0


# Shown in the Actions log / failure annotation. Plain Korean for parents.
# Single line each: they are written to $GITHUB_OUTPUT.
HINTS = {
    "invalid_credentials": "키즈노트 아이디 또는 비밀번호가 틀렸습니다. "
                           "KIDSNOTE_USERNAME / KIDSNOTE_PASSWORD 시크릿을 확인해 주세요.",
    "blocked": "로그인 실패가 반복돼 키즈노트 계정이 잠겼습니다. 키즈노트에서 비밀번호를 "
               "재설정한 뒤 KIDSNOTE_PASSWORD 시크릿을 새 비밀번호로 바꿔 주세요.",
    "2fa_enabled": "키즈노트 계정에 2단계 인증이 켜져 있어 자동 로그인을 할 수 없습니다. "
                   "2단계 인증을 끄거나 KIDSNOTE_SESSION_COOKIE 시크릿을 사용해 주세요.",
    "session_expired": "KIDSNOTE_SESSION_COOKIE가 만료됐거나 값이 올바르지 않습니다(쿠키는 로그인 후 14일). "
                       "KIDSNOTE_USERNAME / KIDSNOTE_PASSWORD 시크릿을 등록하면 더 이상 만료 걱정이 없습니다.",
    "missing": "키즈노트 로그인 정보가 없습니다. "
               "KIDSNOTE_USERNAME / KIDSNOTE_PASSWORD 시크릿을 등록해 주세요.",
    "rate_limited": "키즈노트에 요청이 너무 많아 잠시 막혔습니다. 다음 실행에서 자동으로 다시 시도합니다.",
    "login_busy": "같은 키즈노트 계정의 로그인이 동시에 진행돼 잠시 막혔습니다. 다음 실행에서 자동으로 다시 시도합니다.",
    "server_error": "키즈노트 서버에 일시적인 문제가 있습니다. 다음 실행에서 자동으로 다시 시도합니다.",
    "network": "키즈노트 서버에 접속하지 못했습니다. 일시적인 문제라면 다음 실행에서 자동으로 다시 시도합니다.",
    "unexpected": "키즈노트 로그인 응답이 예상과 다릅니다. 키즈노트 로그인 방식이 바뀌었을 수 있습니다.",
}

# The web login form cleans what the parent types before sending it (login
# page JS, 2026-09-14): id -> lower-case, first 32 chars, trimmed; password
# -> every whitespace and Korean character removed. Doing the same makes a
# secret behave exactly like typing it on kidsnote.com, and drops the stray
# newlines / BOMs that copy-pasted secrets often carry.
_BLANK = re.compile(r"[\s﻿]")  # JS \s matches U+FEFF (BOM); Python's \s doesn't
_TRIM = re.compile(r"^[\s﻿]+|[\s﻿]+$")
_KOREAN = re.compile(r"[ㄱ-ㅎ|ㅏ-ㅣ|가-힣]")  # the form's exact class, literal '|' included
_SESSIONID = re.compile(r"[A-Za-z0-9]+")


class AuthError(RuntimeError):
    """Login failed. ``reason`` is one of the HINTS keys."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class Session(NamedTuple):
    sessionid: str
    source: str  # "login" or "cookie"
    login_error: AuthError | None  # why id/password login failed, when the cookie was used instead


def normalize_username(value: str | None) -> str:
    return _TRIM.sub("", _TRIM.sub("", value or "").lower()[:32])


def normalize_password(value: str | None) -> str:
    return _KOREAN.sub("", _BLANK.sub("", value or ""))


def normalize_cookie(value: str | None) -> str:
    """Accept the bare value or a pasted 'sessionid=<value>; ...'; '' if it can't be a sessionid."""
    value = _BLANK.sub("", value or "").strip("\"'").split(";", 1)[0]
    if value.lower().startswith("sessionid="):
        value = value[len("sessionid="):].strip("\"'")
    return value if _SESSIONID.fullmatch(value) else ""


def _opener(user_agent: str, jar: CookieJar | None = None) -> urllib.request.OpenerDirector:
    handlers = [urllib.request.HTTPCookieProcessor(jar)] if jar is not None else []
    opener = urllib.request.build_opener(*handlers)
    opener.addheaders = [
        ("User-Agent", user_agent),
        ("Accept", "application/json, text/plain, */*"),
        ("Accept-Language", "ko"),
    ]
    return opener


def _retry_wait(retry_after: str | None, default: float) -> float:
    try:
        return min(max(float(retry_after), 0.0), MAX_RETRY_AFTER)
    except (TypeError, ValueError):
        return default


def _request(
    opener: urllib.request.OpenerDirector,
    method: str,
    path: str,
    *,
    body: dict | None = None,
    cookie: str = "",
) -> tuple[int, bytes]:
    """Send one request, retrying network errors, 5xx and 429. Returns (status, body)."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    attempts = len(RETRY_DELAYS) + 1
    for attempt in range(attempts):
        last = attempt == attempts - 1
        req = urllib.request.Request(KIDSNOTE_BASE + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
            req.add_header("Origin", KIDSNOTE_BASE)
            req.add_header("Referer", f"{KIDSNOTE_BASE}/kr/login")
        if cookie:
            req.add_header("Cookie", f"sessionid={cookie}")
        retry_after = None
        try:
            with opener.open(req, timeout=REQUEST_TIMEOUT) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            retry_after = e.headers.get("Retry-After") if e.headers is not None else None
            try:
                raw = e.read()
            except (OSError, http.client.HTTPException):
                raw = b""
            finally:
                e.close()
        except (OSError, http.client.HTTPException) as e:  # URLError, timeout, DNS, dropped connection
            if last:
                raise AuthError("network", f"kidsnote.com unreachable: {e}") from e
            _sleep(RETRY_DELAYS[attempt])
            continue
        if (status == 429 or status >= 500) and not last:
            _sleep(_retry_wait(retry_after, RETRY_DELAYS[attempt]))
            continue
        return status, raw
    raise AssertionError("unreachable")


def _json(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _status_error(status: int, where: str) -> AuthError:
    if status == 429:
        return AuthError("rate_limited", f"HTTP 429 on {where}")
    if status >= 500:
        return AuthError("server_error", f"HTTP {status} on {where}")
    return AuthError("unexpected", f"HTTP {status} on {where}")


def login(username: str, password: str, user_agent: str = DEFAULT_USER_AGENT) -> str:
    """Log in with id/password and return the new ``sessionid`` value."""
    username, password = normalize_username(username), normalize_password(password)
    if not username or not password:
        raise AuthError("missing", "KIDSNOTE_USERNAME / KIDSNOTE_PASSWORD is empty")
    jar = CookieJar()
    opener = _opener(user_agent, jar)

    quoted = urllib.parse.quote(username, safe="")
    status, raw = _request(opener, "GET", f"/api/v1/second-factors/{quoted}/")
    if status == 200:
        info = _json(raw)
        if info.get("is_enabled") and info.get("second_factors"):
            raise AuthError("2fa_enabled", "2-step verification is enabled on this account")
    # Any other status is not fatal: the SPA only uses this call to decide
    # whether to show the 2FA form, and the login call below reports errors.

    # kidsnote answers 409 "Already exists." while another login for the same
    # account is in flight (several children's jobs start together). A retry a
    # few seconds later succeeds, and both sessions stay valid (checked live).
    for attempt in range(len(LOGIN_BUSY_DELAYS) + 1):
        status, raw = _request(
            opener, "POST", "/api/web/login/",
            body={"username": username, "password": password, "remember_me": True},
        )
        if status != 409 or attempt == len(LOGIN_BUSY_DELAYS):
            break
        _sleep(LOGIN_BUSY_DELAYS[attempt] + _jitter())
    if status == 409:
        raise AuthError("login_busy", "HTTP 409 on /api/web/login/: another login for this account is in progress")
    if status != 200:
        if status == 429 or status >= 500:
            raise _status_error(status, "/api/web/login/")
        if _json(raw).get("err_code") == "blocked":
            raise AuthError("blocked", f"HTTP {status} on /api/web/login/: account blocked")
        if status in (400, 401, 403, 404):
            raise AuthError("invalid_credentials", f"HTTP {status} on /api/web/login/")
        raise _status_error(status, "/api/web/login/")

    sessionid = next((c.value for c in jar if c.name == "sessionid"), None) or ""
    if not _SESSIONID.fullmatch(sessionid):
        raise AuthError("unexpected", "login succeeded but kidsnote sent no usable sessionid cookie")
    return sessionid


def session_is_valid(sessionid: str, user_agent: str = DEFAULT_USER_AGENT) -> bool:
    """True if ``sessionid`` can read /api/v1/me/children/."""
    status, _ = _request(_opener(user_agent), "GET", "/api/v1/me/children/", cookie=sessionid)
    if status == 200:
        return True
    if status in (401, 403):
        return False
    raise _status_error(status, "/api/v1/me/children/")


def list_children(sessionid: str, user_agent: str = DEFAULT_USER_AGENT) -> list[dict]:
    """Children registered on the account (dicts with at least id and name)."""
    status, raw = _request(_opener(user_agent), "GET", "/api/v1/me/children/", cookie=sessionid)
    if status in (401, 403):
        raise AuthError("session_expired", "session was rejected while listing children")
    if status != 200:
        raise _status_error(status, "/api/v1/me/children/")
    data = _json(raw)
    results = data.get("results") if isinstance(data.get("results"), list) else data.get("children")
    return [c for c in (results or []) if isinstance(c, dict)]


def resolve_session(
    username: str | None,
    password: str | None,
    cookie: str | None,
    user_agent: str = DEFAULT_USER_AGENT,
) -> Session:
    """Get a working session: fresh id/password login first, KIDSNOTE_SESSION_COOKIE as fallback.

    Raises AuthError. When both fail the login error wins, since it is usually the
    actionable one, except for 2-step-verification accounts: they rely on the cookie
    on purpose, so "cookie expired" is what they need to hear.
    """
    login_error: AuthError | None = None
    if normalize_username(username) and normalize_password(password):
        try:
            sessionid = login(username, password, user_agent)
            if session_is_valid(sessionid, user_agent):
                return Session(sessionid, "login", None)
            login_error = AuthError("unexpected", "kidsnote rejected the session it had just issued")
        except AuthError as e:
            login_error = e

    if _TRIM.sub("", cookie or ""):
        sessionid = normalize_cookie(cookie)
        try:
            if sessionid and session_is_valid(sessionid, user_agent):
                return Session(sessionid, "cookie", login_error)
            cookie_error = AuthError("session_expired",
                                     "KIDSNOTE_SESSION_COOKIE is expired or is not a sessionid value")
        except AuthError as e:
            cookie_error = e
        if login_error is None or login_error.reason == "2fa_enabled":
            raise cookie_error
        raise login_error
    raise login_error or AuthError("missing", "no kidsnote credentials configured")


def _github_append(var: str, line: str) -> None:
    path = os.environ.get(var)
    if not path:
        raise SystemExit(f"--github needs ${var}; run it inside GitHub Actions")
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Log in to kidsnote and verify the session.")
    ap.add_argument(
        "--github", action="store_true",
        help="GitHub Actions mode: export the session to $GITHUB_ENV (masked) and "
             "write ok/reason/hint to $GITHUB_OUTPUT instead of exiting non-zero.",
    )
    args = ap.parse_args(argv)

    try:
        session = resolve_session(
            os.environ.get("KIDSNOTE_USERNAME"),
            os.environ.get("KIDSNOTE_PASSWORD"),
            os.environ.get("KIDSNOTE_SESSION_COOKIE"),
        )
    except AuthError as e:
        reason, detail = e.reason, str(e)
    except Exception as e:  # report it like a login failure (once) instead of crash-looping
        reason, detail = "unexpected", f"{type(e).__name__}: {e}"
    else:
        if args.github:
            print(f"::add-mask::{session.sessionid}")
        how = "fresh login" if session.source == "login" else "KIDSNOTE_SESSION_COOKIE"
        print(f"Kidsnote login OK ({how})")
        if session.login_error is not None:
            err = session.login_error
            message = (f"아이디/비밀번호 로그인에 실패해 KIDSNOTE_SESSION_COOKIE로 계속합니다. "
                       f"{HINTS.get(err.reason, HINTS['unexpected'])} [{err.reason}]")
            print(("::warning title=키즈노트 자동 로그인 실패::" if args.github else "WARNING: ") + message)
        if args.github:
            _github_append("GITHUB_ENV", f"KIDSNOTE_SESSION_COOKIE={session.sessionid}")
            _github_append("GITHUB_OUTPUT", "ok=true")
        return 0

    hint = HINTS.get(reason, HINTS["unexpected"])
    print(f"Kidsnote login FAILED [{reason}] {detail}\n{hint}")
    if args.github:
        _github_append("GITHUB_OUTPUT", "ok=false")
        _github_append("GITHUB_OUTPUT", f"reason={reason}")
        _github_append("GITHUB_OUTPUT", f"hint={hint}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
