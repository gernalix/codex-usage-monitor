"""Focused tests for passive Codex live telemetry; no external services."""
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from codex_monitor.capsules.quota import live_dashboard as live


def write_rollout(path, now, *, new=True, totals=(100, 210)):
    path.parent.mkdir(parents=True, exist_ok=True)
    sid = "12345678-1234-1234-1234-123456789012"
    ts = lambda offset: live.utc(now + offset)
    rows = [
        {"type": "session_meta", "timestamp": ts(-20 if new else -7200),
         "payload": {"id": sid, "cwd": "/home/test/projects/ANALYTICS",
                     "originator": "codex_exec", "base_instructions": "SECRET-NOT-EXPOSED"}},
        {"type": "response_item", "timestamp": ts(-18),
         "payload": {"type": "message", "content": "SECRET-NOT-EXPOSED"}},
        {"type": "event_msg", "timestamp": ts(-12),
         "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": totals[0]}}}},
        {"type": "event_msg", "timestamp": ts(-3),
         "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": totals[1]}}}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return sid


class LiveDashboardTests(unittest.TestCase):
    def test_recent_tokens_and_no_prompt_leaks(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "rollout-a.jsonl"
            sid = write_rollout(path, now)
            session = live.recent_session(path, now)
            self.assertEqual(session["session_id"], sid)
            self.assertEqual(session["project"], "ANALYTICS")
            self.assertEqual(session["tokens_60s"], 210)
            self.assertNotIn("SECRET", json.dumps(session))
            self.assertEqual(live.recent_sessions(root, now)["status"], "observed")

    def test_existing_session_uses_only_confirmed_delta(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-a.jsonl"
            write_rollout(path, now, new=False)
            result = live.recent_session(path, now)
            self.assertEqual(result["tokens_60s"], 110)
            self.assertTrue(result["partial"])

    def test_old_token_events_do_not_imply_live_activity(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-a.jsonl"
            write_rollout(path, now - 1800)
            self.assertIsNone(live.recent_session(path, now))
            self.assertEqual(live.recent_sessions(Path(tmp), now)["sessions"], [])

    def test_no_attribution_from_running_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for pid, cmd in [
                (20, b"/usr/lib/chatgpt/resources/codex\0app-server\0"),
                (21, b"/usr/lib/chatgpt/ChatGPT\0"),
                (22, b"/usr/lib/chatgpt/ChatGPT\0--type=renderer\0"),
                (23, b"/usr/bin/python3\0some-other-service\0"),
            ]:
                node = root / str(pid)
                node.mkdir()
                (node / "cmdline").write_bytes(cmd)
            result = live.codex_processes(root)
            self.assertEqual([r["pid"] for r in result], [20, 21])
            self.assertTrue(all("non dimostrato" in r["note"] for r in result))

    def test_readonly_existing_quota_and_drop(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "quota.db"
            with sqlite3.connect(db) as con:
                con.executescript(
                    "CREATE TABLE quota_snapshots(snapshot_id INTEGER PRIMARY KEY, acquired_at_utc TEXT, "
                    "acquisition_status TEXT, weekly_used_percent REAL, weekly_remaining_percent REAL, "
                    "weekly_reset_at_utc TEXT);"
                    "CREATE TABLE quota_attributions(snapshot_id INTEGER PRIMARY KEY, diagnostic_json TEXT);"
                )
                con.executemany("INSERT INTO quota_snapshots VALUES (?,?,?,?,?,?)", [
                    (1, "2026-11-18T10:00:00Z", "ok", 18, 82, "2026-11-20T00:00:00Z"),
                    (2, "2026-11-18T10:15:00Z", "ok", 19, 81, "2026-11-20T00:00:00Z"),
                ])
                con.execute("INSERT INTO quota_attributions VALUES (?,?)",
                            (2, json.dumps({"sessions": [], "observed_total_tokens": 0})))
            value = live.quota_snapshot(db)
            self.assertEqual(value["remaining_percent"], 81)
            self.assertEqual(value["last_drop"]["delta_pp"], 1)
            self.assertEqual(value["last_drop"]["local_sessions"], 0)
            self.assertEqual(live.quota_snapshot(db), value)

    def test_dashboard_asset_is_in_immutable_runtime_manifest(self):
        from codex_monitor.capsules.runtime_deploy.implementation import runtime_hashes
        hashes = runtime_hashes(Path(__file__).resolve().parents[1])
        self.assertIn("codex_live_dashboard.py", hashes)
        self.assertIn("src/codex_monitor/capsules/quota/live_dashboard.html", hashes)


if __name__ == "__main__":
    unittest.main()
