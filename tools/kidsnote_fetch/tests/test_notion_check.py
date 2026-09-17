"""Tests for notion_check.py: the Notion half of the pre-run check."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import notion_check as nc  # noqa: E402
from fake_http import FakeServer  # noqa: E402
from fake_notion import NOTION_TOKEN, FakeNotion, dashed, hexid  # noqa: E402

PAGE, DB, VIEW = hexid(1), hexid(2), hexid(3)


class CheckNotionTest(unittest.TestCase):
    def setUp(self):
        self.notion = FakeNotion()
        self.notion.add_page(PAGE)
        self.notion.add_database(DB)
        self.server = FakeServer(self.notion)
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.sleeps: list[float] = []
        for patcher in (mock.patch.object(nc, "NOTION_API", self.server.url),
                        mock.patch.object(nc, "_sleep", self.sleeps.append),
                        mock.patch.object(nc, "REQUEST_TIMEOUT", 5)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def assertReason(self, reason, token, target):
        with self.assertRaises(nc.NotionCheckError) as cm:
            nc.check_notion(token, target)
        self.assertEqual(cm.exception.reason, reason, str(cm.exception))

    def test_database_link(self):
        self.assertEqual(nc.check_notion(NOTION_TOKEN, f"https://www.notion.so/myspace/{DB}?v={VIEW}"), "database")

    def test_page_share_link(self):
        self.assertEqual(nc.check_notion(NOTION_TOKEN, f"https://www.notion.so/키즈노트-백업-{PAGE}?source=copy_link"),
                         "page")
        self.assertEqual(self.server.paths(), ["GET /users/me", f"GET /databases/{PAGE}", f"GET /pages/{PAGE}",
                                               f"GET /blocks/{PAGE}/children"])

    def test_page_that_already_holds_the_backup_table(self):
        holder, table = hexid(5), hexid(6)
        self.notion.add_page(holder)
        self.notion.add_database(table, parent_page=holder)
        self.assertEqual(nc.check_notion(NOTION_TOKEN, holder), "page_with_database")

    def test_new_app_domain_link(self):
        self.assertEqual(nc.check_notion(NOTION_TOKEN, f"https://app.notion.com/p/{PAGE}?pvs=204"), "page")

    def test_messy_pasted_values(self):
        self.assertEqual(nc.check_notion(f" Bearer {NOTION_TOKEN}\n", f"﻿{dashed(PAGE)} "), "page")

    def test_sends_bearer_token_and_notion_version(self):
        nc.check_notion(NOTION_TOKEN, DB)
        req = self.server.requests[0]
        self.assertEqual(req.headers["authorization"], f"Bearer {NOTION_TOKEN}")
        self.assertEqual(req.headers["notion-version"], nc.NOTION_VERSION)

    def test_wrong_token(self):
        self.assertReason("notion_token_invalid", "ntn_wrong", DB)
        self.assertEqual(self.server.paths(), ["GET /users/me"])

    def test_token_with_impossible_characters_is_rejected_without_a_request(self):
        self.assertReason("notion_token_invalid", "노션토큰", DB)
        self.assertReason("notion_token_invalid", "ntn_abc def", DB)
        self.assertEqual(self.server.requests, [])

    def test_page_not_connected(self):
        self.assertReason("notion_no_access", NOTION_TOKEN, hexid(99))

    def test_missing_settings(self):
        self.assertReason("notion_missing", "", DB)
        self.assertReason("notion_missing", NOTION_TOKEN, "  \n")
        self.assertReason("notion_missing", None, None)
        self.assertEqual(self.server.requests, [])

    def test_value_without_any_notion_id(self):
        self.assertReason("notion_bad_id", NOTION_TOKEN, "키즈노트 백업 페이지")
        self.assertEqual(self.server.requests, [])

    def test_server_errors_are_retried_then_reported(self):
        self.notion.scripts = [(500, {}, {})] * 3
        self.assertReason("notion_server_error", NOTION_TOKEN, DB)
        self.assertEqual(self.sleeps, list(nc.RETRY_DELAYS))

    def test_transient_error_recovers(self):
        self.notion.scripts = [(503, {}, {})]
        self.assertEqual(nc.check_notion(NOTION_TOKEN, DB), "database")

    def test_rate_limited(self):
        self.notion.scripts = [(429, {}, {})] * 3
        self.assertReason("notion_rate_limited", NOTION_TOKEN, DB)

    def test_unreachable(self):
        with FakeServer(lambda req: (200, {}, {})) as other:
            dead = other.url
        with mock.patch.object(nc, "NOTION_API", dead):
            self.assertReason("notion_network", NOTION_TOKEN, DB)


class HintsTest(unittest.TestCase):
    def test_every_reason_has_a_single_line_hint(self):
        source = Path(nc.__file__).read_text(encoding="utf-8")
        reasons = set(re.findall(r'NotionCheckError\(\s*"([a-z_]+)"', source))
        self.assertTrue(reasons)
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertIn(reason, nc.HINTS)
        for reason, hint in nc.HINTS.items():
            with self.subTest(hint=reason):
                self.assertTrue(hint.strip())
                self.assertNotIn("\n", hint)
                self.assertNotIn("%", hint)


if __name__ == "__main__":
    unittest.main()
