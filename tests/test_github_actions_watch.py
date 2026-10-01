import unittest
from unittest.mock import patch
from codex_monitor.capsules.github_actions import implementation as watch


class RetiredWatcherTests(unittest.TestCase):
    def test_legacy_scan_cannot_launch_or_publish(self):
        with patch('subprocess.run') as process:
            for entry in (watch.process_scan,watch.publish_snapshot,watch.semantic_snapshot,watch.notification_message):
                with self.assertRaisesRegex(watch.WatchError,'retired'):
                    entry({})
            self.assertEqual(2,watch.main(['run']))
            process.assert_not_called()
