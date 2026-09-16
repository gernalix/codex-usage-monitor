from __future__ import annotations

import ast
from pathlib import Path
import unittest

from scripts import check_architecture_boundaries as boundaries


class ArchitectureBoundaryTest(unittest.TestCase):
    def errors(self, source: str, path: str = "src/codex_monitor/capsules/alpha/implementation.py") -> list[str]:
        return boundaries.check_tree(Path(path), ast.parse(source))

    def test_rejects_wildcard_import(self) -> None:
        self.assertTrue(self.errors("from package import *\n"))

    def test_rejects_dynamic_reexport(self) -> None:
        self.assertTrue(self.errors("EXPORTED = globals()\n"))

    def test_rejects_namespace_monkey_patch(self) -> None:
        self.assertTrue(self.errors("import package as dependency\ndependency.run = lambda: None\n"))

    def test_rejects_private_cross_capsule_import(self) -> None:
        self.assertTrue(self.errors("from codex_monitor.capsules.beta.implementation import run\n"))

    def test_allows_public_cross_capsule_api(self) -> None:
        self.assertFalse(self.errors("from codex_monitor.capsules.beta import api\n"))

    def test_rejects_top_level_adapter_logic(self) -> None:
        tree = ast.parse("def business_logic():\n    return 1\n")
        self.assertTrue(boundaries.check_adapter(Path("adapter.py"), tree))


if __name__ == "__main__":
    unittest.main()
