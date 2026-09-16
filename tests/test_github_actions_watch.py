from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import github_actions_watch as watch


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def completed_run(
    repo: str = "gernalix/example",
    *,
    workflow_id: int = 10,
    branch: str = "main",
    run_id: int = 100,
    conclusion: str = "failure",
    created_at: str = "2026-09-16T10:00:00Z",
) -> dict[str, object]:
    return {
        "repo": repo,
        "workflow_id": workflow_id,
        "workflow_name": "CI",
        "branch": branch,
        "run_id": run_id,
        "run_number": run_id,
        "conclusion": conclusion,
        "created_at": created_at,
        "updated_at": created_at,
        "head_sha": f"{run_id:040d}"[-40:],
        "url": f"https://github.com/{repo}/actions/runs/{run_id}",
    }


class GitHubActionsWatchTests(unittest.TestCase):
    def test_first_failure_opens_incident_and_notifies_once(self) -> None:
        state = {"schema": 1, "owner": "gernalix", "keys": {}, "recent_events": []}
        row = completed_run()

        notify, events, bootstrap = watch.process_scan(state, {"k": row}, observed_at="2026-09-16T10:01:00Z")

        self.assertTrue(bootstrap)
        self.assertEqual(["k"], notify)
        self.assertEqual(["k"], events)
        self.assertTrue(state["keys"]["k"]["open"])
        self.assertEqual("failure", state["keys"]["k"]["notification_pending"])

    def test_repeated_failure_updates_count_without_new_notification_after_dispatch(self) -> None:
        state = {"schema": 1, "owner": "gernalix", "keys": {}, "recent_events": []}
        watch.process_scan(state, {"k": completed_run(run_id=100)}, observed_at="2026-09-16T10:01:00Z")
        state["keys"]["k"]["notification_pending"] = None

        notify, events, bootstrap = watch.process_scan(
            state,
            {"k": completed_run(run_id=101, created_at="2026-09-16T11:00:00Z")},
            observed_at="2026-09-16T11:01:00Z",
        )

        self.assertFalse(bootstrap)
        self.assertEqual([], notify)
        self.assertEqual([], events)
        self.assertEqual(2, state["keys"]["k"]["failure_count"])
        self.assertTrue(state["keys"]["k"]["open"])

    def test_success_after_open_failure_closes_incident_and_notifies_recovery(self) -> None:
        state = {"schema": 1, "owner": "gernalix", "keys": {}, "recent_events": []}
        watch.process_scan(state, {"k": completed_run(run_id=100)}, observed_at="2026-09-16T10:01:00Z")
        state["keys"]["k"]["notification_pending"] = None

        notify, events, _bootstrap = watch.process_scan(
            state,
            {"k": completed_run(run_id=102, conclusion="success", created_at="2026-09-16T12:00:00Z")},
            observed_at="2026-09-16T12:01:00Z",
        )

        self.assertEqual(["k"], notify)
        self.assertEqual(["k"], events)
        self.assertFalse(state["keys"]["k"]["open"])
        self.assertEqual("recovery", state["keys"]["k"]["notification_pending"])

    def test_bootstrap_notification_is_aggregated(self) -> None:
        state = {"schema": 1, "owner": "gernalix", "keys": {}, "recent_events": []}
        current = {
            "a": completed_run(repo="gernalix/a", run_id=1),
            "b": completed_run(repo="gernalix/b", run_id=2),
        }

        notify, events, bootstrap = watch.process_scan(state, current, observed_at="2026-09-16T10:01:00Z")
        message = watch.notification_message(state, notify)

        self.assertTrue(bootstrap)
        self.assertEqual(["a", "b"], notify)
        self.assertEqual(["a", "b"], events)
        self.assertIn("2 GitHub CI incidents open", message)

    def test_publish_noops_when_only_observation_metadata_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare = root / "origin.git"
            repo = root / "repo"
            self.assertEqual(0, run(["git", "init", "--bare", str(bare)]).returncode)
            self.assertEqual(0, run(["git", "clone", str(bare), str(repo)]).returncode)
            self.assertEqual(0, run(["git", "config", "user.email", "test@example.invalid"], repo).returncode)
            self.assertEqual(0, run(["git", "config", "user.name", "Test"], repo).returncode)
            self.assertEqual(0, run(["git", "checkout", "-b", "main"], repo).returncode)
            target = repo / "index/github-actions.json"
            target.parent.mkdir(parents=True)
            base = {
                "schema": "codex-usage.github-actions.v1",
                "generated_at_utc": "2026-09-16T10:00:00Z",
                "owner": "gernalix",
                "status": "ok",
                "repos_scanned": 1,
                "active_incident_count": 1,
                "active_incidents": [
                    {
                        "incident_key": "gernalix/example|10|main",
                        "repo": "gernalix/example",
                        "workflow_id": 10,
                        "workflow_name": "CI",
                        "branch": "main",
                        "open": True,
                        "failure_count": 1,
                        "last_observed_at": "2026-09-16T10:00:00Z",
                        "last_notified_at": "2026-09-16T10:00:10Z",
                    }
                ],
                "recent_events": [
                    {
                        "type": "failure",
                        "incident_key": "gernalix/example|10|main",
                        "repo": "gernalix/example",
                        "workflow_name": "CI",
                        "observed_at": "2026-09-16T10:00:00Z",
                    }
                ],
                "scan_errors": [],
            }
            target.write_text(json.dumps(base, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            self.assertEqual(0, run(["git", "add", "index/github-actions.json"], repo).returncode)
            self.assertEqual(0, run(["git", "commit", "-m", "base"], repo).returncode)
            self.assertEqual(0, run(["git", "push", "-u", "origin", "main"], repo).returncode)
            before = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()

            changed = json.loads(json.dumps(base))
            changed["generated_at_utc"] = "2026-09-16T11:00:00Z"
            changed["active_incidents"][0]["last_observed_at"] = "2026-09-16T11:00:00Z"
            changed["active_incidents"][0]["last_notified_at"] = "2026-09-16T11:00:10Z"
            changed["recent_events"][0]["observed_at"] = "2026-09-16T11:00:00Z"

            result = watch.publish_snapshot(repo, str(bare), changed, dry_run=False)

            self.assertEqual({"status": "noop"}, result)
            after = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
            self.assertEqual(before, after)
            self.assertEqual(base, json.loads(target.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
