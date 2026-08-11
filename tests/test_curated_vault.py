import json
from pathlib import Path
import tempfile
import unittest

import codex_curated_vault as vault


class CuratedVaultTests(unittest.TestCase):
    def test_extracts_links_final_report_and_bottlenecks(self):
        sid = "019fe7aa-38cc-7991-b9bb-07dcdf755ae8"
        events = [
            {"session_id": sid, "timestamp_utc": "2026-08-09T17:55:28Z", "top_type": "session_meta", "subtype": None, "role": None, "content_text": ""},
            {"session_id": sid, "timestamp_utc": "2026-08-09T17:55:28Z", "top_type": "response_item", "subtype": "message", "role": "user", "content_text": "# AGENTS.md instructions\nignore for curated transcript"},
            {"session_id": sid, "timestamp_utc": "2026-08-09T17:55:29Z", "top_type": "response_item", "subtype": "message", "role": "user", "content_text": "PROMPT_ID=731462\nproject_id=8\nCheck archive."},
            {"session_id": sid, "timestamp_utc": "2026-08-09T17:55:30Z", "top_type": "response_item", "subtype": "message", "role": "assistant", "content_text": "The first query failed; retrying with the real schema."},
            {"session_id": sid, "timestamp_utc": "2026-08-09T17:55:31Z", "top_type": "response_item", "subtype": "function_call", "role": None, "tool_name": "exec_command", "content_text": '{"cmd":"sqlite3 vault.sqlite .schema"}'},
            {"session_id": sid, "timestamp_utc": "2026-08-09T17:55:32Z", "top_type": "response_item", "subtype": "function_call_output", "role": None, "content_text": "Process exited with code 1\nError: no such table"},
            {"session_id": sid, "timestamp_utc": "2026-08-09T17:55:33Z", "top_type": "response_item", "subtype": "reasoning", "role": None, "content_text": "Fallback is needed because the schema is missing."},
            {"session_id": sid, "timestamp_utc": "2026-08-09T17:55:40Z", "top_type": "response_item", "subtype": "message", "role": "assistant", "content_text": "PROMPT_ID=731462\nESITO=OK"},
            {"session_id": sid, "timestamp_utc": "2026-08-09T17:55:40Z", "top_type": "event_msg", "subtype": "task_complete", "role": None, "content_text": ""},
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            normalized = root / "normalized/sessions"
            normalized.mkdir(parents=True)
            source = normalized / f"{sid}.jsonl"
            source.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")

            meta = vault.parse_session(source)
            self.assertEqual(meta["prompt_ids"], ["731462"])
            self.assertEqual(meta["project_ids"], ["8"])
            self.assertEqual(len(meta["user_items"]), 1)
            self.assertEqual(len(meta["reports"]), 1)
            self.assertEqual(len(meta["bottlenecks"]), 3)

            out = root / "vault"
            vault.write_session(out, meta)
            vault.rebuild_indexes(out, {sid: meta})

            chat = (out / "chats" / f"{sid}.md").read_text(encoding="utf-8")
            prompt = (out / "prompts/731462.md").read_text(encoding="utf-8")
            project = (out / "projects/8.md").read_text(encoding="utf-8")
            self.assertIn("2026-08-09T19:55:29+02:00", chat)
            self.assertIn("[[prompts/731462|731462]]", chat)
            self.assertIn(f"[[chats/{sid}|{sid}]]", prompt)
            self.assertIn("## Final reports", project)
            self.assertIn("## Bottleneck signals", project)


if __name__ == "__main__":
    unittest.main()
