"""NotionMirror setup: whatever NOTION_DATABASE_ID points at becomes a usable backup database."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

try:
    import requests

    import notion_mirror as nm
except ImportError:  # pragma: no cover - requests not installed
    nm = None

from fake_http import FakeServer  # noqa: E402
from fake_notion import NOTION_TOKEN, FakeNotion, dashed, hexid, paragraph_block  # noqa: E402

PAGE, DB, VIEW = hexid(1), hexid(2), hexid(3)


@unittest.skipIf(nm is None, "requests not installed")
class MirrorSetupTest(unittest.TestCase):
    def setUp(self):
        self.notion = FakeNotion()
        self.server = FakeServer(self.notion)
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        patcher = mock.patch.object(nm, "NOTION_API", self.server.url)
        patcher.start()
        self.addCleanup(patcher.stop)

    def resolved(self, target, token=NOTION_TOKEN):
        mirror = nm.NotionMirror(token, target, session=requests.Session())
        mirror._resolve_schema()
        return mirror

    def test_database_link_is_used_as_is(self):
        self.notion.add_database(DB)
        m = self.resolved(f"https://www.notion.so/myspace/{DB}?v={VIEW}")
        self.assertEqual(m.database_id, DB)
        self.assertEqual((m._prop_title, m._prop_report_id, m._prop_date), ("이름", "Report ID", "날짜"))
        self.assertEqual((self.notion.created, self.notion.patched), ([], []))

    def test_messy_secrets_are_cleaned_before_any_request(self):
        self.notion.add_database(DB)
        m = self.resolved(f"﻿ {dashed(DB)}\n", token=f" {NOTION_TOKEN}\r\n")
        self.assertEqual((m.database_id, m.token), (DB, NOTION_TOKEN))

    def test_page_holding_an_inline_database_uses_that_database(self):
        self.notion.add_page(PAGE, [paragraph_block(1)])
        self.notion.add_database(DB, parent_page=PAGE)
        m = self.resolved(f"https://www.notion.so/키즈노트-백업-{PAGE}")
        self.assertEqual(m.database_id, DB)
        self.assertEqual(self.notion.created, [])

    def test_inline_database_further_down_a_long_page_is_found(self):
        self.notion.children_page_size = 2
        self.notion.add_page(PAGE, [paragraph_block(1), paragraph_block(2), paragraph_block(3)])
        self.notion.add_database(DB, parent_page=PAGE)
        m = self.resolved(PAGE)
        self.assertEqual(m.database_id, DB)
        self.assertEqual(self.notion.created, [])
        self.assertEqual(self.server.paths().count(f"GET /blocks/{PAGE}/children"), 2)

    def test_blank_page_gets_a_backup_database(self):
        self.notion.add_page(PAGE)
        m = self.resolved(f"https://www.notion.so/{PAGE}?source=copy_link")
        self.assertEqual(len(self.notion.created), 1)
        body = self.notion.created[0]
        self.assertEqual(body["parent"], {"type": "page_id", "page_id": PAGE})
        self.assertTrue(body["is_inline"])
        self.assertEqual(set(body["properties"]), {"이름", "날짜", "Report ID"})
        self.assertIn(m.database_id, self.notion.databases)
        self.assertEqual((m._prop_title, m._prop_report_id, m._prop_date), ("이름", "Report ID", "날짜"))

    def test_second_run_on_the_same_page_reuses_the_database(self):
        self.notion.add_page(PAGE)
        first = self.resolved(PAGE)
        second = self.resolved(PAGE)
        self.assertEqual(len(self.notion.created), 1)
        self.assertEqual(second.database_id, first.database_id)

    def test_missing_columns_are_added(self):
        self.notion.add_database(DB, properties={"이름": {"type": "title"}, "태그": {"type": "multi_select"}})
        m = self.resolved(DB)
        self.assertEqual(self.notion.patched, [
            (DB, {"properties": {"Report ID": {"number": {"format": "number"}}, "날짜": {"date": {}}}})])
        self.assertEqual((m._prop_report_id, m._prop_date), ("Report ID", "날짜"))

    def test_column_name_taken_by_another_type_gets_another_name(self):
        self.notion.add_database(DB, properties={"이름": {"type": "title"}, "Report ID": {"type": "rich_text"},
                                                 "날짜": {"type": "rich_text"}})
        m = self.resolved(DB)
        self.assertEqual(set(self.notion.patched[0][1]["properties"]), {"키즈노트 번호", "작성일"})
        self.assertEqual((m._prop_report_id, m._prop_date), ("키즈노트 번호", "작성일"))

    def test_wrong_token_is_explained_in_korean(self):
        self.notion.add_database(DB)
        with self.assertRaisesRegex(RuntimeError, "노션 토큰"):
            self.resolved(DB, token="ntn_wrong")

    def test_unconnected_page_is_explained_in_korean(self):
        with self.assertRaisesRegex(RuntimeError, "접근할 수 없습니다"):
            self.resolved(hexid(99))

    def test_database_creation_refused(self):
        self.notion.add_page(PAGE)
        self.notion.create_status = 403
        with self.assertRaisesRegex(RuntimeError, "만들지 못했습니다"):
            self.resolved(PAGE)

    def test_adding_columns_refused(self):
        self.notion.add_database(DB, properties={"이름": {"type": "title"}})
        self.notion.patch_status = 403
        with self.assertRaisesRegex(RuntimeError, "추가하지 못했습니다"):
            self.resolved(DB)


if __name__ == "__main__":
    unittest.main()
