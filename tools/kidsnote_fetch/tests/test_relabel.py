"""Tests for relabel.py: rewriting label text on already-published report pages in place."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

try:
    import requests

    import notion_mirror as nm
    import relabel
except ImportError:  # pragma: no cover - requests not installed
    nm = relabel = None

from fake_http import FakeServer  # noqa: E402

REPORT = {
    "id": 1332843862,
    "author": {"type": "teacher"},
    "author_name": "새싹2반 교사",
    "class_name": "새싹2반",
    "date_written": "2026-01-09",
    "meal_status": "fixed",
    "sleep_hour": "1_to_1.5",
    "bowel_status": "watery",
    "temperature_status": "normal",
    "weather": "mostly_cloudy",
    "bowel": [{"time_bowel": "10:00", "status": "hard"}, {"time_bowel": "16:20", "status": "watery"}],
    "sleep": [{"time_start": "12:30", "time_end": "14:00"}],
}
NEW_META = ("👩‍🏫 선생님 새싹2반 교사 · 새싹2반 · 작성 2026-01-09 · 🍽️ 식사 정량 · "
            "💤 수면 1시간~1시간30분 · 💩 배변 묽음 · 🌡️ 체온 정상")
PLAIN = {"bold": False, "italic": False, "strikethrough": False, "underline": False,
         "code": False, "color": "default"}
GRAY = dict(PLAIN, color="gray")


def seg(text, annotations=PLAIN):
    return {"type": "text", "text": {"content": text, "link": None},
            "annotations": dict(annotations), "plain_text": text, "href": None}


def block(bid, btype, text, annotations=PLAIN, **extra):
    return {"object": "block", "id": bid, "type": btype, "has_children": False,
            btype: {"rich_text": [seg(text, annotations)], **extra}}


def published_page(report, variant, prefix=""):
    """Top-level blocks as a version of the tool published them, in Notion's GET shape."""
    r = relabel.render(report, variant)
    blocks = [block(prefix + "meta", "paragraph",
                    " · ".join(nm.NotionMirror._meta_bits(report) + r.chips), GRAY)]
    if r.weather:
        blocks.append(block(prefix + "weather", "callout", r.weather,
                            icon={"type": "emoji", "emoji": "🌤️"}, color="blue_background"))
    blocks.append(block(prefix + "body", "paragraph", "오늘은 블록 놀이를 했어요."))
    blocks.append(block(prefix + "h-sleep", "heading_3", "💤 낮잠"))
    blocks.append(block(prefix + "sleep-0", "paragraph", "12:30 ~ 14:00"))
    if r.bowel_lines:
        blocks.append(block(prefix + "h-bowel", "heading_3", relabel.BOWEL_HEADING))
        blocks += [block(f"{prefix}bowel-{i}", "paragraph", line) for i, line in enumerate(r.bowel_lines)]
    blocks.append(block(prefix + "h-photo", "heading_3", "사진"))
    blocks.append({"object": "block", "id": prefix + "img", "type": "image",
                   "image": {"type": "file"}, "has_children": False})
    return blocks


def set_text(blocks, bid, text):
    for b in blocks:
        if b["id"] == bid:
            s = b[b["type"]]["rich_text"][0]
            s["text"]["content"] = s["plain_text"] = text


def apply(blocks, plan):
    out = copy.deepcopy(blocks)
    for bid, body in plan.updates:
        for b in out:
            if b["id"] == bid:
                new_seg = dict(body[b["type"]]["rich_text"][0])
                new_seg["plain_text"] = new_seg["text"]["content"]
                b[b["type"]]["rich_text"] = [new_seg]
    return [b for b in out if b["id"] not in plan.deletes]


def texts(blocks):
    return {b["id"]: relabel._block_text(b) for b in blocks}


@unittest.skipIf(relabel is None, "requests not installed")
class PlanPageTest(unittest.TestCase):
    def test_legacy_page_is_rewritten_to_kidsnote_wording(self):
        old = published_page(REPORT, "legacy")
        self.assertIn("💩 배변 watery", texts(old)["meta"])
        self.assertIn("정해진 식단", texts(old)["meta"])
        plan = relabel.plan_page(old, REPORT)
        self.assertEqual(plan.skipped, [])
        self.assertEqual({bid for bid, _ in plan.updates}, {"meta", "weather", "bowel-0", "bowel-1"})
        fixed = texts(apply(old, plan))
        self.assertEqual(fixed["meta"], NEW_META)
        self.assertEqual(fixed["weather"], "오늘의 날씨: 🌥️ 구름많음")
        self.assertEqual([fixed["bowel-0"], fixed["bowel-1"]], ["10:00  딱딱함", "16:20  묽음"])
        self.assertEqual(fixed["body"], "오늘은 블록 놀이를 했어요.")
        self.assertEqual(fixed["sleep-0"], "12:30 ~ 14:00")

    def test_second_run_changes_nothing(self):
        old = published_page(REPORT, "legacy")
        once = apply(old, relabel.plan_page(old, REPORT))
        again = relabel.plan_page(once, REPORT)
        self.assertFalse(again.changed)
        self.assertEqual(again.skipped, [])

    def test_raw_code_page_becomes_the_current_rendering(self):
        old = published_page(REPORT, "raw")
        self.assertIn("🍽️ 식사 fixed", texts(old)["meta"])
        self.assertEqual(texts(apply(old, relabel.plan_page(old, REPORT))), texts(published_page(REPORT, "new")))

    def test_page_mixing_old_versions(self):
        old = published_page(REPORT, "legacy")
        set_text(old, "meta", texts(old)["meta"].replace("정해진 식단", "fixed"))
        self.assertEqual(texts(apply(old, relabel.plan_page(old, REPORT)))["meta"], NEW_META)

    def test_current_page_needs_nothing(self):
        plan = relabel.plan_page(published_page(REPORT, "new"), REPORT)
        self.assertFalse(plan.changed)
        self.assertEqual(plan.skipped, [])

    def test_patch_keeps_block_type_and_gray_color(self):
        updates = dict(relabel.plan_page(published_page(REPORT, "legacy"), REPORT).updates)
        self.assertEqual(list(updates["meta"]), ["paragraph"])
        self.assertEqual(updates["meta"]["paragraph"]["rich_text"][0]["annotations"]["color"], "gray")
        self.assertEqual(list(updates["weather"]), ["callout"])

    def test_renamed_teacher_leaves_meta_line_alone_but_fixes_the_rest(self):
        old = published_page(dict(REPORT, author_name="예전 선생님"), "legacy")
        plan = relabel.plan_page(old, REPORT)
        self.assertNotIn("meta", dict(plan.updates))
        self.assertIn("meta", plan.skipped)
        self.assertIn("weather", dict(plan.updates))

    def test_hand_edited_text_is_left_alone(self):
        old = published_page(REPORT, "legacy")
        set_text(old, "meta", texts(old)["meta"].replace("💩 배변 watery", "💩 배변 조금 묽었어요"))
        set_text(old, "bowel-1", "16:20  조금 묽음")
        set_text(old, "weather", "오늘의 날씨: 흐리다가 맑음")
        plan = relabel.plan_page(old, REPORT)
        self.assertFalse(plan.changed)
        self.assertEqual(sorted(plan.skipped), ["bowel", "meta", "weather"])

    def test_formatted_or_split_block_is_left_alone(self):
        old = published_page(REPORT, "legacy")
        text = texts(old)["meta"]
        old[0]["paragraph"]["rich_text"] = [seg(text[:10], GRAY), seg(text[10:], GRAY)]
        self.assertIn("meta", relabel.plan_page(old, REPORT).skipped)
        old = published_page(REPORT, "legacy")
        old[0]["paragraph"]["rich_text"][0]["annotations"]["bold"] = True
        self.assertIn("meta", relabel.plan_page(old, REPORT).skipped)

    def test_hidden_weather_callout_is_removed(self):
        report = dict(REPORT, weather="none")
        old = published_page(report, "legacy")
        self.assertEqual(texts(old)["weather"], "오늘의 날씨: none")
        plan = relabel.plan_page(old, report)
        self.assertEqual(plan.deletes, ["weather"])
        self.assertFalse(relabel.plan_page(apply(old, plan), report).changed)

    def test_parent_post_has_no_weather_to_fix(self):
        report = dict(REPORT, author={"type": "parent"})
        plan = relabel.plan_page(published_page(report, "legacy"), report)
        self.assertNotIn("weather", dict(plan.updates))
        self.assertNotIn("weather", plan.skipped)

    def test_bowel_section_with_a_missing_row_is_left_alone(self):
        old = [b for b in published_page(REPORT, "legacy") if b["id"] != "bowel-1"]
        plan = relabel.plan_page(old, REPORT)
        self.assertIn("bowel", plan.skipped)
        self.assertFalse(any(bid.startswith("bowel") for bid, _ in plan.updates))

    def test_report_without_life_records_or_blocks(self):
        report = {"id": 1, "author": {"type": "teacher"}, "author_name": "교사", "date_written": "2026-01-01"}
        plan = relabel.plan_page([block("meta", "paragraph", "👩‍🏫 선생님 교사 · 작성 2026-01-01", GRAY)], report)
        self.assertFalse(plan.changed)
        self.assertEqual(plan.skipped, [])
        self.assertFalse(relabel.plan_page([], REPORT).changed)

    def test_label_tables_are_restored_even_on_error(self):
        before = (nm.STATUS_KO, nm.SLEEP_HOUR_KO, nm.WEATHER_KO, nm.ACTIVITY_RATE_KO)
        with self.assertRaises(RuntimeError), relabel._label_tables({}, {}, {}, {}):
            raise RuntimeError("boom")
        after = (nm.STATUS_KO, nm.SLEEP_HOUR_KO, nm.WEATHER_KO, nm.ACTIVITY_RATE_KO)
        self.assertTrue(all(a is b for a, b in zip(before, after)))

    def test_new_rendering_matches_what_publishing_writes(self):
        mirror = nm.NotionMirror("token", "db", session=requests.Session())
        mirror.disable_llm_callouts = True
        children = mirror._build_children(REPORT, [], [], [])
        self.assertEqual(relabel._block_text(children[0]), " · ".join(nm.NotionMirror._meta_bits(REPORT)))
        callouts = [relabel._block_text(b) for b in children if b["type"] == "callout"]
        self.assertEqual(callouts, [relabel.render(REPORT, "new").weather])
        detail = nm.NotionMirror._life_record_detail_blocks(REPORT)
        self.assertEqual(relabel._bowel_lines(detail), relabel.render(REPORT, "new").bowel_lines)


class FakeNotion:
    """Block children / update / delete endpoints with small pages to exercise pagination."""

    PAGE_SIZE = 4

    def __init__(self, pages):
        self.pages = copy.deepcopy(pages)
        self.fail_get: set[str] = set()
        self.patch_script: list = []
        self.patches: list = []
        self.deletes: list = []

    def __call__(self, req):
        parts = req.path.strip("/").split("/")
        if req.method == "GET" and len(parts) == 3 and parts[0] == "blocks" and parts[2] == "children":
            if parts[1] in self.fail_get:
                return 500, {}, {"message": "boom"}
            blocks = self.pages[parts[1]]
            start = int(req.query.get("start_cursor", ["0"])[0])
            more = start + self.PAGE_SIZE < len(blocks)
            return 200, {}, {"results": blocks[start:start + self.PAGE_SIZE], "has_more": more,
                             "next_cursor": str(start + self.PAGE_SIZE) if more else None}
        if req.method == "PATCH" and len(parts) == 2:
            if self.patch_script:
                return self.patch_script.pop(0)
            body = req.json()
            self.patches.append(parts[1])
            for blocks in self.pages.values():
                for b in blocks:
                    if b["id"] == parts[1]:
                        new_seg = dict(body[b["type"]]["rich_text"][0])
                        new_seg["plain_text"] = new_seg["text"]["content"]
                        b[b["type"]]["rich_text"] = [new_seg]
            return 200, {}, {"object": "block", "id": parts[1]}
        if req.method == "DELETE" and len(parts) == 2:
            self.deletes.append(parts[1])
            for pid in self.pages:
                self.pages[pid] = [b for b in self.pages[pid] if b["id"] != parts[1]]
            return 200, {}, {"object": "block", "id": parts[1], "archived": True}
        return 404, {}, {"message": "not found"}


@unittest.skipIf(relabel is None, "requests not installed")
class RelabelRunnerTest(unittest.TestCase):
    def setUp(self):
        self.current = dict(REPORT, id=2, bowel_status="average", bowel=[])
        self.notion = FakeNotion({
            "page-a": published_page(REPORT, "legacy", prefix="a-"),
            "page-b": published_page(self.current, "new", prefix="b-"),
        })
        server = FakeServer(self.notion)
        server.__enter__()
        self.addCleanup(server.__exit__, None, None, None)
        self.sleeps: list[float] = []
        for patcher in (mock.patch.object(nm, "NOTION_API", server.url),
                        mock.patch.object(relabel, "_sleep", self.sleeps.append)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.mirror = nm.NotionMirror("token", "db", session=requests.Session())
        self.page_map = {REPORT["id"]: "page-a", 2: "page-b"}
        self.reports = [REPORT, self.current, dict(REPORT, id=3)]  # id 3 has no page yet

    def run_relabel(self, **kwargs):
        return relabel.relabel_report_pages(self.mirror, self.reports, self.page_map, **kwargs)

    def test_rewrites_outdated_pages_and_counts(self):
        counts = self.run_relabel()
        self.assertEqual(counts, {"updated": 1, "unchanged": 1, "skipped": 0, "failed": 0})
        self.assertEqual(sorted(self.notion.patches), ["a-bowel-0", "a-bowel-1", "a-meta", "a-weather"])
        self.assertEqual(texts(self.notion.pages["page-a"]), texts(published_page(REPORT, "new", prefix="a-")))

    def test_second_run_is_a_no_op(self):
        self.run_relabel()
        self.notion.patches.clear()
        self.assertEqual(self.run_relabel(), {"updated": 0, "unchanged": 2, "skipped": 0, "failed": 0})
        self.assertEqual(self.notion.patches, [])

    def test_reads_every_page_of_block_children(self):
        self.run_relabel()
        self.assertGreater(len(self.notion.pages["page-a"]), FakeNotion.PAGE_SIZE * 2)
        self.assertIn("a-bowel-1", self.notion.patches)  # lives past the first two API pages

    def test_failing_page_is_counted_and_others_continue(self):
        self.notion.fail_get.add("page-b")
        with self.assertLogs("relabel", level="WARNING") as logs:
            counts = self.run_relabel()
        self.assertEqual((counts["updated"], counts["failed"]), (1, 1))
        self.assertEqual(len(self.sleeps), relabel.MAX_ATTEMPTS - 1)
        self.assertNotIn("새싹", "\n".join(logs.output))  # never page text in logs

    def test_rate_limited_update_is_retried(self):
        self.notion.patch_script = [(429, {"Retry-After": "1"}, {"message": "slow down"})]
        counts = self.run_relabel()
        self.assertEqual(counts["updated"], 1)
        self.assertEqual(self.sleeps, [1.0])
        self.assertEqual(texts(self.notion.pages["page-a"])["a-meta"], NEW_META)

    def test_stops_when_time_budget_is_used_up(self):
        with self.assertLogs("relabel", level="WARNING"):
            counts = self.run_relabel(time_left=lambda: 0)
        self.assertEqual(counts["stopped_early"], 1)
        self.assertEqual(self.notion.patches, [])


if __name__ == "__main__":
    unittest.main()
