from __future__ import annotations

import unittest

import deploy_runtime


class DeployRuntimeDependenciesTest(unittest.TestCase):
    def test_publisher_base_module_is_deployed(self) -> None:
        self.assertIn("codex_usage_publisher.py", deploy_runtime.RUNTIME_FILES)
        self.assertIn("codex_usage_publisher_base.py", deploy_runtime.RUNTIME_FILES)
        self.assertIn("codex_usage_publisher_legacy.py", deploy_runtime.RUNTIME_FILES)


if __name__ == "__main__":
    unittest.main()
