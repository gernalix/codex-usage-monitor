from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_monitor.capsules.publishing import api as publisher


class PublicationSemanticBackfillTests(unittest.TestCase):
    @staticmethod
    def cycle() -> dict[str, object]:
        return {
            "metrics": {
                "schema": "codex-usage.prompt-metrics.v1",
                "cycle_key": "cycle-fixed-regression",
                "prompt_id": "742615",
                "chat_id": 273,
                "native_session_id": "session-742615",
                "turn_id": "turn-742615",
                "source_path": "/tmp/rollout-742615.jsonl",
                "timestamp_end_utc": "2026-09-16T10:20:17Z",
                "prompt_text_redacted": "PROMPT_ID=742615\n",
                "final_response_redacted": "FIXED\nRecovered successfully.\n",
                "repo_paths": [],
            },
            "events": [{"top_type": "event_msg", "subtype": "task_complete"}],
            "final_event_id": "event-742615",
            "source_sha256": "legacy-fingerprint",
        }

    def test_publication_semantics_version_salts_cycle_fingerprint(self) -> None:
        cycle = self.cycle()
        raw = publisher._RAW_CYCLE_FINGERPRINT(cycle)
        expected = publisher.legacy.digest_text(
            f"publication-semantics-v{publisher.PUBLICATION_SEMANTICS_VERSION}:{raw}"
        )
        self.assertEqual(expected, publisher._publication_fingerprint(cycle))
        self.assertNotEqual(raw, publisher._publication_fingerprint(cycle))

    def test_existing_published_cycle_becomes_pending_without_preseeding_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = Path(temp) / "state"
            fake_rollout = Path(temp) / "rollout.jsonl"
            fake_rollout.write_text("{}\n", encoding="utf-8")
            cycle = self.cycle()

            with publisher.connect_state(state_dir) as con:
                con.execute(
                    """
                    INSERT INTO cycles (
                        cycle_key, session_id, chat_id, prompt_id, turn_id,
                        final_event_id, source_path, source_sha256, cycle_sha256,
                        fingerprint_schema, completed_at_utc, published_commit,
                        telegram_sent, updated_at_utc
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "cycle-fixed-regression",
                        "session-742615",
                        273,
                        "742615",
                        "turn-742615",
                        "event-742615",
                        str(fake_rollout),
                        "legacy-fingerprint",
                        "legacy-fingerprint",
                        publisher.FINGERPRINT_SCHEMA,
                        "2026-09-16T10:20:17Z",
                        "published-before-semantic-change",
                        1,
                        "2026-09-16T10:21:00Z",
                    ),
                )
                con.commit()

                old_should_export = publisher._base._should_export
                try:
                    publisher._base._should_export = False
                    cycles, _events = publisher._base.parse_session(
                        fake_rollout,
                        con,
                        legacy_parse_session_fn=lambda _path, _con: ([cycle], []),
                    )
                    self.assertTrue(publisher._base.export_required())
                finally:
                    publisher._base._should_export = old_should_export

                row = con.execute(
                    "SELECT source_sha256,cycle_sha256,fingerprint_schema,published_commit "
                    "FROM cycles WHERE cycle_key=?",
                    ("cycle-fixed-regression",),
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual("legacy-fingerprint", row["source_sha256"])
                self.assertEqual("legacy-fingerprint", row["cycle_sha256"])
                self.assertEqual(publisher.FINGERPRINT_SCHEMA, row["fingerprint_schema"])
                self.assertEqual("published-before-semantic-change", row["published_commit"])

                parsed = cycles[0]
                self.assertEqual("FIXED", parsed["metrics"]["status"])
                self.assertNotEqual("legacy-fingerprint", parsed["source_sha256"])


if __name__ == "__main__":
    unittest.main()
