"""Tests for notify_once.py — one email per login outage."""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import notify_once  # noqa: E402
from fake_http import FakeServer  # noqa: E402

STEP = notify_once.STEP_NAME
REPO = "someone/kidsnote-backup"
CURRENT_RUN = "999"


def run_entry(run_id, conclusion, status="completed"):
    return {"id": run_id, "status": status, "conclusion": conclusion}


def jobs_with(step_conclusion=None):
    steps = [{"name": "Set up job", "conclusion": "success"},
             {"name": "Kidsnote login", "conclusion": "success"}]
    if step_conclusion is not None:
        steps.append({"name": STEP, "conclusion": step_conclusion})
    steps.append({"name": "Keep cron schedule alive", "conclusion": "success"})
    return {"jobs": [{"id": 1, "steps": steps}]}


class PickPreviousRunTest(unittest.TestCase):
    def test_skips_current_run_and_unfinished_or_cancelled(self):
        runs = [run_entry(999, "failure"), run_entry(5, "cancelled"),
                run_entry(4, None, status="in_progress"), run_entry(3, "skipped"),
                run_entry(2, "success"), run_entry(1, "failure")]
        self.assertEqual(notify_once.pick_previous_run(runs, CURRENT_RUN)["id"], 2)

    def test_none_when_no_finished_run(self):
        self.assertIsNone(notify_once.pick_previous_run([], CURRENT_RUN))
        self.assertIsNone(notify_once.pick_previous_run([run_entry(5, "cancelled")], CURRENT_RUN))

    def test_current_run_id_compared_as_string(self):
        self.assertIsNone(notify_once.pick_previous_run([run_entry(999, "failure")], "999"))


class RunReportedTest(unittest.TestCase):
    def test_loud_or_quiet_step_counts_as_reported(self):
        self.assertTrue(notify_once.run_reported(jobs_with("failure")["jobs"]))
        self.assertTrue(notify_once.run_reported(jobs_with("success")["jobs"]))

    def test_skipped_missing_or_unfinished_step_is_not_reported(self):
        self.assertFalse(notify_once.run_reported(jobs_with("skipped")["jobs"]))
        self.assertFalse(notify_once.run_reported(jobs_with(None)["jobs"]))
        self.assertFalse(notify_once.run_reported([{"steps": [{"name": STEP, "conclusion": None}]}]))
        self.assertFalse(notify_once.run_reported([{"steps": None}, {}]))


class WorkflowFileTest(unittest.TestCase):
    def test_parses_workflow_ref(self):
        ref = "someone/kidsnote-backup/.github/workflows/my-mirror.yml@refs/heads/main"
        self.assertEqual(notify_once.workflow_file(ref), "my-mirror.yml")

    def test_falls_back_to_default(self):
        self.assertEqual(notify_once.workflow_file(""), notify_once.DEFAULT_WORKFLOW_FILE)
        self.assertEqual(notify_once.workflow_file("garbage"), notify_once.DEFAULT_WORKFLOW_FILE)


class MainTest(unittest.TestCase):
    """End to end against a fake GitHub API."""

    def make_env(self, server, **extra):
        env = {
            "GITHUB_API_URL": server.url,
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_RUN_ID": CURRENT_RUN,
            "GITHUB_TOKEN": "test-token",
            "GITHUB_WORKFLOW_REF": f"{REPO}/.github/workflows/kidsnote-to-notion.yml@refs/heads/main",
            "REASON": "invalid_credentials",
            "HINT": "아이디 또는 비밀번호가 틀렸습니다.",
        }
        env.update(extra)
        return env

    def github_api(self, runs, jobs_by_run, runs_status=200, jobs_status=200):
        def handler(req):
            if req.path == f"/repos/{REPO}/actions/workflows/kidsnote-to-notion.yml/runs":
                return runs_status, {}, {"workflow_runs": runs}
            for run_id, jobs in jobs_by_run.items():
                if req.path == f"/repos/{REPO}/actions/runs/{run_id}/jobs":
                    return jobs_status, {}, jobs
            return 404, {}, {"message": "Not Found"}
        return handler

    def run_main(self, env):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = notify_once.main(env)
        return code, buf.getvalue()

    def test_first_failure_after_success_is_loud(self):
        api = self.github_api([run_entry(999, None, "in_progress"), run_entry(10, "success")],
                              {10: jobs_with("skipped")})
        with FakeServer(api) as srv:
            code, out = self.run_main(self.make_env(srv))
        self.assertEqual(code, 1)
        self.assertIn("::error title=백업 준비 확인 실패::아이디 또는 비밀번호가 틀렸습니다. [invalid_credentials]", out)
        self.assertNotIn("::warning title=", out)

    def test_repeat_failure_after_loud_run_is_quiet(self):
        api = self.github_api([run_entry(10, "failure")], {10: jobs_with("failure")})
        with FakeServer(api) as srv:
            code, out = self.run_main(self.make_env(srv))
        self.assertEqual(code, 0)
        self.assertIn("::warning title=백업 준비 확인 실패 (이미 알림 보냄)::", out)
        self.assertNotIn("::error", out)

    def test_repeat_failure_after_quiet_run_stays_quiet(self):
        api = self.github_api([run_entry(11, "success"), run_entry(10, "failure")],
                              {11: jobs_with("success"), 10: jobs_with("failure")})
        with FakeServer(api) as srv:
            code, _ = self.run_main(self.make_env(srv))
        self.assertEqual(code, 0)

    def test_cancelled_runs_are_looked_past(self):
        api = self.github_api([run_entry(12, "cancelled"), run_entry(10, "failure")],
                              {10: jobs_with("failure")})
        with FakeServer(api) as srv:
            code, _ = self.run_main(self.make_env(srv))
        self.assertEqual(code, 0)

    def test_no_previous_runs_is_loud(self):
        with FakeServer(self.github_api([], {})) as srv:
            code, out = self.run_main(self.make_env(srv))
        self.assertEqual(code, 1)
        self.assertIn("::error", out)

    def test_sends_auth_header_and_completed_filter(self):
        api = self.github_api([run_entry(10, "success")], {10: jobs_with(None)})
        with FakeServer(api) as srv:
            self.run_main(self.make_env(srv))
        runs_req = srv.requests[0]
        self.assertEqual(runs_req.headers["authorization"], "Bearer test-token")
        self.assertEqual(runs_req.query["status"], ["completed"])
        self.assertEqual(srv.paths()[1], f"GET /repos/{REPO}/actions/runs/10/jobs")

    def test_api_errors_fall_back_to_notifying(self):
        for runs_status, jobs_status in ((403, 200), (500, 200), (200, 500)):
            with self.subTest(runs_status=runs_status, jobs_status=jobs_status):
                api = self.github_api([run_entry(10, "failure")], {10: jobs_with("failure")},
                                      runs_status=runs_status, jobs_status=jobs_status)
                with FakeServer(api) as srv:
                    code, out = self.run_main(self.make_env(srv))
                self.assertEqual(code, 1)
                self.assertIn("이전 실행 기록을 확인하지 못해", out)
                self.assertIn("::error", out)

    def test_non_json_api_response_falls_back_to_notifying(self):
        with FakeServer(lambda req: (200, {"Content-Type": "text/html"}, "<html>")) as srv:
            code, _ = self.run_main(self.make_env(srv))
        self.assertEqual(code, 1)

    def test_unreachable_api_falls_back_to_notifying(self):
        with FakeServer(lambda req: (200, {}, {})) as srv:
            dead_url = srv.url
        code, out = self.run_main(self.make_env(srv, GITHUB_API_URL=dead_url))
        self.assertEqual(code, 1)
        self.assertIn("이전 실행 기록을 확인하지 못해", out)

    def test_missing_github_env_does_not_crash(self):
        code, out = self.run_main({"REASON": "network", "HINT": "x"})
        self.assertEqual(code, 1)
        self.assertIn("::error", out)

    def test_defaults_when_reason_and_hint_empty(self):
        code, out = self.run_main({"REASON": "", "HINT": ""})
        self.assertEqual(code, 1)
        self.assertIn("백업 준비 확인에 실패했습니다. [unexpected]", out)

    def test_message_is_escaped(self):
        code, out = self.run_main({"REASON": "x", "HINT": "100% 실패\n두번째 줄"})
        self.assertEqual(code, 1)
        self.assertIn("100%25 실패%0A두번째 줄", out)
        error_lines = [line for line in out.splitlines() if line.startswith("::error")]
        self.assertEqual(len(error_lines), 1)


class PerChildJobTest(unittest.TestCase):
    """With several children, one child's reported problem must not silence another's."""

    JOBS = [{"name": "백업 (자녀 1)", "steps": [{"name": STEP, "conclusion": "skipped"}]},
            {"name": "백업 (자녀 2)", "steps": [{"name": STEP, "conclusion": "failure"}]}]

    def test_only_the_same_childs_job_counts(self):
        self.assertTrue(notify_once.run_reported(self.JOBS, "백업 (자녀 2)"))
        self.assertFalse(notify_once.run_reported(self.JOBS, "백업 (자녀 1)"))
        self.assertTrue(notify_once.run_reported(self.JOBS))  # no job name: any job

    def test_main_uses_job_name(self):
        def api(req):
            if req.path.endswith("/runs"):
                return 200, {}, {"workflow_runs": [run_entry(10, "failure")]}
            return 200, {}, {"jobs": self.JOBS}
        with FakeServer(api) as srv:
            env = {"GITHUB_API_URL": srv.url, "GITHUB_REPOSITORY": REPO, "GITHUB_RUN_ID": CURRENT_RUN,
                   "GITHUB_TOKEN": "t", "REASON": "notion_no_access", "HINT": "x"}
            with redirect_stdout(io.StringIO()):
                loud = notify_once.main(dict(env, JOB_NAME="백업 (자녀 1)"))
                quiet = notify_once.main(dict(env, JOB_NAME="백업 (자녀 2)"))
        self.assertEqual((loud, quiet), (1, 0))


if __name__ == "__main__":
    unittest.main()
