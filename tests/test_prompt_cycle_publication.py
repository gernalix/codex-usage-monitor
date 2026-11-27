from __future__ import annotations

import json
import os
import stat
from unittest.mock import patch
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

    def test_repeat_export_preserves_all_identical_files_including_aliases_and_indexes(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(publisher._base, "_should_export", True):
            repo = Path(temp)
            cycles = self.cycles()
            cycles.reverse()  # The latest alias follows the timestamp, independent of input order.
            cycles[0]["events"][0]["content_text"] = "città" * 18000
            cycle = dict(cycles[0], metrics={**cycles[0]["metrics"], "prompt_id": None, "cycle_key": "unassigned"})
            cycles.append(cycle)
            quota = repo / "missing.sqlite"
            publisher.export_repo(repo, cycles, {}, quota)
            files = [p for p in repo.rglob("*") if p.is_file()]
            before = {p: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in files}
            publisher.export_repo(repo, cycles, {}, quota)
            self.assertEqual(before, {p: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in files})
            alias = repo / "prompts/583214/transcript.jsonl"
            expected = before[alias][2]
            alias.write_bytes(expected.replace(b"\n", b"\r\n"))
            publisher.export_repo(repo, cycles, {}, quota)
            self.assertEqual(alias.read_bytes(), expected)

    def test_export_restores_private_independent_regular_files(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            path = repo / "record.json"
            publisher._base.write_json(path, {"id": 1})
            sibling = repo / "sibling"
            os.link(path, sibling)
            publisher._base.write_json(path, {"id": 1})
            self.assertNotEqual(path.stat().st_ino, sibling.stat().st_ino)
            path.unlink()
            path.symlink_to(sibling)
            publisher._base.write_json(path, {"id": 1})
            self.assertFalse(path.is_symlink())
            path.chmod(0o644)
            publisher._base.write_json(path, {"id": 1})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

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
            self.assertEqual(flat["cycle_id"], "cycle-b")
            self.assertNotIn("cycle_key", flat)
            self.assertEqual([row["cycle_id"] for row in index_rows], ["cycle-a", "cycle-b"])
            self.assertTrue(all("cycle_key" not in row for row in index_rows))

            latest = json.loads((repo / "index/latest.json").read_text(encoding="utf-8"))
            self.assertEqual(latest["latest_prompt"]["path"], "prompts/583214/cycles/cycle-b")

    def test_transcript_redacts_provider_tokens_across_records(self) -> None:
        digest = "f9e813a7ca05132b6d2c" * 2
        provider = "check_Source" + "GraphContext"
        cycles = self.cycles()
        cycles[0]["events"] = [
            {"role": "tool", "content_text": "reported value " + digest, "count": 2},
            {"role": "assistant", "content_text": provider + " diagnostic"},
        ]
        cycles[1]["events"] = [{"role": "assistant", "content_text": "Git ref " + digest}]
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            publisher.export_repo(repo, cycles, {7: cycles[0]["events"]}, Path(temporary) / "missing.sqlite")
            affected = repo / "prompts/583214/cycles/cycle-a/transcript.jsonl"
            rows = [json.loads(line) for line in affected.read_text().splitlines()]
            self.assertEqual(rows[0], {"role": "tool", "content_text": "reported value [REDACTED]", "count": 2})
            self.assertEqual(rows[1], cycles[0]["events"][1])
            self.assertIn(digest, (repo / "prompts/583214/cycles/cycle-b/transcript.jsonl").read_text())
            self.assertIn(digest, cycles[0]["events"][0]["content_text"])

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
