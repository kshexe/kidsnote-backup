"""fetch.py's login block: fails fast and clearly, before any Notion work."""
from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

try:
    import fetch
except ImportError:  # pragma: no cover - requests not installed
    fetch = None

import kidsnote_auth as ka  # noqa: E402
from fake_http import FakeServer  # noqa: E402
from fake_kidsnote import SESSIONID, FakeKidsnote  # noqa: E402

USER, PASSWORD = "parent01", "Secret!23"
NOTION_MISSING = "NOTION_TOKEN / NOTION_DATABASE_ID missing"


@unittest.skipIf(fetch is None, "fetch.py dependencies (requests) not installed")
class FetchAuthTest(unittest.TestCase):
    def setUp(self):
        self.kn = FakeKidsnote({USER: PASSWORD})
        server = FakeServer(self.kn)
        server.__enter__()
        self.addCleanup(server.__exit__, None, None, None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.empty_env_file = Path(tmp.name, ".env")
        self.empty_env_file.write_text("", encoding="utf-8")
        for patcher in (mock.patch.object(ka, "KIDSNOTE_BASE", server.url),
                        mock.patch.object(ka, "_sleep", lambda s: None),
                        # the Notion side is out of scope; reaching it proves login passed
                        mock.patch.dict(sys.modules, {"notion_mirror": types.SimpleNamespace(NotionMirror=object)})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_fetch(self, **env):
        with mock.patch.dict(os.environ, {}):
            for key in list(os.environ):
                if key.startswith(("KIDSNOTE_", "NOTION_")):
                    del os.environ[key]
            os.environ.update(env)
            with self.assertLogs(level="INFO") as logs, self.assertRaises(SystemExit) as cm:
                fetch.main(["--publish-to-notion", "--no-local-save", "--env-file", str(self.empty_env_file)])
        return str(cm.exception.code), "\n".join(logs.output)

    def test_no_credentials(self):
        with mock.patch.dict(os.environ, {}):
            for key in list(os.environ):
                if key.startswith(("KIDSNOTE_", "NOTION_")):
                    del os.environ[key]
            with self.assertRaises(SystemExit) as cm:
                fetch.main(["--publish-to-notion", "--no-local-save", "--env-file", str(self.empty_env_file)])
        self.assertIn("Kidsnote credentials missing", str(cm.exception.code))

    def test_fresh_login_passes_before_notion_setup(self):
        code, logs = self.run_fetch(KIDSNOTE_USERNAME=USER, KIDSNOTE_PASSWORD=PASSWORD)
        self.assertIn(NOTION_MISSING, code)
        self.assertIn("Kidsnote session ready (fresh login)", logs)

    def test_ci_path_uses_session_from_login_step(self):
        self.kn.valid_sessions.add(SESSIONID)
        code, logs = self.run_fetch(KIDSNOTE_SESSION_COOKIE=SESSIONID)
        self.assertIn(NOTION_MISSING, code)
        self.assertIn("Kidsnote session ready (KIDSNOTE_SESSION_COOKIE)", logs)

    def test_wrong_password_stops_before_notion(self):
        with mock.patch.dict(os.environ, {}):
            for key in list(os.environ):
                if key.startswith(("KIDSNOTE_", "NOTION_")):
                    del os.environ[key]
            os.environ.update(KIDSNOTE_USERNAME=USER, KIDSNOTE_PASSWORD="wrong")
            with self.assertRaises(SystemExit) as cm:
                fetch.main(["--publish-to-notion", "--no-local-save", "--env-file", str(self.empty_env_file)])
        self.assertEqual(str(cm.exception.code),
                         "Kidsnote login failed (invalid_credentials): HTTP 400 on /api/web/login/")

    def test_wrong_password_with_valid_cookie_warns_and_continues(self):
        self.kn.valid_sessions.add(SESSIONID)
        code, logs = self.run_fetch(KIDSNOTE_USERNAME=USER, KIDSNOTE_PASSWORD="wrong",
                                    KIDSNOTE_SESSION_COOKIE=SESSIONID)
        self.assertIn(NOTION_MISSING, code)
        self.assertIn("login failed (invalid_credentials", logs)
        self.assertIn("Kidsnote session ready (KIDSNOTE_SESSION_COOKIE)", logs)


if __name__ == "__main__":
    unittest.main()
