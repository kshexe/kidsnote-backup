"""Structural checks on the workflows.

The pre-run check / notify-once / gating steps and the per-child matrix only
work together: a renamed step, a missing `if:`, or a step-level env that
shadows $GITHUB_ENV silently brings back repeated failure emails, a stale
cookie, or one child's settings leaking into another child's job. Pin them.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import notify_once  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "kidsnote-to-notion.yml"
UNIT_TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "unit-tests.yml"
JOB_NAME = "백업 (자녀 ${{ matrix.slot.label }})"
SLOT_CHILD = ("${{ secrets[format('KIDSNOTE_CHILD_NAME{0}', matrix.slot.suffix)] "
              "|| vars[format('KIDSNOTE_CHILD_NAME{0}', matrix.slot.suffix)] || '' }}")
SLOT_TOKEN = "${{ secrets[format('NOTION_TOKEN{0}', matrix.slot.suffix)] || secrets.NOTION_TOKEN }}"
SLOT_DB = "${{ secrets[format('NOTION_DATABASE_ID{0}', matrix.slot.suffix)] }}"


def _find_bash():
    if os.name == "nt":
        for path in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
            if os.path.exists(path):
                return path
        return None
    return shutil.which("bash")


BASH = _find_bash()


def run_step_script(script: str, env_values: dict[str, str]) -> dict[str, str]:
    """Run a workflow `run:` script the way Actions does (bash -e) and return its $GITHUB_OUTPUT."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp, "output")
        out.touch()
        script_file = Path(tmp, "step.sh")
        script_file.write_text(script, encoding="utf-8", newline="\n")
        env = {k: v for k, v in os.environ.items()
               if k not in ("REPO_VAR_AI", "REPO_SECRET_AI", "GROWTH_STORY", "MILESTONES", "INTERESTS",
                            "TEACHER_THANKS", "DB_2", "DB_3", "DB_4", "DB_5")}
        env.update(env_values)
        env["GITHUB_OUTPUT"] = str(out).replace("\\", "/")
        subprocess.run([BASH, "-e", str(script_file).replace("\\", "/")], env=env, check=True,
                       capture_output=True)
        return dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines() if "=" in line)


@unittest.skipIf(yaml is None, "PyYAML not installed")
class MirrorWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        cls.job = cls.doc["jobs"]["mirror"]
        cls.steps = cls.job["steps"]
        cls.names = [s.get("name") or s.get("uses") for s in cls.steps]

    def step(self, name):
        return self.steps[self.names.index(name)]

    def test_workflow_file_name_matches_notify_default(self):
        self.assertEqual(WORKFLOW.name, notify_once.DEFAULT_WORKFLOW_FILE)

    def test_keeps_schedule_and_actions_write_permission(self):
        triggers = self.doc.get("on") or self.doc.get(True)  # YAML 1.1 reads `on` as True
        self.assertIn("schedule", triggers)
        self.assertIn("workflow_dispatch", triggers)
        self.assertEqual(self.doc["permissions"].get("actions"), "write")

    def test_one_job_per_configured_child(self):
        self.assertEqual(self.job["needs"], "plan")
        self.assertEqual(self.job["name"], JOB_NAME)
        self.assertFalse(self.job["strategy"]["fail-fast"])
        self.assertEqual(self.job["strategy"]["matrix"]["slot"], "${{ fromJSON(needs.plan.outputs.slots) }}")
        self.assertLessEqual(self.job["timeout-minutes"], 360)

    def test_preflight_runs_first_with_the_childs_settings(self):
        pre = self.step("Check login and settings")
        self.assertEqual(self.names.index("Check login and settings"), 1, "must run right after checkout")
        self.assertEqual(pre.get("id"), "auth")
        self.assertIn("preflight.py --github", pre["run"])
        self.assertNotIn("if", pre)
        for secret in ("KIDSNOTE_USERNAME", "KIDSNOTE_PASSWORD", "KIDSNOTE_SESSION_COOKIE"):
            self.assertEqual(pre["env"][secret], "${{ secrets.%s }}" % secret)
        self.assertEqual(pre["env"]["KIDSNOTE_CHILD_NAME"], SLOT_CHILD)
        self.assertEqual(pre["env"]["NOTION_TOKEN"], SLOT_TOKEN)
        self.assertEqual(pre["env"]["NOTION_DATABASE_ID"], SLOT_DB)

    def test_every_step_reads_the_same_child_slot(self):
        mirror = self.step("Mirror Kidsnote → Notion")["env"]
        verify = self.step("Verify required secrets were present")["env"]
        self.assertEqual((mirror["KIDSNOTE_CHILD_NAME"], mirror["NOTION_TOKEN"], mirror["NOTION_DATABASE_ID"]),
                         (SLOT_CHILD, SLOT_TOKEN, SLOT_DB))
        self.assertEqual((verify["NOTION_TOKEN"], verify["NOTION_DATABASE_ID"]), (SLOT_TOKEN, SLOT_DB))
        raw = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("secrets.NOTION_DATABASE_ID }}", raw)
        self.assertNotIn("secrets.KIDSNOTE_CHILD_NAME ||", raw)

    def test_notify_step_name_condition_and_job_name(self):
        self.assertIn(notify_once.STEP_NAME, self.names, "notify_once.STEP_NAME must equal the workflow step name")
        notify = self.step(notify_once.STEP_NAME)
        self.assertEqual(self.names.index(notify_once.STEP_NAME), 2)
        self.assertIn("notify_once.py", notify["run"])
        self.assertIn("!cancelled()", notify["if"])
        self.assertIn("steps.auth.outputs.ok != 'true'", notify["if"])
        self.assertEqual(notify["env"]["GITHUB_TOKEN"], "${{ github.token }}")
        self.assertEqual(notify["env"]["JOB_NAME"], JOB_NAME, "must match the job name notify_once filters on")

    def test_every_later_step_is_gated_on_the_check(self):
        for step in self.steps[3:]:
            name = step.get("name") or step.get("uses")
            cond = str(step.get("if", ""))
            if name == "Keep cron schedule alive":
                self.assertEqual(cond, "always()")
                continue
            with self.subTest(step=name):
                gated = "steps.auth.outputs.ok == 'true'" in cond or "steps.ai.outputs.value" in cond
                self.assertTrue(gated, f"step {name!r} must be skipped when the check failed (if: {cond!r})")

    def test_mirror_step_does_not_shadow_fresh_session(self):
        env = self.step("Mirror Kidsnote → Notion").get("env", {})
        self.assertNotIn("KIDSNOTE_SESSION_COOKIE", env,
                         "a step-level env would override the fresh sessionid from $GITHUB_ENV")
        self.assertNotIn("KIDSNOTE_PASSWORD", env)

    def test_dashboard_toggles_come_from_the_resolve_step(self):
        env = self.step("Mirror Kidsnote → Notion")["env"]
        for name in ("growth_story", "milestones", "interests", "teacher_thanks"):
            with self.subTest(name=name):
                self.assertEqual(env[f"DISABLE_{name.upper()}"], "${{ steps.ai.outputs.disable_%s }}" % name)

    def test_relabel_input_is_off_by_default_and_wired(self):
        triggers = self.doc.get("on") or self.doc.get(True)
        relabel_input = triggers["workflow_dispatch"]["inputs"]["relabel"]
        self.assertEqual(relabel_input["default"], "off")
        self.assertEqual(relabel_input["options"], ["off", "on"])
        self.assertIn("--relabel-existing", self.step("Mirror Kidsnote → Notion")["run"])

    def test_scripts_referenced_by_workflow_exist(self):
        for script in ("kidsnote_auth.py", "notify_once.py", "fetch.py", "preflight.py", "notion_check.py",
                       "secret_input.py", "relabel.py"):
            self.assertTrue((ROOT / "tools" / "kidsnote_fetch" / script).is_file(), script)

    @unittest.skipIf(BASH is None, "bash not available")
    def test_ai_and_dashboard_toggles_are_forgiving(self):
        script = self.step("Resolve effective AI toggle")["run"]
        for env_values, expected in (({"REPO_SECRET_AI": "on"}, "on"), ({"REPO_SECRET_AI": " ON\n"}, "on"),
                                     ({"REPO_SECRET_AI": "켜기"}, "on"), ({"REPO_SECRET_AI": "\ufeffon"}, "on"),
                                     ({"REPO_VAR_AI": "True"}, "on"), ({"REPO_SECRET_AI": '"on"'}, "on"),
                                     ({"REPO_SECRET_AI": "off"}, "off"), ({}, "off"),
                                     ({"REPO_SECRET_AI": "onn"}, "off")):
            with self.subTest(env=env_values):
                self.assertEqual(run_step_script(script, env_values)["value"], expected)
        out = run_step_script(script, {"GROWTH_STORY": " OFF ", "MILESTONES": "on", "INTERESTS": "끄기"})
        self.assertEqual((out["disable_growth_story"], out["disable_milestones"], out["disable_interests"],
                          out["disable_teacher_thanks"]), ("true", "false", "true", "false"))


@unittest.skipIf(yaml is None, "PyYAML not installed")
class PlanJobTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["plan"]
        cls.find = cls.plan["steps"][0]

    def test_outputs_only_slot_labels(self):
        self.assertEqual(self.plan["outputs"]["slots"], "${{ steps.find.outputs.slots }}")
        for n in (2, 3, 4, 5):
            self.assertEqual(self.find["env"][f"DB_{n}"], "${{ secrets.NOTION_DATABASE_ID_%d }}" % n)

    @unittest.skipIf(BASH is None, "bash not available")
    def test_slots_follow_the_configured_databases(self):
        def slots(**env):
            return [s["label"] for s in json.loads(run_step_script(self.find["run"], env)["slots"])]
        self.assertEqual(slots(), ["1"])
        self.assertEqual(slots(DB_2="https://www.notion.so/abc"), ["1", "2"])
        self.assertEqual(slots(DB_2="x", DB_4="y"), ["1", "2", "4"])
        self.assertEqual(slots(DB_3=" \n"), ["1"])
        suffixes = [s["suffix"] for s in json.loads(run_step_script(self.find["run"], {"DB_5": "z"})["slots"])]
        self.assertEqual(suffixes, ["", "_5"])


@unittest.skipIf(yaml is None, "PyYAML not installed")
class UnitTestsWorkflowTest(unittest.TestCase):
    def test_runs_only_in_the_original_repo_and_says_users_can_ignore_it(self):
        doc = yaml.safe_load(UNIT_TESTS_WORKFLOW.read_text(encoding="utf-8"))
        self.assertIn("사용자", doc["name"])
        self.assertIn("github.repository == 'redchupa/kidsnote-backup'", doc["jobs"]["unit-tests"]["if"])


if __name__ == "__main__":
    unittest.main()
