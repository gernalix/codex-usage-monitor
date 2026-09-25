from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from codex_monitor.capsules.orchestration import implementation as orch


class OrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.roadmap_repo = self.root / "roadmap"
        self.state_dir = self.roadmap_repo / "operations/task-state"
        self.state_dir.mkdir(parents=True)
        self.roadmap_db = self.roadmap_repo / "roadmap.sqlite"
        self.history_db = self.root / "prompt_history.sqlite"
        self._make_roadmap()
        self._make_history()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_roadmap(self) -> None:
        conn = sqlite3.connect(self.roadmap_db)
        conn.executescript(
            """
            CREATE TABLE prompts(
              prompt_id TEXT PRIMARY KEY,
              title TEXT, project_id TEXT, project_name TEXT, repo TEXT,
              prompt_type TEXT, model TEXT, reasoning TEXT, megavault_mode TEXT,
              queue_position INTEGER, current_path TEXT, status TEXT,
              created_at TEXT, updated_at TEXT
            );
            CREATE VIEW v_runnable_prompts AS
              SELECT * FROM prompts WHERE status='pending';
            """
        )
        conn.executemany(
            """INSERT INTO prompts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    "111111", "Ready", "8", "C2", "repo", "Prompt",
                    "GPT", "low", "FAST", 1, "prompts/ready.md", "pending",
                    "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
                ),
                (
                    "222222", "Running", "8", "C2", "repo", "Prompt",
                    "GPT", "medium", "FAST", 2, "prompts/run.md", "running",
                    "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z",
                ),
            ],
        )
        conn.commit()
        conn.close()
        (self.state_dir / "222222.md").write_text(
            "# state\n\n## Next action\nContinue from the checkpoint.\n",
            encoding="utf-8",
        )

    def _make_history(self) -> None:
        conn = sqlite3.connect(self.history_db)
        conn.executescript(
            """
            CREATE TABLE prompts(
              prompt_uid TEXT PRIMARY KEY, prompt_id TEXT, source TEXT,
              conversation_id TEXT, message_id TEXT, role TEXT, title TEXT,
              prompt_text TEXT, created_at TEXT, repo TEXT, task_type TEXT
            );
            CREATE TABLE source_records(
              source TEXT, source_key TEXT, payload_hash TEXT, kind TEXT,
              ingested_at TEXT
            );
            CREATE VIRTUAL TABLE prompt_fts USING fts5(
              prompt_uid UNINDEXED,title,prompt_text,repo,task_type
            );
            """
        )
        conn.execute(
            "INSERT INTO prompts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "chat:1", None, "chatgpt", "conv", "m1", "user",
                "Old chat", "fix Android navigation bug", "2026-01-01",
                "PersonalHub", "debug",
            ),
        )
        conn.execute(
            "INSERT INTO prompt_fts VALUES(?,?,?,?,?)",
            ("chat:1", "Old chat", "fix Android navigation bug", "PersonalHub", "debug"),
        )
        conn.execute(
            "INSERT INTO source_records VALUES(?,?,?,?,?)",
            ("chatgpt", "k", "h", "prompt", "2026-01-01T00:00:00Z"),
        )
        conn.commit()
        conn.close()

    def test_status_joins_roadmap_checkpoint_and_history(self) -> None:
        payload = orch.orchestrator_status(
            self.roadmap_db, self.roadmap_repo, self.history_db
        )
        self.assertEqual(payload["roadmap"]["status_counts"]["running"], 1)
        self.assertEqual(
            payload["roadmap"]["running"][0]["checkpoint"]["next_action"],
            "Continue from the checkpoint.",
        )
        self.assertEqual(payload["history"]["prompts"], 1)

    def test_search_uses_shared_history_read_only(self) -> None:
        rows = orch.context_search("Android navigation", self.history_db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "chatgpt")
    def test_claim_delegates_to_roadmap_start_helper(self) -> None:
        tools = self.roadmap_repo / "tools"
        tools.mkdir()
        script = tools / "roadmap_start.py"
        script.write_text(
            "import json\nprint(json.dumps({'status':'ok','prompt_id':'111111'}))\n",
            encoding="utf-8",
        )
        payload = orch.claim_prompt("111111", self.roadmap_repo, timeout=2)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["prompt_id"], "111111")


if __name__ == "__main__":
    unittest.main()
