from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from codex_monitor.capsules.c2_health import implementation as health


class C2HealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.quota = self.root / "quota.sqlite"
        self.archive = self.root / "archive.sqlite"
        self.history = self.root / "history.sqlite"
        self.roadmap = self.root / "roadmap.sqlite"
        self.publisher = self.root / "publisher.json"
        self.now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        self._make_quota()
        self._make_archive()
        self._make_history()
        self._make_roadmap()
        self.publisher.write_text("{}", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _stamp(self, seconds_ago: int = 0) -> str:
        value = self.now - dt.timedelta(seconds=seconds_ago)
        return value.isoformat().replace("+00:00", "Z")
    def _make_quota(self) -> None:
        conn = sqlite3.connect(self.quota)
        conn.execute(
            """CREATE TABLE acquisition_runs(
                run_id INTEGER PRIMARY KEY,
                started_at_utc TEXT,
                completed_at_utc TEXT,
                status TEXT,
                source_method TEXT,
                source_version TEXT,
                error TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO acquisition_runs VALUES(1,?,?,?,?,?,?)",
            (self._stamp(), self._stamp(), "ok", "test", "1", None),
        )
        conn.commit()
        conn.close()

    def _make_archive(self) -> None:
        conn = sqlite3.connect(self.archive)
        conn.execute(
            """CREATE TABLE import_runs(
                run_id INTEGER PRIMARY KEY,
                started_at_utc TEXT,
                completed_at_utc TEXT,
                status TEXT,
                sessions_seen INTEGER,
                sessions_imported INTEGER,
                sessions_skipped INTEGER,
                source_files_imported INTEGER,
                error TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO import_runs VALUES(1,?,?,?,?,?,?,?,?)",
            (self._stamp(), self._stamp(), "ok", 1, 0, 1, 0, None),
        )
        conn.commit()
        conn.close()
    def _make_history(self) -> None:
        conn = sqlite3.connect(self.history)
        conn.executescript(
            """
            CREATE TABLE prompts(prompt_uid TEXT PRIMARY KEY);
            CREATE TABLE source_records(
                source TEXT, source_key TEXT, payload_hash TEXT, kind TEXT,
                ingested_at TEXT
            );
            """
        )
        conn.execute("INSERT INTO prompts VALUES('p')")
        for source in ("codex-roadmap", "codex-session-bandit", "codex-usage", "chatgpt"):
            conn.execute(
                "INSERT INTO source_records VALUES(?,?,?,?,?)",
                (source, source, "hash", "prompt", self._stamp()),
            )
        conn.commit()
        conn.close()

    def _make_roadmap(self) -> None:
        conn = sqlite3.connect(self.roadmap)
        conn.execute("CREATE TABLE prompts(prompt_id TEXT,status TEXT)")
        conn.execute("INSERT INTO prompts VALUES('1','running')")
        conn.commit()
        conn.close()

    def test_aggregate_health_is_up_for_fresh_complete_sources(self) -> None:
        payload = health.aggregate_health(
            quota_db=self.quota,
            archive_db=self.archive,
            history_db=self.history,
            roadmap_db=self.roadmap,
            publisher_state=self.publisher,
        )
        self.assertTrue(payload["healthy"])
        self.assertEqual(payload["status"], "up")
        self.assertTrue(all(item["ok"] for item in payload["components"].values()))
    def test_history_reports_missing_chatgpt_source(self) -> None:
        conn = sqlite3.connect(self.history)
        conn.execute("DELETE FROM source_records WHERE source='chatgpt'")
        conn.commit()
        conn.close()
        payload = health.aggregate_health(
            quota_db=self.quota,
            archive_db=self.archive,
            history_db=self.history,
            roadmap_db=self.roadmap,
            publisher_state=self.publisher,
        )
        self.assertFalse(payload["healthy"])
        self.assertFalse(payload["components"]["history"]["ok"])
        self.assertIn("chatgpt", payload["components"]["history"]["detail"])

    def test_kuma_monitor_spec_matches_health_timer_contract(self) -> None:
        self.assertEqual(health.KUMA_MONITOR_SPEC["name"], "C2")
        self.assertEqual(health.KUMA_MONITOR_SPEC["type"], "push")
        self.assertGreaterEqual(health.KUMA_MONITOR_SPEC["interval"], 300)
        self.assertEqual(health.KUMA_MONITOR_SPEC["max_retries"], 2)

    def test_build_push_url_replaces_status_message_and_ping(self) -> None:
        url = health.build_push_url(
            "https://example.test/api/push/token?status=down&msg=old&ping=1",
            status="up",
            message="C2 healthy",
            ping_ms=12.5,
        )
        self.assertIn("status=up", url)
        self.assertIn("msg=C2+healthy", url)
        self.assertIn("ping=12.5", url)

    @mock.patch("urllib.request.urlopen")
    def test_push_health_requires_kuma_ok(self, opener: mock.Mock) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = json.dumps({"ok": True}).encode()
        opener.return_value = response
        health.push_health(
            {"status": "up", "components": {}},
            push_url="https://example.test/api/push/token",
        )
        opener.assert_called_once()


if __name__ == "__main__":
    unittest.main()
