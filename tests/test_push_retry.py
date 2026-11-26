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
