from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import codex_human_log as m


class HumanLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sessions = self.root / "sessions/2026/10/16"
        self.sessions.mkdir(parents=True)
        self.state = self.root / "state"
        self.chat_id = "01a141a3-0879-7411-9245-6b144ba5ec41"
        self.src = self.sessions / ("rollout-2026-10-16T01-00-00-" + self.chat_id + ".jsonl")
        self.initial = [
            {"type": "session_meta", "payload": {"id": self.chat_id, "cwd": "/home/daniele/projects/demo"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": "/goal sistemare il sistema di backup"}]}},
        ]

    def write(self, events):
        with self.src.open("a", encoding="utf-8") as fp:
            for event in events:
                fp.write(json.dumps(event, ensure_ascii=False) + "\n")

    def assistant(self, msg, ordinal=10, channel=None):
        return {"type": "response_item", "ordinal": ordinal, "timestamp": "2026-10-16T01:02:00Z", "payload":
                {"type": "message", "role": "assistant", "channel": channel, "content": [{"text": msg}]}}

    def args(self, **kw):
        data = dict(source=str(self.root / "sessions"), state_dir=str(self.state),
                    megavault=str(self.root / "missing.sqlite"), translator="plain",
                    model="gpt-6-luna", bootstrap_chat=None, min_interval=0,
                    max_translations=2, max_ai_per_hour=24, telegram=False, fedora=False, dry_run=False)
        data.update(kw)
        return argparse.Namespace(**data)

    def test_live_append_idempotency_and_secret_exclusion(self):
        self.write(self.initial)
        self.assertEqual(m.run_once(self.args())["updates"], 0)
        self.write([
            {"type": "response_item", "payload": {"type": "reasoning", "text": "HIDDEN REASONING"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "output": "PRIVATE TOOL CONTENT"}},
            self.assistant("GitHub ha rifiutato il tentativo, sk-thisisasecret123456789!"),
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "CommandExecution", "exit_code": 1,
                                                                                  "command": "PRIVATE SHELL CONTENT"}}},
        ])
        self.assertEqual(m.run_once(self.args())["updates"], 1)
        self.assertEqual(m.run_once(self.args())["updates"], 0)
        live = (self.state / "live.md").read_text()
        machine = (self.state / "live.jsonl").read_text()
        self.assertIn("GitHub ha rifiutato", live)
        self.assertIn("sistemare il sistema di backup", (self.state / "state.json").read_text())
        for forbidden in ("thisisasecret", "PRIVATE TOOL CONTENT", "PRIVATE SHELL CONTENT", "HIDDEN REASONING"):
            self.assertNotIn(forbidden, live + machine)
        self.assertEqual(len(machine.splitlines()), 1)

    def test_bootstrap_bounded_and_no_replay(self):
        self.write(self.initial + [self.assistant("Il collaudo è riuscito.")])
        self.assertEqual(m.run_once(self.args(bootstrap_chat=self.chat_id))["updates"], 1)
        self.assertEqual(m.run_once(self.args())["updates"], 0)

    def test_partial_line_not_consumed(self):
        self.write(self.initial)
        m.run_once(self.args())
        with self.src.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(self.assistant("Una prova incompleta"))[:-4])
        self.assertEqual(m.run_once(self.args())["updates"], 0)
        with self.src.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(self.assistant("Una prova incompleta"))[-4:] + "\n")
        # On receiving the final bytes the record becomes valid exactly once.
        self.assertEqual(m.run_once(self.args())["updates"], 1)
        self.assertEqual(m.run_once(self.args())["updates"], 0)

    def test_schema_metadata_from_canonical_db(self):
        db = self.root / "db.sqlite"
        with sqlite3.connect(db) as conn:
            conn.executescript("""
                CREATE TABLE prompt_codex (id TEXT PRIMARY KEY, project_id INT, title TEXT);
                CREATE TABLE projects (project_id INT PRIMARY KEY, slug TEXT);
                CREATE TABLE prompt_codex_chats (prompt_id TEXT, chat_id TEXT, bound_at TEXT);
                CREATE TABLE prompt_codex_descriptions (prompt_id TEXT, human_description TEXT);
                INSERT INTO projects VALUES (8, 'codex-usage-monitor');
                INSERT INTO prompt_codex VALUES ('P90', 8, 'Log leggibile');
                INSERT INTO prompt_codex_chats VALUES ('P90', '01a141a3-0879-7411-9245-6b144ba5ec41', '2026-10-16');
                INSERT INTO prompt_codex_descriptions VALUES ('P90', 'Tradurre i messaggi difficili.');
            """)
        result = m.linked_context(self.chat_id, db)
        self.assertEqual(result["project"], "codex-usage-monitor")
        self.assertEqual(result["prompts"][0]["id"], "P90")

    def test_observer_excluded(self):
        self.write(self.initial + [self.assistant("Message")])
        self.assertEqual(m.head_context(self.src)["objective"], "/goal sistemare il sistema di backup")
        data = m.read_new_events(self.src, {}, bootstrap=True)[1]
        self.assertEqual(len([d for d in data if d["kind"] == "message"]), 1)

    def test_redaction_and_fallback_flags(self):
        message = "key sk-ABCDEFGHIJKLMNOPQRST and user@example.com"
        safe = m.clean(message)
        self.assertNotIn("ABCDEFGHIJKLMNOP", safe)
        self.assertNotIn("@example.com", safe)
        self.assertEqual(m.fallback(["qualcosa"])["priorita"], "routine")


    def test_bootstrap_one_chat_does_not_replay_old_others(self):
        self.write(self.initial + [self.assistant("La prima chat ha finito.")])
        other = "01a141a3-0879-7411-9245-6b144ba5ec42"
        p2 = self.sessions / ("rollout-2026-10-16T01-00-01-" + other + ".jsonl")
        p2.write_text("\n".join(json.dumps(e) for e in [
            {"type": "session_meta", "payload": {"id": other, "cwd": "/home/daniele/projects/elsewhere"}},
            self.assistant("STORICO che non va notificato")]) + "\n")
        self.assertEqual(m.run_once(self.args(bootstrap_chat=self.chat_id))["updates"], 1)
        self.assertEqual(m.run_once(self.args())["updates"], 0)
        self.assertNotIn("STORICO che non va notificato", (self.state / "live.md").read_text())

    def test_new_sessions_after_init_are_reported(self):
        self.write(self.initial)
        self.assertEqual(m.run_once(self.args())["updates"], 0)
        other = "01a141a3-0879-7411-9245-6b144ba5ec42"
        p2 = self.sessions / ("rollout-2026-10-16T01-00-01-" + other + ".jsonl")
        p2.write_text("\n".join(json.dumps(e) for e in [
            {"type": "session_meta", "payload": {"id": other, "cwd": "/home/daniele/projects/elsewhere"}},
            self.assistant("Ho completato il controllo del nuovo progetto")]) + "\n")
        self.assertEqual(m.run_once(self.args())["updates"], 1)

    def test_model_input_excludes_tool_details(self):
        from unittest.mock import patch
        self.write(self.initial + [
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "output": "SECRET FROM TOOL"}},
            {"type": "response_item", "payload": {"type": "reasoning", "text": "SECRET FROM REASONING"}},
            self.assistant("Ho testato il progetto e ottenuto un risultato."),
        ])
        report = {key: "Non verificato" for key in m.FIELDS} | {"priorita": "routine"}
        with patch.object(m, "codex_translate", return_value=(report, "observer-id")) as translator:
            self.assertEqual(m.run_once(self.args(bootstrap_chat=self.chat_id, translator="codex"))["updates"], 1)
        payload = translator.call_args[0][0]
        self.assertNotIn("SECRET FROM TOOL", json.dumps(payload))
        self.assertNotIn("SECRET FROM REASONING", json.dumps(payload))

    def test_old_agent_message_format(self):
        self.write(self.initial)
        m.run_once(self.args())
        self.write([{"type": "event_msg", "payload": {
            "type": "agent_message", "message": "Abbiamo completato i controlli."}}])
        self.assertEqual(m.run_once(self.args())["updates"], 1)
        self.assertIn("Abbiamo completato", (self.state / "live.md").read_text())

    def test_observer_thread_is_not_processed(self):
        self.write(self.initial)
        m.run_once(self.args())
        other = "01a141a3-0879-7411-9245-6b144ba5ec42"
        p2 = self.sessions / ("rollout-2026-10-16T01-00-01-" + other + ".jsonl")
        p2.write_text("\n".join(json.dumps(e) for e in [
            {"type": "session_meta", "payload": {"id": other,
                "cwd": "/home/daniele/.local/state/codex-human-log/observer"}},
            self.assistant("QUESTA CHAT NON VA TRADOTTA")]) + "\n")
        self.assertEqual(m.run_once(self.args())["updates"], 0)
        self.assertNotIn("QUESTA CHAT NON VA TRADOTTA", (self.state / "state.json").read_text())


if __name__ == "__main__":
    unittest.main()
