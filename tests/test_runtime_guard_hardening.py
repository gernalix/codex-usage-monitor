from __future__ import annotations

import json
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

from codex_monitor.capsules.publishing import base as publisher
from codex_monitor.capsules.runtime_deploy import implementation as deploy_runtime


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


class RuntimeGuardHardeningTests(unittest.TestCase):
    def make_repo_pair(self, root: Path) -> tuple[Path, Path]:
        root.mkdir(parents=True, exist_ok=True)
        bare = root / "origin.git"
        repo = root / "repo"
        self.assertEqual(0, run(["git", "init", "--bare", str(bare)]).returncode)
        self.assertEqual(0, run(["git", "clone", str(bare), str(repo)]).returncode)
        self.assertEqual(0, run(["git", "config", "user.email", "test@example.invalid"], repo).returncode)
        self.assertEqual(0, run(["git", "config", "user.name", "Test"], repo).returncode)
        for name in deploy_runtime.RUNTIME_FILES:
            (repo / name).write_text("VALUE = 1\n", encoding="utf-8")
        package = repo / deploy_runtime.RUNTIME_PACKAGE
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("\n", encoding="utf-8")
        self.assertEqual(0, run(["git", "add", *deploy_runtime.RUNTIME_FILES, "src/codex_monitor"], repo).returncode)
        self.assertEqual(0, run(["git", "commit", "-m", "base"], repo).returncode)
        self.assertEqual(0, run(["git", "branch", "-M", "main"], repo).returncode)
        self.assertEqual(0, run(["git", "push", "-u", "origin", "main"], repo).returncode)
        return repo, bare

    def test_deploy_fetches_remote_before_sync_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, bare = self.make_repo_pair(root / "pair")
            other = root / "other"
            self.assertEqual(0, run(["git", "clone", str(bare), str(other)]).returncode)
            self.assertEqual(0, run(["git", "config", "user.email", "test@example.invalid"], other).returncode)
            self.assertEqual(0, run(["git", "config", "user.name", "Test"], other).returncode)
            self.assertEqual(0, run(["git", "checkout", "main"], other).returncode)
            (other / "remote.txt").write_text("remote\n", encoding="utf-8")
            self.assertEqual(0, run(["git", "add", "remote.txt"], other).returncode)
            self.assertEqual(0, run(["git", "commit", "-m", "remote advance"], other).returncode)
            self.assertEqual(0, run(["git", "push", "origin", "main"], other).returncode)

            with self.assertRaisesRegex(deploy_runtime.DeployError, "not synchronized"):
                deploy_runtime.assert_clean_synced(repo)

    def test_release_integrity_is_verified_and_files_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _bare = self.make_repo_pair(root / "pair")
            runtime = root / "runtime"
            result = deploy_runtime.deploy(repo, runtime)
            release = Path(result["release"])
            current = runtime / "current"
            self.assertEqual(release.resolve(), current.resolve())
            self.assertFalse(bool((release / "codex_usage_publisher.py").stat().st_mode & stat.S_IWUSR))

            target = release / "codex_usage_publisher.py"
            target.chmod(0o644)
            target.write_text("VALUE = 999\n", encoding="utf-8")
            with self.assertRaisesRegex(deploy_runtime.DeployError, "integrity mismatch|hash mismatch"):
                deploy_runtime.deploy(repo, runtime)

    def test_mutating_exec_commands_produce_write_evidence(self) -> None:
        workdir = "/tmp/example-repo"
        mutating = (
            "sed -i 's/a/b/' file.txt",
            "git add file.txt && git commit -m test",
            "printf x > file.txt",
            "python3 -c \"from pathlib import Path; Path('x').write_text('y')\"",
        )
        for cmd in mutating:
            event = {
                "tool_name": "exec_command",
                "content_text": json.dumps({"cmd": cmd, "workdir": workdir}),
            }
            self.assertEqual([workdir], publisher._exec_write_paths_from_event(event), cmd)

        for cmd in ("true", "rg -n TODO .", "git status --short", "python3 -m unittest"):
            event = {
                "tool_name": "exec_command",
                "content_text": json.dumps({"cmd": cmd, "workdir": workdir}),
            }
            self.assertEqual([], publisher._exec_write_paths_from_event(event), cmd)


if __name__ == "__main__":
    unittest.main()
