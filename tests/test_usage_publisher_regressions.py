from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import codex_usage_publisher as publisher


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def first_goal_cycle(session_id: str = "019fd1da-cc5d-7db1-b880-a14be6111c38") -> list[dict[str, object]]:
    return [
        {"timestamp": "2026-09-07T10:00:00Z", "type": "session_meta", "payload": {"session_id": session_id}},
        {"timestamp": "2026-09-07T10:00:01Z", "type": "turn_context", "payload": {"turn_id": "turn-1", "model": "gpt-5.5", "collaboration_mode": {"settings": {"reasoning_effort": "medium"}}}},
        {"timestamp": "2026-09-07T10:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": "/goal Esegui personalhub-places-history-map-geofencing"}]}},
        {"timestamp": "2026-09-07T10:00:03Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"text": "PROMPT_ID: `681395`\nRESULT: PAUSA"}], "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"}}},
        {"timestamp": "2026-09-07T10:00:04Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": "PROMPT_ID: `681395`\nRESULT: PAUSA", "duration_ms": 1000}},
    ]


def goal_continuation() -> list[dict[str, object]]:
    return [
        {"timestamp": "2026-09-07T10:01:00Z", "type": "turn_context", "payload": {"turn_id": "turn-2", "model": "gpt-5.5", "collaboration_mode": {"settings": {"reasoning_effort": "medium"}}}},
        {"timestamp": "2026-09-07T10:01:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": "<codex_internal_context source=\"goal\">\nContinue working toward the active thread goal.\n</codex_internal_context>"}]}},
        {"timestamp": "2026-09-07T10:01:02Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-2", "last_agent_message": "Same blocker remains.", "duration_ms": 500}},
    ]


class UsagePublisherRegressionTests(unittest.TestCase):
    def test_final_response_prompt_id_fallback_accepts_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "session.jsonl"
            write_jsonl(session, first_goal_cycle())
            with publisher.connect_state(root / "state") as con:
                cycles, _events = publisher.parse_session(session, con)
            self.assertEqual(cycles[0]["metrics"]["prompt_id"], "681395")

    def test_existing_cycle_fingerprint_stays_stable_when_rollout_grows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "session.jsonl"
            write_jsonl(session, first_goal_cycle())
            with publisher.connect_state(root / "state") as con:
                before, _events = publisher.parse_session(session, con)
                first_fingerprint = before[0]["cycle_sha256"]
                write_jsonl(session, first_goal_cycle() + goal_continuation())
                after, _events = publisher.parse_session(session, con)
            self.assertEqual(after[0]["cycle_sha256"], first_fingerprint)

    def test_goal_continuation_inherits_recovered_prompt_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "session.jsonl"
            write_jsonl(session, first_goal_cycle() + goal_continuation())
            with publisher.connect_state(root / "state") as con:
                cycles, _events = publisher.parse_session(session, con)
            self.assertEqual([cycle["metrics"]["prompt_id"] for cycle in cycles], ["681395", "681395"])

    def test_existing_state_db_gets_cycle_fingerprint_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            db = state / "publisher.sqlite"
            with sqlite3.connect(db) as con:
                con.executescript(
                    """
                    CREATE TABLE session_chats (
                        chat_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL UNIQUE,
                        first_seen_utc TEXT NOT NULL
                    );
                    CREATE TABLE cycles (
                        cycle_key TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        chat_id INTEGER NOT NULL,
                        prompt_id TEXT,
                        turn_id TEXT,
                        final_event_id TEXT NOT NULL,
                        source_path TEXT NOT NULL,
                        source_sha256 TEXT NOT NULL,
                        completed_at_utc TEXT,
                        published_commit TEXT,
                        telegram_sent INTEGER NOT NULL DEFAULT 0,
                        updated_at_utc TEXT NOT NULL
                    );
                    """
                )
            with publisher.connect_state(state) as con:
                columns = {row["name"] for row in con.execute("PRAGMA table_info(cycles)")}
            self.assertIn("cycle_sha256", columns)


if __name__ == "__main__":
    unittest.main()
