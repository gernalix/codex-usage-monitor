from __future__ import annotations

import unittest

from codex_monitor.capsules.publishing import api
from codex_monitor.capsules.publishing import base


class PublishingStatusTests(unittest.TestCase):
    def test_bare_fixed_is_terminal(self) -> None:
        self.assertEqual("FIXED", api.status_from_final("FIXED\nService repaired."))

    def test_explicit_fixed_is_terminal(self) -> None:
        self.assertEqual("FIXED", api.status_from_final("PROMPT_ID=742615\nRESULT=FIXED"))

    def test_existing_terminal_statuses_remain_supported(self) -> None:
        self.assertEqual("PASS", api.status_from_final("PASS\nok"))
        self.assertEqual("BLOCKED", api.status_from_final("RESULT: `BLOCKED`"))
        self.assertEqual("WAITING_FOR_EVENT", api.status_from_final("STATUS: WAITING_FOR_EVENT"))
        self.assertEqual("UNKNOWN", api.status_from_final("No explicit terminal status."))

    def test_verification_labels_are_terminal(self) -> None:
        self.assertEqual(
            "PASS",
            api.status_from_final("CAUSA: cache\nVERIFICA: PASS — repository valido"),
        )
        self.assertEqual("FAIL", api.status_from_final("VERIFICATION=FAIL"))
        self.assertEqual("BLOCKED", api.status_from_final("ESITO: BLOCKED"))

    def test_public_api_patches_base_parser_used_by_runtime(self) -> None:
        self.assertIs(base.status_from_final, api.status_from_final)
        self.assertEqual("FIXED", base.status_from_final("FIXED\nrepaired"))


if __name__ == "__main__":
    unittest.main()
