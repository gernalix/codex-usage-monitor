from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("verify_repo_script", ROOT / "scripts" / "verify_repo.py")
assert SPEC is not None and SPEC.loader is not None
verify_repo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_repo)


class VerifyRepoTests(unittest.TestCase):
    def test_resource_warning_output_forces_failure_even_with_zero_exit(self) -> None:
        rc = verify_repo.run(
            [sys.executable, "-c", "print('ResourceWarning: unclosed database in test')"],
            fail_on_resource_warning=True,
        )
        self.assertEqual(86, rc)

    def test_clean_zero_exit_remains_success(self) -> None:
        rc = verify_repo.run(
            [sys.executable, "-c", "print('clean')"],
            fail_on_resource_warning=True,
        )
        self.assertEqual(0, rc)


if __name__ == "__main__":
    unittest.main()
