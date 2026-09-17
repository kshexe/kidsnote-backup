"""Every life-record code kidsnote's own UI knows must render with kidsnote's exact wording.

tests/data/kidsnote_status_labels_ko.json is kidsnote's wording, copied from
the i18n store of its web report page. A raw `watery` leaked into published
pages before this test existed.
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

try:
    import notion_mirror as nm
except ImportError:  # pragma: no cover - requests not installed
    nm = None

OFFICIAL = json.loads((HERE / "data" / "kidsnote_status_labels_ko.json").read_text(encoding="utf-8"))
STATUS_FIELDS = ("meal_status", "bowel_status", "temperature_status", "mood_status", "health_status",
                 "outdoor_activity_status", "bath_status", "nail_status")
RAW_CODE = re.compile(r"[a-z]+_[a-z_0-9]+|\b(?:watery|diarrhea|slight|shower|none|true|false|undefined)\b")


@unittest.skipIf(nm is None, "notion_mirror dependencies (requests) not installed")
class OfficialWordingTest(unittest.TestCase):
    def test_fixture_is_complete(self):
        for field in STATUS_FIELDS + ("sleep_hour", "weather", "activity_rate"):
            with self.subTest(field=field):
                self.assertTrue(OFFICIAL.get(field), f"fixture has no codes for {field}")

    def test_status_codes_use_kidsnote_wording(self):
        for field in STATUS_FIELDS:
            for code, label in OFFICIAL[field].items():
                with self.subTest(field=field, code=code):
                    self.assertEqual(nm.STATUS_KO.get(code), label)

    def test_sleep_codes_use_kidsnote_wording(self):
        for code, label in OFFICIAL["sleep_hour"].items():
            with self.subTest(code=code):
                self.assertEqual(nm.SLEEP_HOUR_KO.get(code), label)

    def test_weather_codes_use_kidsnote_wording_or_are_hidden(self):
        for code, label in OFFICIAL["weather"].items():
            with self.subTest(code=code):
                if label == "표시안함":
                    self.assertIn(code, nm.WEATHER_HIDDEN)
                    self.assertNotIn(code, nm.WEATHER_KO)
                else:
                    self.assertIn(code, nm.WEATHER_KO)
                    emoji, _, text = nm.WEATHER_KO[code].partition(" ")
                    self.assertEqual(text, label)
                    self.assertTrue(emoji and not emoji.isalnum(), "keep the leading emoji (page titles use it)")

    def test_activity_rate_uses_kidsnote_wording(self):
        self.assertEqual(nm.ACTIVITY_RATE_KO, OFFICIAL["activity_rate"])

    def test_shared_status_table_has_no_conflicting_words(self):
        # STATUS_KO is shared by every *_status field; a code must mean the same thing everywhere.
        codes = {code for field in STATUS_FIELDS for code in OFFICIAL[field]}
        for code in codes:
            labels = {OFFICIAL[f][code] for f in STATUS_FIELDS if code in OFFICIAL[f]}
            with self.subTest(code=code):
                self.assertEqual(len(labels), 1, labels)


@unittest.skipIf(nm is None, "notion_mirror dependencies (requests) not installed")
class LifeRecordChipsTest(unittest.TestCase):
    def bits(self, **report):
        return nm.NotionMirror._life_record_bits(report)

    def assertNoRawCodes(self, bits):
        for chip in bits:
            with self.subTest(chip=chip):
                self.assertIsNone(RAW_CODE.search(chip), chip)

    def test_codes_that_leaked_before_are_korean_now(self):
        bits = self.bits(meal_status="fixed", sleep_hour="sleep_hardly", bowel_status="watery",
                         temperature_status="normal")
        self.assertEqual(bits, ["🍽️ 식사 정량", "💤 수면 잠을 설쳤어요", "💩 배변 묽음", "🌡️ 체온 정상"])
        self.assertIn("💩 배변 설사", self.bits(bowel_status="diarrhea"))

    def test_every_official_code_renders_without_raw_code(self):
        for field in STATUS_FIELDS:
            for code in OFFICIAL[field]:
                value = {"true": True, "false": False}.get(code, code) if field == "outdoor_activity_status" else code
                with self.subTest(field=field, code=code):
                    bits = self.bits(**{field: value})
                    self.assertEqual(len(bits), 1, bits)
                    self.assertNoRawCodes(bits)
        for code in OFFICIAL["sleep_hour"]:
            with self.subTest(sleep_hour=code):
                self.assertNoRawCodes(self.bits(sleep_hour=code))

    def test_outdoor_activity_booleans_and_strings(self):
        self.assertIn("🏃 야외활동 O", self.bits(outdoor_activity_status=True))
        self.assertIn("🏃 야외활동 X", self.bits(outdoor_activity_status=False))
        self.assertIn("🏃 야외활동 O", self.bits(outdoor_activity_status="true"))
        self.assertEqual(self.bits(outdoor_activity_status=None), [])

    def test_activity_rate_numbers_and_strings(self):
        self.assertIn("⭐ 활동 적극적", self.bits(activity_rate=10))
        self.assertIn("⭐ 활동 보통", self.bits(activity_rate="20"))
        self.assertIn("⭐ 활동 소극적", self.bits(activity_rate=30))
        self.assertEqual(self.bits(activity_rate=None), [])

    def test_unknown_future_code_still_shows_instead_of_disappearing(self):
        self.assertIn("💩 배변 brand_new_code", self.bits(bowel_status="brand_new_code"))

    def test_empty_report(self):
        self.assertEqual(self.bits(), [])


if __name__ == "__main__":
    unittest.main()
