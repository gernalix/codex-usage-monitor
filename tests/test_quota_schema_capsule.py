import tempfile
from pathlib import Path
import unittest

from codex_monitor.capsules.quota import implementation, schema


class QuotaSchemaBoundaryTests(unittest.TestCase):
    def test_stable_config_and_schema_public_imports(self):
        for name in ('Config','connect_db','ensure_column','init_db'):
            self.assertIs(getattr(implementation,name),getattr(schema,name))

    def test_schema_init_is_idempotent_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg=schema.Config(
                db_path=Path(directory)/'quota.sqlite',
                state_dir=Path(directory)/'state',
                lock_path=Path(directory)/'lock',
                codex_bin='codex',app_server_port=38655,
                source_timeout_sec=15,startup_timeout_sec=15,sqlite_timeout_sec=5,
                telegram_helper=Path(directory)/'unused',telegram_enabled=False,
                notify_approaching_expiry_hours=24,notify_failure_after_runs=3,
                notification_cooldown_minutes=60,
            )
            schema.init_db(cfg)
            schema.init_db(cfg)
            with schema.connect_db(cfg) as connection:
                tables={row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
                self.assertIn('quota_snapshots',tables)
                self.assertIn('rate_limit_snapshots',tables)
                self.assertIn('notification_events',tables)


if __name__=='__main__':
    unittest.main()
