from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from codex_monitor.capsules.publishing import implementation as publisher


class PromptCyclePublicationTests(unittest.TestCase):
    @staticmethod
    def cycles() -> list[dict[str, object]]:
        return [
            {
                "metrics": {
                    "prompt_id": "583214",
                    "cycle_key": "cycle-a",
                    "chat_id": 1,
                    "native_session_id": "session-a",
                    "timestamp_end_utc": "2026-09-03T17:25:15Z",
                    "status": "PASS",
                },
                "events": [{"content_text": "first"}],
            },
            {
                "metrics": {
                    "prompt_id": "583214",
                    "cycle_key": "cycle-b",
                    "chat_id": 2,
                    "native_session_id": "session-b",
                    "timestamp_end_utc": "2026-09-16T09:49:47Z",
                    "status": "UNKNOWN",
                },
                "events": [{"content_text": "second"}],
            },
        ]

    def test_same_prompt_id_preserves_each_cycle_and_flat_latest_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()

            old_should_export = publisher._base._should_export
            try:
                publisher._base._should_export = True
                publisher.export_repo(repo, self.cycles(), {}, root / "missing.sqlite")
            finally:
                publisher._base._should_export = old_should_export

            index_rows = [
                json.loads(line)
                for line in (repo / "index/prompts.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(
                [row["path"] for row in index_rows],
                [
                    "prompts/583214/cycles/cycle-a",
                    "prompts/583214/cycles/cycle-b",
                ],
            )
            self.assertTrue((repo / "prompts/583214/cycles/cycle-a/metrics.json").is_file())
            self.assertTrue((repo / "prompts/583214/cycles/cycle-b/metrics.json").is_file())

            flat = json.loads((repo / "prompts/583214/metrics.json").read_text(encoding="utf-8"))
            self.assertEqual(flat["cycle_key"], "cycle-b")

            latest = json.loads((repo / "index/latest.json").read_text(encoding="utf-8"))
            self.assertEqual(latest["latest_prompt"]["path"], "prompts/583214/cycles/cycle-b")

    def test_missing_stable_layout_backfills_even_when_no_cycle_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()

            old_should_export = publisher._base._should_export
            try:
                publisher._base._should_export = False
                publisher.export_repo(repo, self.cycles(), {}, root / "missing.sqlite")
            finally:
                publisher._base._should_export = old_should_export

            self.assertTrue((repo / "prompts/583214/cycles/cycle-a/metrics.json").is_file())
            self.assertTrue((repo / "prompts/583214/cycles/cycle-b/metrics.json").is_file())
            rows = [
                json.loads(line)
                for line in (repo / "index/prompts.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(rows), 2)
            self.assertTrue(all("/cycles/" in row["path"] for row in rows))


if __name__ == "__main__":
    unittest.main()
