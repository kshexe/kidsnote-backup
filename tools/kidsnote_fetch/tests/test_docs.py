"""The setup guide must match what a new user actually sees.

README.md / QUICK_START.md are followed click by click by people who have never
used GitHub or Notion. A renamed step, a reason code with no row in the fix
table, a secret name the workflow never reads or a dead link strands them with
no way forward. Pin every fact the guide quotes from the code.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from urllib.parse import unquote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import kidsnote_auth  # noqa: E402
import notify_once  # noqa: E402
import notion_check  # noqa: E402
import preflight  # noqa: E402

ROOT = HERE.parents[2]
DOCS = {name: (ROOT / name).read_text(encoding="utf-8") for name in ("README.md", "QUICK_START.md")}
README, QUICK_START = DOCS["README.md"], DOCS["QUICK_START.md"]
WORKFLOW = (ROOT / ".github" / "workflows" / "kidsnote-to-notion.yml").read_text(encoding="utf-8")
UNIT_TESTS = (ROOT / ".github" / "workflows" / "unit-tests.yml").read_text(encoding="utf-8")
REQUIRED_SECRETS = {"NOTION_TOKEN", "NOTION_DATABASE_ID", "KIDSNOTE_USERNAME", "KIDSNOTE_PASSWORD",
                    "KIDSNOTE_CHILD_NAME", "AI_FEATURES"}


def _source(module) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


def _prose(text: str) -> str:
    """Text outside fenced code blocks."""
    return re.sub(r"^```.*?^```[^\n]*$", "", text, flags=re.M | re.S)


def _section(text: str, heading: str) -> str:
    start = text.index(heading) + len(heading)
    end = re.search(r"^#{1,2} ", text[start:], flags=re.M)
    return text[start:start + end.start()] if end else text[start:]


def _anchor_region(text: str, anchor: str) -> str:
    start = text.index(f'<a id="{anchor}">')
    end = text.find("<a id=", start + 1)
    return text[start:end if end != -1 else len(text)]


def _first_cell_codes(table: str, pattern: str) -> set[str]:
    codes: set[str] = set()
    for line in table.splitlines():
        if line.startswith("| `"):
            codes |= set(re.findall(pattern, line.split("|")[1]))
    return codes


def _slug(heading: str) -> str:
    h = re.sub(r"<[^>]+>", "", heading.strip().lower())
    return re.sub(r"[^\w\- ]", "", h).replace(" ", "-")


def _anchors(text: str) -> set[str]:
    found, seen = set(), {}
    for line in _prose(text).splitlines():
        m = re.match(r"^#{1,6}\s+(.*)$", line)
        if m:
            s = _slug(m.group(1))
            n = seen.get(s, 0)
            found.add(s if n == 0 else f"{s}-{n}")
            seen[s] = n + 1
        found.update(re.findall(r'<a id="([^"]+)"', line))
    return found


def _code_reasons() -> set[str]:
    reasons = set(kidsnote_auth.HINTS) | set(notion_check.HINTS)
    reasons |= set(re.findall(r'AuthError\(\s*"([a-z0-9_]+)"', _source(kidsnote_auth)))
    reasons |= set(re.findall(r'NotionCheckError\(\s*"([a-z0-9_]+)"', _source(notion_check)))
    reasons |= set(re.findall(r'CheckFailed\(\s*"([a-z0-9_]+)"', _source(preflight)))
    return reasons


class ReasonTableTest(unittest.TestCase):
    def test_every_failure_reason_has_a_row_and_no_row_is_invented(self):
        table = _section(README, "## 준비 확인 실패 — 이유별 해결")
        self.assertEqual(_first_cell_codes(table, r"`([a-z0-9_]+)`"), _code_reasons())

    def test_reason_codes_quoted_elsewhere_exist(self):
        for name, text in DOCS.items():
            for code in re.findall(r"`\[([a-z0-9_]+)\]`", text):
                with self.subTest(doc=name, code=code):
                    self.assertIn(code, _code_reasons())


class QuotedOutputTest(unittest.TestCase):
    def test_preflight_lines_shown_in_the_guide_are_printed_by_preflight(self):
        source = _source(preflight)
        for name, text in DOCS.items():
            for line in re.findall(r"노션 연결 OK \([^)]*\)", text):
                with self.subTest(doc=name, line=line):
                    self.assertIn(line, source)
            for how in re.findall(r"키즈노트 로그인 OK \(([^)]*)\)", text):
                with self.subTest(doc=name, how=how):
                    self.assertIn(f'"{how}"', source)
        self.assertIn('f"자녀 확인 OK: {mask_name(', source)

    def test_annotation_titles_match_notify_once(self):
        source = _source(notify_once)
        for title in ("백업 준비 확인 실패", "백업 준비 확인 실패 (이미 알림 보냄)"):
            with self.subTest(title=title):
                self.assertIn(f"title={title}::", source)
                self.assertIn(title, README)

    def test_workflow_job_and_step_names(self):
        for label in ("Kidsnote → Notion mirror", "Check login and settings", "Mirror Kidsnote → Notion"):
            with self.subTest(label=label):
                self.assertIn(label, README)
                self.assertRegex(WORKFLOW, rf"(?m)^(name|\s+- name): {re.escape(label)}$")
        self.assertIn("name: 백업 (자녀 ${{ matrix.slot.label }})", WORKFLOW)
        self.assertIn("`백업 (자녀 1)`", QUICK_START)

    def test_developer_workflow_name(self):
        name = re.search(r"(?m)^name: (.+)$", UNIT_TESTS).group(1)
        for doc, text in DOCS.items():
            with self.subTest(doc=doc):
                self.assertIn(name, text)

    def test_run_workflow_inputs(self):
        for field in ("limit", "monthly_sample", "force_refresh", "relabel"):
            with self.subTest(field=field):
                self.assertRegex(WORKFLOW, rf"(?m)^      {field}:$")
                self.assertIn(f"`{field}`", QUICK_START + README)


class SecretNamesTest(unittest.TestCase):
    def test_setup_tables_list_the_same_six_secrets(self):
        quick = _first_cell_codes(_section(QUICK_START, "## 6️⃣"), r"`([A-Z_]+)`")
        readme = _first_cell_codes(_anchor_region(README, "6-3-시크릿-6개-등록"), r"`([A-Z_]+)`")
        self.assertEqual(quick, REQUIRED_SECRETS)
        self.assertEqual(readme, REQUIRED_SECRETS)

    def test_every_secret_name_in_the_guide_is_read_by_the_workflow(self):
        for doc, text in DOCS.items():
            for name in set(re.findall(r"`([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)`", text)):
                base, _, slot = name.rpartition("_") if re.search(r"_[2-9]$", name) else (name, "", "")
                with self.subTest(doc=doc, name=name):
                    self.assertTrue(re.search(rf"\b{base}\b", WORKFLOW), f"{base} not in the workflow")
                    if slot:
                        self.assertIn(slot, "2345")
                        self.assertIn(f"format('{base}{{0}}', matrix.slot.suffix)", WORKFLOW)

    def test_extra_child_slots_go_up_to_five(self):
        for n in "2345":
            with self.subTest(slot=n):
                self.assertIn(f"`KIDSNOTE_CHILD_NAME_{n}`", README)
                self.assertIn(f"`NOTION_DATABASE_ID_{n}`", README)
                self.assertIn(f"NOTION_DATABASE_ID_{n}", WORKFLOW)
        self.assertNotIn("NOTION_DATABASE_ID_6", WORKFLOW)


class LinksTest(unittest.TestCase):
    def test_links_to_headings_and_files_resolve(self):
        anchors = {name: _anchors(text) for name, text in DOCS.items()}
        for doc, text in DOCS.items():
            for target in re.findall(r"\]\(([^)\s]+)\)", _prose(text)):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                path, _, frag = target.partition("#")
                with self.subTest(doc=doc, target=target):
                    if not path:
                        self.assertIn(frag, anchors[doc])
                        continue
                    self.assertTrue((ROOT / unquote(path)).exists())
                    if frag and path in anchors:
                        self.assertIn(frag, anchors[path])

    def test_images_exist(self):
        for doc, text in DOCS.items():
            for src in re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", text):
                if not src.startswith("http"):
                    with self.subTest(doc=doc, src=src):
                        self.assertTrue((ROOT / unquote(src)).exists())


class RenderingTest(unittest.TestCase):
    def test_no_accidental_strikethrough(self):
        # GitHub renders ~text~ as strikethrough, so "1~3시간" twice in a paragraph eats the text between.
        for doc, text in DOCS.items():
            for paragraph in _prose(text).split("\n\n"):
                plain = re.sub(r"`[^`]*`", "", paragraph)
                with self.subTest(doc=doc, paragraph=paragraph.strip()[:60]):
                    self.assertLess(plain.count("~"), 2)


if __name__ == "__main__":
    unittest.main()
