import unittest
from unittest.mock import patch
from codex_monitor.capsules.orchestration import implementation as orch


class RetirementTests(unittest.TestCase):
    def test_all_legacy_surfaces_fail_closed_without_process_or_database(self):
        with patch('subprocess.run') as process, patch('sqlite3.connect') as database:
            for entry in (orch.claim_prompt, orch.finish_prompt, orch.orchestrator_status,
                          orch.global_checkpoint, orch.checkpoint_next_action,
                          orch.runnable_prompts, orch.context_search):
                with self.assertRaisesRegex(orch.C2OrchestratorError, 'retired'):
                    entry('123456')
            self.assertEqual(2, orch.main(['claim', '123456']))
            process.assert_not_called()
            database.assert_not_called()
