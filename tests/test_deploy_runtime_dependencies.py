from __future__ import annotations

from pathlib import Path
import subprocess
import unittest
from unittest import mock

from codex_monitor.capsules.runtime_deploy import implementation as deploy_runtime


class DeployRuntimeDependenciesTest(unittest.TestCase):
    def test_publisher_base_module_is_deployed(self) -> None:
        self.assertIn("codex_usage_publisher.py", deploy_runtime.RUNTIME_FILES)
        self.assertIn("codex_usage_publisher_base.py", deploy_runtime.RUNTIME_FILES)
        self.assertIn("codex_usage_publisher_legacy.py", deploy_runtime.RUNTIME_FILES)

    def test_assert_clean_synced_can_skip_redundant_fetch(self) -> None:
        status = subprocess.CompletedProcess(["git", "status", "--porcelain"], 0, stdout="", stderr="")
        with mock.patch.object(deploy_runtime, "run", return_value=status) as run_mock:
            with mock.patch.object(
                deploy_runtime,
                "git_stdout",
                side_effect=["origin/main", "abc123", "abc123"],
            ) as git_stdout_mock:
                commit = deploy_runtime.assert_clean_synced(Path("."), fetch=False)

        self.assertEqual(commit, "abc123")
        run_mock.assert_called_once_with(["git", "status", "--porcelain"], Path("."))
        self.assertEqual(git_stdout_mock.call_count, 3)

    def test_assert_clean_synced_reports_fetch_timeout(self) -> None:
        status = subprocess.CompletedProcess(["git", "status", "--porcelain"], 0, stdout="", stderr="")
        timeout = subprocess.TimeoutExpired(cmd=["git", "fetch", "origin"], timeout=7)
        with mock.patch.object(deploy_runtime, "run", side_effect=[status, timeout]):
            with mock.patch.object(deploy_runtime, "git_stdout", side_effect=["origin/main"]):
                with self.assertRaisesRegex(deploy_runtime.DeployError, "timed out after 7s"):
                    deploy_runtime.assert_clean_synced(Path("."), fetch_timeout=7)


if __name__ == "__main__":
    unittest.main()
