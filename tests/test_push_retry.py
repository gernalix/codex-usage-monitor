import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.codex_monitor.capsules.publishing import push_retry


class PublisherRetryTests(unittest.TestCase):
    def test_failed_scan_backs_off_before_it_can_be_repeated(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            git = Mock(side_effect=subprocess.TimeoutExpired("git push", 1200))
            with self.assertRaises(push_retry.legacy.PublisherError):
                push_retry.push(state, state, git)
            with self.assertRaises(push_retry.legacy.PublisherError):
                push_retry.push(state, state, git)
            self.assertEqual(git.call_count, 1)
            self.assertTrue(push_retry.pending(state, state, Mock()))
            deadline = json.loads((state / "push-retry.json").read_text())["retry_after"]
            git.side_effect = None
            with patch.object(push_retry.time, "time", return_value=deadline + 1):
                push_retry.push(state, state, git)
            git.assert_called_with(["git", "push", "origin", "main"], state, timeout=1200)
            self.assertFalse((state / "push-retry.json").exists())

    def test_guard_rejection_holds_descendants_until_history_is_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            tip = "ab" * 20
            message = f"Gitleaks ha fermato il push. possibile credenziale nei commit verso {tip[:12]}."
            git = Mock(side_effect=push_retry.legacy.PublisherError(message))
            head = subprocess.CompletedProcess([], 0, stdout=tip)
            ancestor = subprocess.CompletedProcess([], 0)
            rewritten = subprocess.CompletedProcess([], 1)
            with patch.object(push_retry.legacy, "run", side_effect=[head, ancestor, rewritten]):
                with self.assertRaisesRegex(push_retry.legacy.PublisherError, "quarantined"):
                    push_retry.push(state, state, git)
                with self.assertRaisesRegex(push_retry.legacy.PublisherError, "quarantined"):
                    push_retry.push(state, state, git)
                git.side_effect = None
                push_retry.push(state, state, git)
            self.assertEqual(git.call_count, 2)
            git.assert_called_with(["git", "push", "origin", "main"], state, timeout=1200)
