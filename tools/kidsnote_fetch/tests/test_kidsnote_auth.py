"""Tests for kidsnote_auth.py against a fake kidsnote server (no network).

Set KIDSNOTE_LIVE_TEST=1 to also run a few checks against the real
kidsnote.com (a made-up id, plus KIDSNOTE_USERNAME/PASSWORD if present).
"""
from __future__ import annotations

import io
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import kidsnote_auth as ka  # noqa: E402
from fake_http import FakeServer  # noqa: E402
from fake_kidsnote import SESSIONID, FakeKidsnote  # noqa: E402

USER, PASSWORD = "parent01", "Secret!23"
SF_PATH = f"GET /api/v1/second-factors/{USER}/"
LOGIN_PATH = "POST /api/web/login/"
CHILDREN_PATH = "GET /api/v1/me/children/"


class KidsnoteTestCase(unittest.TestCase):
    """Starts a fake kidsnote, points the module at it, and records retry sleeps."""

    def setUp(self):
        self.kn = FakeKidsnote({USER: PASSWORD})
        self.server = FakeServer(self.kn)
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.sleeps: list[float] = []
        for patcher in (mock.patch.object(ka, "KIDSNOTE_BASE", self.server.url),
                        mock.patch.object(ka, "_sleep", self.sleeps.append),
                        mock.patch.object(ka, "REQUEST_TIMEOUT", 5)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def count(self, path):
        return self.server.paths().count(path)

    def assertAuthError(self, reason, func, *args):
        with self.assertRaises(ka.AuthError) as cm:
            func(*args)
        self.assertEqual(cm.exception.reason, reason, str(cm.exception))
        return cm.exception


class NormalizeTest(unittest.TestCase):
    def test_username_is_cleaned_like_the_web_form(self):
        self.assertEqual(ka.normalize_username("  Parent01 \n"), "parent01")
        self.assertEqual(ka.normalize_username("﻿parent01\r\n"), "parent01")
        self.assertEqual(ka.normalize_username("A" * 40), "a" * 32)
        self.assertEqual(ka.normalize_username(""), "")
        self.assertEqual(ka.normalize_username(None), "")

    def test_password_is_cleaned_like_the_web_form(self):
        self.assertEqual(ka.normalize_password(" Sec ret\t!23\r\n"), "Secret!23")
        self.assertEqual(ka.normalize_password("﻿Secret!23"), "Secret!23")
        self.assertEqual(ka.normalize_password("abc한글ㅎㅏ123"), "abc123")
        self.assertEqual(ka.normalize_password("a|b"), "ab")  # the form's character class drops '|' too
        self.assertEqual(ka.normalize_password(None), "")

    def test_password_keeps_symbols_and_other_scripts(self):
        symbols = "!@#$%^&*()_+-=[]{};':\",./<>?`~\\"
        self.assertEqual(ka.normalize_password(symbols), symbols)
        self.assertEqual(ka.normalize_password("Pässwörd日本"), "Pässwörd日本")

    def test_cookie_accepts_common_paste_forms(self):
        for pasted in (SESSIONID, f"  {SESSIONID}\n", f"﻿{SESSIONID}", f'"{SESSIONID}"',
                       f"sessionid={SESSIONID}", f"sessionid={SESSIONID}; csrftoken=abc",
                       f"SessionID = {SESSIONID};"):
            with self.subTest(pasted=pasted):
                self.assertEqual(ka.normalize_cookie(pasted), SESSIONID)

    def test_cookie_rejects_values_that_cannot_be_a_sessionid(self):
        for bad in ("", None, "   ", "abc-def", "abc\r\nX-Injected: 1", "쿠키", "sessionid="):
            with self.subTest(bad=bad):
                self.assertEqual(ka.normalize_cookie(bad), "")


class LoginTest(KidsnoteTestCase):
    def test_success_sends_what_the_web_form_sends(self):
        self.assertEqual(ka.login(" Parent01\n", " Secret!23\n"), SESSIONID)
        self.assertEqual(self.server.paths(), [SF_PATH, LOGIN_PATH])
        self.assertEqual(self.kn.login_bodies, [{"username": USER, "password": PASSWORD, "remember_me": True}])
        post = self.server.requests[1]
        self.assertEqual(post.headers["content-type"], "application/json")
        self.assertIn("Mozilla", post.headers["user-agent"])
        self.assertEqual(self.sleeps, [])

    def test_special_characters_in_password_are_sent_verbatim(self):
        tricky = 'p@ss"w\\ord!#%&<>{}日本'
        self.kn.users[USER] = tricky
        self.assertEqual(ka.login(USER, tricky), SESSIONID)
        self.assertEqual(self.kn.login_bodies[-1]["password"], tricky)

    def test_username_is_url_quoted_in_second_factor_lookup(self):
        self.kn.users["a b"] = PASSWORD
        self.assertEqual(ka.login("a b", PASSWORD), SESSIONID)
        self.assertEqual(self.server.requests[0].path, "/api/v1/second-factors/a%20b/")

    def test_two_factor_account_stops_before_sending_password(self):
        self.kn.two_factor = True
        self.assertAuthError("2fa_enabled", ka.login, USER, PASSWORD)
        self.assertEqual(self.count(LOGIN_PATH), 0)

    def test_two_factor_flag_without_factors_logs_in_normally(self):
        self.kn.two_factor, self.kn.second_factors = True, []
        self.assertEqual(ka.login(USER, PASSWORD), SESSIONID)

    def test_second_factor_lookup_failure_is_not_fatal(self):
        self.kn.scripts["second_factors"] = [(404, {}, {"detail": "no"})]
        self.assertEqual(ka.login(USER, PASSWORD), SESSIONID)
        self.kn.scripts["second_factors"] = [(500, {}, "")] * 3
        self.assertEqual(ka.login(USER, PASSWORD), SESSIONID)
        self.assertEqual(self.sleeps, list(ka.RETRY_DELAYS))

    def test_wrong_password_is_reported_and_never_retried(self):
        self.assertAuthError("invalid_credentials", ka.login, USER, "wrong")
        self.assertEqual(self.count(LOGIN_PATH), 1)
        self.assertEqual(self.sleeps, [])

    def test_other_client_errors_are_invalid_credentials_without_retry(self):
        for status in (401, 403, 404):
            with self.subTest(status=status):
                self.server.requests.clear()
                self.kn.scripts["login"] = [(status, {}, {})]
                self.assertAuthError("invalid_credentials", ka.login, USER, PASSWORD)
                self.assertEqual(self.count(LOGIN_PATH), 1)

    def test_non_json_error_body(self):
        self.kn.scripts["login"] = [(400, {"Content-Type": "text/html"}, "<html>oops</html>")]
        self.assertAuthError("invalid_credentials", ka.login, USER, PASSWORD)

    def test_blocked_account(self):
        self.kn.scripts["login"] = [(403, {}, {"err_code": "blocked"})]
        self.assertAuthError("blocked", ka.login, USER, PASSWORD)
        self.assertEqual(self.count(LOGIN_PATH), 1)

    def test_rate_limit_is_retried_then_reported(self):
        self.kn.scripts["login"] = [(429, {}, {})] * 3
        self.assertAuthError("rate_limited", ka.login, USER, PASSWORD)
        self.assertEqual(self.count(LOGIN_PATH), 3)
        self.assertEqual(self.sleeps, list(ka.RETRY_DELAYS))

    def test_retry_after_is_respected_and_capped(self):
        self.kn.scripts["login"] = [(429, {"Retry-After": "7"}, {}), (429, {"Retry-After": "999"}, {})]
        self.assertEqual(ka.login(USER, PASSWORD), SESSIONID)
        self.assertEqual(self.sleeps, [7.0, float(ka.MAX_RETRY_AFTER)])

    def test_server_error_recovers_on_retry(self):
        self.kn.scripts["login"] = [(503, {}, "")]
        self.assertEqual(ka.login(USER, PASSWORD), SESSIONID)
        self.assertEqual(self.sleeps, [ka.RETRY_DELAYS[0]])

    def test_persistent_server_error(self):
        self.kn.scripts["login"] = [(500, {}, "")] * 3
        self.assertAuthError("server_error", ka.login, USER, PASSWORD)

    def test_unexpected_status_is_not_retried(self):
        self.kn.scripts["login"] = [(418, {}, {"detail": "teapot"})]
        self.assertAuthError("unexpected", ka.login, USER, PASSWORD)
        self.assertEqual(self.count(LOGIN_PATH), 1)

    def test_concurrent_login_collision_is_retried(self):
        # Seen live when two children's jobs log in at the same moment.
        self.kn.scripts["login"] = [(409, {}, {"detail": "Already exists."})]
        with mock.patch.object(ka, "_jitter", lambda: 0.0):
            self.assertEqual(ka.login(USER, PASSWORD), SESSIONID)
        self.assertEqual(self.count(LOGIN_PATH), 2)
        self.assertEqual(self.sleeps, [ka.LOGIN_BUSY_DELAYS[0]])

    def test_persistent_collision_is_login_busy(self):
        self.kn.scripts["login"] = [(409, {}, {"detail": "Already exists."})] * (len(ka.LOGIN_BUSY_DELAYS) + 1)
        with mock.patch.object(ka, "_jitter", lambda: 0.0):
            self.assertAuthError("login_busy", ka.login, USER, PASSWORD)
        self.assertEqual(self.count(LOGIN_PATH), len(ka.LOGIN_BUSY_DELAYS) + 1)
        self.assertEqual(self.sleeps, list(ka.LOGIN_BUSY_DELAYS))

    def test_jitter_stays_small(self):
        for _ in range(5):
            self.assertTrue(0.0 <= ka._jitter() < 3.0)

    def test_success_without_cookie_is_unexpected(self):
        self.kn.issue_cookie = False
        self.assertAuthError("unexpected", ka.login, USER, PASSWORD)

    def test_malformed_cookie_is_unexpected(self):
        self.kn.cookie_value = "not-a-session-id"
        self.assertAuthError("unexpected", ka.login, USER, PASSWORD)

    def test_empty_credentials_make_no_request(self):
        self.assertAuthError("missing", ka.login, "", PASSWORD)
        self.assertAuthError("missing", ka.login, USER, "  \n")
        self.assertEqual(self.server.requests, [])

    def test_unreachable_server_is_network_after_retries(self):
        with FakeServer(lambda req: (200, {}, {})) as other:
            dead = other.url
        with mock.patch.object(ka, "KIDSNOTE_BASE", dead):
            self.assertAuthError("network", ka.login, USER, PASSWORD)
        self.assertEqual(self.sleeps, list(ka.RETRY_DELAYS))

    def test_timeout_is_network(self):
        self.kn.delay = 1.0
        with mock.patch.object(ka, "REQUEST_TIMEOUT", 0.2):
            self.assertAuthError("network", ka.login, USER, PASSWORD)


class SessionIsValidTest(KidsnoteTestCase):
    def test_valid_session(self):
        self.kn.valid_sessions.add(SESSIONID)
        self.assertTrue(ka.session_is_valid(SESSIONID))
        self.assertEqual(self.server.requests[0].headers["cookie"], f"sessionid={SESSIONID}")

    def test_rejected_session(self):
        self.assertFalse(ka.session_is_valid(SESSIONID))
        self.kn.scripts["children"] = [(403, {}, {})]
        self.assertFalse(ka.session_is_valid(SESSIONID))

    def test_server_trouble_raises_instead_of_calling_it_expired(self):
        self.kn.scripts["children"] = [(502, {}, "")] * 3
        self.assertAuthError("server_error", ka.session_is_valid, SESSIONID)
        self.kn.scripts["children"] = [(429, {}, "")] * 3
        self.assertAuthError("rate_limited", ka.session_is_valid, SESSIONID)
        self.kn.scripts["children"] = [(418, {}, "")]
        self.assertAuthError("unexpected", ka.session_is_valid, SESSIONID)


class ResolveSessionTest(KidsnoteTestCase):
    GOOD_COOKIE = "c00k1ec00k1ec00k1ec00k1ec00k1e00"

    def setUp(self):
        super().setUp()
        self.kn.valid_sessions.add(self.GOOD_COOKIE)

    def test_fresh_login_is_preferred_and_verified(self):
        session = ka.resolve_session(USER, PASSWORD, self.GOOD_COOKIE)
        self.assertEqual(session, ka.Session(SESSIONID, "login", None))
        self.assertEqual(self.server.paths(), [SF_PATH, LOGIN_PATH, CHILDREN_PATH])

    def test_wrong_password_falls_back_to_valid_cookie_and_keeps_the_reason(self):
        session = ka.resolve_session(USER, "wrong", self.GOOD_COOKIE)
        self.assertEqual((session.sessionid, session.source), (self.GOOD_COOKIE, "cookie"))
        self.assertEqual(session.login_error.reason, "invalid_credentials")

    def test_two_factor_falls_back_to_cookie(self):
        self.kn.two_factor = True
        session = ka.resolve_session(USER, PASSWORD, f"sessionid={self.GOOD_COOKIE};")
        self.assertEqual(session.source, "cookie")
        self.assertEqual(session.login_error.reason, "2fa_enabled")

    def test_login_error_wins_when_cookie_also_fails(self):
        self.assertAuthError("invalid_credentials", ka.resolve_session, USER, "wrong", "0" * 32)

    def test_two_factor_account_with_expired_cookie_hears_cookie_expired(self):
        self.kn.two_factor = True
        self.assertAuthError("session_expired", ka.resolve_session, USER, PASSWORD, "0" * 32)
        self.assertAuthError("2fa_enabled", ka.resolve_session, USER, PASSWORD, "")

    def test_cookie_only(self):
        self.assertEqual(ka.resolve_session(None, None, self.GOOD_COOKIE),
                         ka.Session(self.GOOD_COOKIE, "cookie", None))
        self.assertAuthError("session_expired", ka.resolve_session, "", "", "0" * 32)

    def test_garbage_cookie_is_rejected_without_sending_it(self):
        self.assertAuthError("session_expired", ka.resolve_session, "", "", "이건 쿠키가 아님; x=1")
        self.assertEqual(self.server.requests, [])

    def test_nothing_configured_is_missing(self):
        for args in (("", "", ""), (None, None, None), ("  ", "\n", " ﻿ "), (USER, "", ""), ("", PASSWORD, "")):
            with self.subTest(args=args):
                self.assertAuthError("missing", ka.resolve_session, *args)
        self.assertEqual(self.server.requests, [])

    def test_issued_session_rejected_without_cookie_is_unexpected(self):
        self.kn.scripts["children"] = [(401, {}, {})]
        self.assertAuthError("unexpected", ka.resolve_session, USER, PASSWORD, "")

    def test_kidsnote_down_is_network(self):
        with FakeServer(lambda req: (200, {}, {})) as other:
            dead = other.url
        with mock.patch.object(ka, "KIDSNOTE_BASE", dead):
            self.assertAuthError("network", ka.resolve_session, USER, PASSWORD, self.GOOD_COOKIE)


class MainTest(KidsnoteTestCase):
    """The workflow entry point: outputs, masking, and never leaking secrets."""

    def run_main(self, *, github, username=None, password=None, cookie=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env_file, out_file = Path(tmp.name, "env"), Path(tmp.name, "output")
        env_file.touch()
        out_file.touch()
        overrides = {"GITHUB_ENV": str(env_file), "GITHUB_OUTPUT": str(out_file)}
        for key, value in (("KIDSNOTE_USERNAME", username), ("KIDSNOTE_PASSWORD", password),
                           ("KIDSNOTE_SESSION_COOKIE", cookie)):
            if value is not None:
                overrides[key] = value
        buf = io.StringIO()
        with mock.patch.dict(os.environ, overrides):
            for key in ("KIDSNOTE_USERNAME", "KIDSNOTE_PASSWORD", "KIDSNOTE_SESSION_COOKIE"):
                if key not in overrides:
                    os.environ.pop(key, None)
            with redirect_stdout(buf):
                code = ka.main(["--github"] if github else [])
        return (code, buf.getvalue(), env_file.read_text(encoding="utf-8"),
                out_file.read_text(encoding="utf-8"))

    def test_github_success_exports_masked_session(self):
        code, out, env, output = self.run_main(github=True, username=USER, password=PASSWORD)
        self.assertEqual(code, 0)
        self.assertEqual(env, f"KIDSNOTE_SESSION_COOKIE={SESSIONID}\n")
        self.assertEqual(output, "ok=true\n")
        lines_with_sid = [line for line in out.splitlines() if SESSIONID in line]
        self.assertEqual(lines_with_sid, [f"::add-mask::{SESSIONID}"])
        self.assertTrue(out.startswith(f"::add-mask::{SESSIONID}\n"), "mask must come before anything else")
        self.assertIn("Kidsnote login OK (fresh login)", out)
        self.assertNotIn(PASSWORD, out)

    def test_github_failure_writes_single_line_outputs_and_exits_zero(self):
        code, out, env, output = self.run_main(github=True, username=USER, password="Wrong!Pass9")
        self.assertEqual(code, 0)
        self.assertEqual(env, "")
        self.assertEqual(output.splitlines(), [
            "ok=false", "reason=invalid_credentials", f"hint={ka.HINTS['invalid_credentials']}"])
        self.assertIn("Kidsnote login FAILED [invalid_credentials]", out)
        self.assertNotIn("Wrong!Pass9", out)

    def test_github_cookie_fallback_warns_about_login(self):
        self.kn.valid_sessions.add("c00k1e" * 5 + "ab")
        code, out, env, output = self.run_main(github=True, username=USER, password="nope",
                                               cookie="c00k1e" * 5 + "ab")
        self.assertEqual((code, output), (0, "ok=true\n"))
        self.assertIn("::warning title=키즈노트 자동 로그인 실패::", out)
        self.assertIn("[invalid_credentials]", out)
        self.assertEqual(env, f"KIDSNOTE_SESSION_COOKIE={'c00k1e' * 5 + 'ab'}\n")

    def test_github_missing_everything(self):
        code, out, env, output = self.run_main(github=True)
        self.assertEqual(code, 0)
        self.assertIn("reason=missing", output)

    def test_local_mode_exit_codes_and_no_session_printed(self):
        code, out, _, _ = self.run_main(github=False, username=USER, password=PASSWORD)
        self.assertEqual(code, 0)
        self.assertNotIn(SESSIONID, out)
        code, out, _, _ = self.run_main(github=False, username=USER, password="wrong")
        self.assertEqual(code, 1)

    def test_crash_is_reported_as_unexpected_not_raised(self):
        with mock.patch.object(ka, "resolve_session", side_effect=ValueError("boom")):
            code, out, env, output = self.run_main(github=True, username=USER, password=PASSWORD)
            self.assertEqual(code, 0)
            self.assertIn("reason=unexpected", output)
            self.assertIn("ValueError: boom", out)
            code, _, _, _ = self.run_main(github=False, username=USER, password=PASSWORD)
            self.assertEqual(code, 1)


class ListChildrenTest(KidsnoteTestCase):
    def test_lists_children_for_a_valid_session(self):
        self.kn.valid_sessions.add(SESSIONID)
        self.kn.children = [{"id": 1, "name": "첫째"}, {"id": 2, "name": "둘째"}]
        self.assertEqual([c["id"] for c in ka.list_children(SESSIONID)], [1, 2])

    def test_rejected_session(self):
        self.assertAuthError("session_expired", ka.list_children, SESSIONID)

    def test_server_trouble(self):
        self.kn.scripts["children"] = [(502, {}, "")] * 3
        self.assertAuthError("server_error", ka.list_children, SESSIONID)


class HintsTest(unittest.TestCase):
    def test_every_reason_raised_has_a_single_line_hint(self):
        source = Path(ka.__file__).read_text(encoding="utf-8")
        reasons = set(re.findall(r'AuthError\(\s*"([a-z0-9_]+)"', source)) | {"rate_limited", "server_error"}
        self.assertTrue(reasons)
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertIn(reason, ka.HINTS)
        for reason, hint in ka.HINTS.items():
            with self.subTest(hint=reason):
                self.assertTrue(hint.strip())
                self.assertNotIn("\n", hint)
                self.assertNotIn("\r", hint)
                self.assertNotIn("%", hint)


@unittest.skipUnless(os.environ.get("KIDSNOTE_LIVE_TEST") == "1", "set KIDSNOTE_LIVE_TEST=1 to hit kidsnote.com")
class LiveKidsnoteTest(unittest.TestCase):
    def test_made_up_account_is_invalid_credentials(self):
        with self.assertRaises(ka.AuthError) as cm:
            ka.login("zz_nonexist_test_48213", "wrong-pass-1")
        self.assertEqual(cm.exception.reason, "invalid_credentials")

    @unittest.skipUnless(os.environ.get("KIDSNOTE_USERNAME") and os.environ.get("KIDSNOTE_PASSWORD"),
                         "KIDSNOTE_USERNAME / KIDSNOTE_PASSWORD not set")
    def test_real_account_logs_in(self):
        session = ka.resolve_session(os.environ["KIDSNOTE_USERNAME"], os.environ["KIDSNOTE_PASSWORD"], "")
        self.assertEqual(session.source, "login")
        self.assertRegex(session.sessionid, r"^[A-Za-z0-9]+$")


if __name__ == "__main__":
    unittest.main()
