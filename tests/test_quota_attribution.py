from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_monitor.capsules.quota import attribution


START = "2026-10-25T20:45:00Z"
END = "2026-10-25T21:00:00Z"


def native(path: Path, *, session_id: str, project: str, automation: str | None, before: int, after: int,
           session_start: str = "2026-10-20T10:00:00Z") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "session_meta", "timestamp": session_start,
         "payload": {"id": session_id, "cwd": f"/home/user/projects/{project}", "originator": "Codex Desktop"}},
        {"type": "event_msg", "timestamp": "2026-10-25T20:44:59Z",
         "payload": {"type": "token_count", "info": {"total_token_usage": {
             "total_tokens": before, "input_tokens": before - 5, "cached_input_tokens": before - 10,
             "output_tokens": 5}}}},
        {"type": "response_item", "timestamp": "2026-10-25T20:50:00Z",
         "payload": {"type": "message", "role": "user", "content": [
             {"type": "input_text", "text": f"<automation_id>{automation}</automation_id>" if automation else "Go"}]}},
        {"type": "event_msg", "timestamp": "2026-10-25T20:55:00Z",
         "payload": {"type": "token_count", "info": {"total_token_usage": {
             "total_tokens": after, "input_tokens": after - 10, "cached_input_tokens": after - 40,
             "output_tokens": 10}}}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def database() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE quota_snapshots(snapshot_id INTEGER PRIMARY KEY, acquired_at_utc TEXT,
          acquisition_status TEXT, weekly_used_percent REAL, weekly_reset_at_utc TEXT);
        CREATE TABLE quota_attributions(snapshot_id INTEGER PRIMARY KEY,
          previous_snapshot_id INTEGER, diagnostic_json TEXT, created_at_utc TEXT);
    """)
    con.executemany("INSERT INTO quota_snapshots VALUES (?, ?, ?, ?, ?)", [
        (1, START, "ok", 1.0, "2026-11-01T10:45:33Z"),
        (2, END, "ok", 2.0, "2026-11-01T10:45:33Z"),
    ])
    return con


class QuotaAttributionTests(unittest.TestCase):
    def test_ranks_native_deltas_and_recognizes_automatic_heartbeats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native(root / "2026/10/19/rollout-a.jsonl", session_id="a", project="video",
                   automation="monitor-privato-video-telegram", before=100, after=3100)
            native(root / "2026/10/20/rollout-b.jsonl", session_id="b", project="Bitwarden",
                   automation=None, before=100, after=600)
            report = attribution.inspect_rollouts(START, END, root)
            self.assertEqual(report["status"], "observed")
            self.assertEqual(report["observed_total_tokens"], 3500)
            self.assertEqual([s["project"] for s in report["sessions"]], ["video", "Bitwarden"])
            self.assertEqual(report["sessions"][0]["activation"], "automatic")
            self.assertEqual(report["sessions"][0]["automation_ids"], ["monitor-privato-video-telegram"])
            self.assertEqual(report["sessions"][0]["cached_input_tokens"], 2970)
            self.assertEqual(report["sessions"][0]["uncached_input_tokens"], 25)

    def test_persists_only_existing_quota_drop_in_canonical_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, database() as con:
            root = Path(tmp)
            native(root / "rollout-a.jsonl", session_id="a", project="video",
                   automation="heartbeat", before=100, after=300)
            report = attribution.build_attribution(con, 2, root)
            self.assertEqual(report["weekly_used_delta_pp"], 1.0)
            attribution.store_attribution(con, report)
            attribution.store_attribution(con, report)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM quota_attributions").fetchone()[0], 1)
            self.assertIn("heartbeat", "\n".join(attribution.attribution_lines(con, 2)))
            self.assertIsNone(attribution.build_attribution(con, 1, root))

    def test_skips_missing_historical_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native(root / "rollout-a.jsonl", session_id="a", project="old",
                   automation=None, before=100, after=300)
            path = root / "rollout-a.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows.pop(1)  # old session without any token event before the interval
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            result = attribution.inspect_rollouts(START, END, root)
            self.assertEqual(result["sessions"], [])

    def test_no_false_attribution_across_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, database() as con:
            con.execute("UPDATE quota_snapshots SET weekly_reset_at_utc=? WHERE snapshot_id=2",
                        ("2026-11-08T10:45:33Z",))
            self.assertIsNone(attribution.build_attribution(con, 2, Path(tmp)))

    def test_missing_local_rollouts_reported_as_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = attribution.inspect_rollouts(START, END, Path(tmp) / "absent")
            self.assertEqual(result["status"], "sessions_unavailable")


if __name__ == "__main__":
    unittest.main()
