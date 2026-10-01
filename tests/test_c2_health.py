import unittest
from unittest.mock import patch
from codex_monitor.capsules.c2_health import implementation as health


class RetiredHealthTests(unittest.TestCase):
    def test_old_health_cannot_read_database_or_push_network(self):
        with patch('sqlite3.connect') as database, patch('urllib.request.urlopen') as network:
            self.assertEqual(2,health.main(['once']))
            database.assert_not_called()
            network.assert_not_called()
