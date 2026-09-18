from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from codex_monitor.capsules.publishing import base as publisher


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


def attached_prompt_cycle(
    attachment: Path,
    session_id: str = "01a0a7b0-92a8-7ff1-bbda-1447e57b6695",
    final_response: str = "RESULT=BLOCKED",
) -> list[dict[str, object]]:
    return [
        {"timestamp": "2026-09-16T00:49:22Z", "type": "session_meta", "payload": {"session_id": session_id}},
        {"timestamp": "2026-09-16T00:49:23Z", "type": "turn_context", "payload": {"turn_id": "turn-attached", "model": "gpt-5.6-sol", "collaboration_mode": {"settings": {"reasoning_effort": "medium"}}}},
        {"timestamp": "2026-09-16T00:49:24Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": f"Referenced pasted text files:\n- pasted text file: {attachment}. Read this file before continuing."}]}},
        {"timestamp": "2026-09-16T00:49:25Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-attached", "last_agent_message": final_response, "duration_ms": 1000}},
    ]


def plain_followup(text: str = "autorizzo", final_response: str = "RESULT=PASS") -> list[dict[str, object]]:
    return [
        {"timestamp": "2026-09-16T00:50:00Z", "type": "turn_context", "payload": {"turn_id": "turn-followup", "model": "gpt-5.6-sol", "collaboration_mode": {"settings": {"reasoning_effort": "medium"}}}},
        {"timestamp": "2026-09-16T00:50:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": text}]}},
        {"timestamp": "2026-09-16T00:50:02Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-followup", "last_agent_message": final_response, "duration_ms": 500}},
    ]


def automatic_continuation(final_response: str = "RESULT=PASS") -> list[dict[str, object]]:
    return [
        {"timestamp": "2026-09-16T00:50:00Z", "type": "turn_context", "payload": {"turn_id": "turn-auto", "model": "gpt-5.6-sol", "collaboration_mode": {"settings": {"reasoning_effort": "medium"}}}},
        {"timestamp": "2026-09-16T00:50:02Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-auto", "last_agent_message": final_response, "duration_ms": 500}},
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

    def test_inline_prompt_id_accepts_markdown_escaped_underscore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "session.jsonl"
            rows = [
                {"timestamp": "2026-09-18T16:54:51Z", "type": "session_meta", "payload": {"session_id": "s-escaped"}},
                {"timestamp": "2026-09-18T16:54:52Z", "type": "turn_context", "payload": {"turn_id": "turn-escaped", "model": "gpt-5.6-sol", "collaboration_mode": {"settings": {"reasoning_effort": "medium"}}}},
                {
                    "timestamp": "2026-09-18T16:54:53Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": r"PROMPT\_ID=918274 | project\_id=49"}],
                    },
                },
                {
                    "timestamp": "2026-09-18T16:54:54Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-escaped",
                        "last_agent_message": "RESULT: PASS — PROMPT_ID 918274",
                        "duration_ms": 1000,
                    },
                },
            ]
            write_jsonl(session, rows)
            with publisher.connect_state(root / "state") as con:
                cycles, _events = publisher.parse_session(session, con)

            self.assertEqual(cycles[0]["metrics"]["prompt_id"], "918274")
            self.assertIn(r"PROMPT\_ID=918274", cycles[0]["metrics"]["prompt_text_redacted"])

    def test_attached_prompt_is_indexed_from_codex_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attachments_root = root / ".codex/attachments"
            attachment = attachments_root / "78a4850d-5cfe-4204-bb55-b890e7f8a5af/pasted-text-1.txt"
            attachment.parent.mkdir(parents=True)
            attachment.write_text("`PROMPT_ID=472913 | project_id=8 | model=GPT-5.6 Sol`\n", encoding="utf-8")
            session = root / "session.jsonl"
            write_jsonl(session, attached_prompt_cycle(attachment))
            original_root = publisher.ATTACHMENTS_ROOT
            publisher.ATTACHMENTS_ROOT = attachments_root
            try:
                with publisher.connect_state(root / "state") as con:
                    cycles, _events = publisher.parse_session(session, con)
            finally:
                publisher.ATTACHMENTS_ROOT = original_root
            self.assertEqual(cycles[0]["metrics"]["prompt_id"], "472913")

    def test_empty_automatic_continuation_inherits_active_prompt_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attachments_root = root / ".codex/attachments"
            attachment = attachments_root / "78a4850d-5cfe-4204-bb55-b890e7f8a5af/pasted-text-1.txt"
            attachment.parent.mkdir(parents=True)
            attachment.write_text("PROMPT_ID=472913\n", encoding="utf-8")
            session = root / "session.jsonl"
            rows = attached_prompt_cycle(
                attachment,
                final_response="Selected model is at capacity. Please try a different model.",
            ) + automatic_continuation("RESULT=PASS")
            write_jsonl(session, rows)
            original_root = publisher.ATTACHMENTS_ROOT
            publisher.ATTACHMENTS_ROOT = attachments_root
            try:
                with publisher.connect_state(root / "state") as con:
                    cycles, _events = publisher.parse_session(session, con)
            finally:
                publisher.ATTACHMENTS_ROOT = original_root
            self.assertEqual(cycles[1]["metrics"]["prompt_text_redacted"], None)
            self.assertEqual([cycle["metrics"]["prompt_id"] for cycle in cycles], ["472913", "472913"])

    def test_blocked_user_followup_inherits_active_prompt_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attachments_root = root / ".codex/attachments"
            attachment = attachments_root / "aaae875b-e112-49f9-8ae9-e911d81dddaa/pasted-text-1.txt"
            attachment.parent.mkdir(parents=True)
            attachment.write_text("PROMPT_ID=157771\n", encoding="utf-8")
            session = root / "session.jsonl"
            rows = attached_prompt_cycle(
                attachment,
                final_response="RESULT: goal marcato BLOCKED dopo tre verifiche consecutive.",
            ) + plain_followup("autorizzo")
            write_jsonl(session, rows)
            original_root = publisher.ATTACHMENTS_ROOT
            publisher.ATTACHMENTS_ROOT = attachments_root
            try:
                with publisher.connect_state(root / "state") as con:
                    cycles, _events = publisher.parse_session(session, con)
            finally:
                publisher.ATTACHMENTS_ROOT = original_root
            self.assertEqual(cycles[0]["metrics"]["status"], "BLOCKED")
            self.assertEqual([cycle["metrics"]["prompt_id"] for cycle in cycles], ["157771", "157771"])

    def test_unrelated_plain_message_does_not_inherit_completed_prompt_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attachments_root = root / ".codex/attachments"
            attachment = attachments_root / "prompt/pasted-text-1.txt"
            attachment.parent.mkdir(parents=True)
            attachment.write_text("PROMPT_ID=816428\n", encoding="utf-8")
            session = root / "session.jsonl"
            rows = attached_prompt_cycle(attachment, final_response="RESULT=PASS") + plain_followup(
                "scrivi il percorso completo del final manifest",
                final_response="/home/daniele/repository-safety/prompt-816429/FINAL_MANIFEST.json",
            )
            write_jsonl(session, rows)
            original_root = publisher.ATTACHMENTS_ROOT
            publisher.ATTACHMENTS_ROOT = attachments_root
            try:
                with publisher.connect_state(root / "state") as con:
                    cycles, _events = publisher.parse_session(session, con)
            finally:
                publisher.ATTACHMENTS_ROOT = original_root
            self.assertEqual([cycle["metrics"]["prompt_id"] for cycle in cycles], ["816428", None])

    def test_chat_metrics_deduplicates_prompt_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attachments_root = root / ".codex/attachments"
            attachment = attachments_root / "aaae875b-e112-49f9-8ae9-e911d81dddaa/pasted-text-1.txt"
            attachment.parent.mkdir(parents=True)
            attachment.write_text("PROMPT_ID=157771\n", encoding="utf-8")
            session = root / "session.jsonl"
            write_jsonl(session, attached_prompt_cycle(attachment) + plain_followup())
            original_root = publisher.ATTACHMENTS_ROOT
            publisher.ATTACHMENTS_ROOT = attachments_root
            try:
                with publisher.connect_state(root / "state") as con:
                    cycles, _events = publisher.parse_session(session, con)
            finally:
                publisher.ATTACHMENTS_ROOT = original_root
            metrics = publisher.chat_metrics(int(cycles[0]["metrics"]["chat_id"]), cycles)
            self.assertEqual(metrics["prompt_ids"], ["157771"])

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
