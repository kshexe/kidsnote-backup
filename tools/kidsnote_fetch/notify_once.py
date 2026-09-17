"""Email the operator once per setup problem, not every 6 hours.

GitHub emails the repo owner whenever a scheduled run fails. With the 6h
cron, a lasting problem (password changed, Notion connection removed) used
to send 4 emails a day until someone fixed it. The workflow runs this only
when the "Check login and settings" step did not succeed:

  * the previous finished run had not reported it -> exit 1, so this run
    fails and GitHub sends one email;
  * the previous finished run already reported it -> exit 0 with a
    warning, so this run ends green and silent (backup skipped).

With several children each child has its own job; JOB_NAME limits the
lookup to that child's job so one child's problem doesn't hide another's.
Fixing the secret is all it takes to resume: the next run passes the check
and the chain resets. If the GitHub API can't be read, it errs on the side
of notifying. Stdlib only, because it runs before `pip install`.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping

# Must equal the workflow step `name:` that runs this script: the previous
# run's steps are searched for it. tests/test_workflow.py pins the match.
STEP_NAME = "Kidsnote login failed - notify once"
DEFAULT_WORKFLOW_FILE = "kidsnote-to-notion.yml"
# A run "says something" only if it finished; cancelled/skipped runs don't count.
FINISHED_CONCLUSIONS = {"success", "failure"}
# The step ran in that run (loud = failure, quiet = success) -> already reported.
REPORTED_CONCLUSIONS = {"success", "failure"}
REQUEST_TIMEOUT = 20


def pick_previous_run(runs: list[dict], current_run_id: str) -> dict | None:
    """Newest finished run other than the current one (the API lists newest first)."""
    for run in runs:
        if str(run.get("id")) == str(current_run_id):
            continue
        if run.get("status") == "completed" and run.get("conclusion") in FINISHED_CONCLUSIONS:
            return run
    return None


def run_reported(jobs: list[dict], job_name: str | None = None) -> bool:
    """True if that run executed the notify step (in ``job_name``'s job, when given)."""
    return any(
        step.get("name") == STEP_NAME and step.get("conclusion") in REPORTED_CONCLUSIONS
        for job in jobs
        if job_name is None or job.get("name") == job_name
        for step in job.get("steps") or []
    )


def workflow_file(workflow_ref: str) -> str:
    """'owner/repo/.github/workflows/x.yml@refs/heads/main' -> 'x.yml'."""
    name = workflow_ref.split("@", 1)[0].rsplit("/", 1)[-1]
    return name if name.endswith((".yml", ".yaml")) else DEFAULT_WORKFLOW_FILE


def _get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "kidsnote-backup-notify-once",
    })
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        e.close()
        raise
    if not isinstance(data, dict):
        raise ValueError(f"unexpected GitHub API response from {url}")
    return data


def already_reported(env: Mapping[str, str]) -> bool:
    """Ask the GitHub API whether the previous finished run already reported a problem."""
    api = (env.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    repo = env["GITHUB_REPOSITORY"]
    token = env["GITHUB_TOKEN"]
    wf = workflow_file(env.get("GITHUB_WORKFLOW_REF", ""))
    runs = _get_json(f"{api}/repos/{repo}/actions/workflows/{wf}/runs?status=completed&per_page=20", token)
    prev = pick_previous_run(runs.get("workflow_runs") or [], env.get("GITHUB_RUN_ID", ""))
    if prev is None:
        return False
    jobs = _get_json(f"{api}/repos/{repo}/actions/runs/{prev['id']}/jobs?per_page=100", token)
    return run_reported(jobs.get("jobs") or [], env.get("JOB_NAME") or None)


def _escape(message: str) -> str:
    """Escape a workflow-command message so '%' and newlines can't break the annotation."""
    return message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    reason = env.get("REASON") or "unexpected"
    hint = env.get("HINT") or "백업 준비 확인에 실패했습니다."
    try:
        reported = already_reported(env)
    except Exception as e:  # can't tell -> notify rather than stay silent
        print("::warning::" + _escape(f"이전 실행 기록을 확인하지 못해 알림을 보냅니다. ({type(e).__name__}: {e})"))
        reported = False

    if reported:
        print("::warning title=백업 준비 확인 실패 (이미 알림 보냄)::"
              + _escape(f"{hint} [{reason}] 이미 알림을 보냈으므로 이번 백업은 조용히 건너뜁니다."))
        return 0
    print("::error title=백업 준비 확인 실패::" + _escape(f"{hint} [{reason}]"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
