"""Synthetic component boundaries; no live collection/publication/notification."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import codex_human_log as human
from codex_monitor.capsules.session_archive import implementation as archive
from codex_monitor.capsules.publishing import chat_dump, legacy
from codex_monitor.capsules.quota import implementation as quota


class RedactionBoundaryTests(unittest.TestCase):
    def test_authentication_text_and_encoded_forms(self):
        marker = "SYNTHETIC_CREDENTIAL_VALUE"
        values = ["Authorization: Basic " + marker,
                  'credentials: "' + marker + '"',
                  "authorization:\n  credentials: |\n    " + marker,
                  json.dumps({"credentials": marker, "ordinary": "keep"}),
                  'password="alpha SYNTHETIC_REMAINDER gamma"',
                  '{"password": "short"}',
                  'client_secret: "' + marker + ' words"',
                  'credentials: "[REDACTED],' + marker + '"']
        values += ['password="abcdefghijklmnop' + sep + 'SYNTHETIC_REMAINDER"'
                   for sep in (",", "]", "}")]
        values += ["cmd --password 'alpha'\"SYNTHETIC_REMAINDER\"",
                   "credentials: [SYNTHETIC_CREDENTIAL_VALUE]",
                   r"https:\/\/fixture:SYNTHETIC_CREDENTIAL_VALUE@example.invalid/repo"]
        for value in values:
            for depth in range(3):
                with self.subTest(depth=depth, value=value):
                    saved = archive.redact_text(value)
                    self.assertNotIn(marker, saved)
                    self.assertNotIn("SYNTHETIC_REMAINDER", saved)
                    self.assertNotIn("short", saved)
                    self.assertEqual(archive.redact_text(saved), saved)
                    self.assertNotIn(marker, human.clean(value))
                value = json.dumps(value)

    def test_url_userinfo_and_legitimate_controls(self):
        for value in ("https://fixture:synthetic-password@example.invalid/repo.git",
                      "https://synthetic-token@example.invalid/repo.git",
                      "ssh://fixture:synthetic-password@example.invalid/repo"):
            saved = archive.redact_text(value)
            self.assertNotIn("synthetic", saved)
            self.assertIn("example.invalid/repo", saved)
            self.assertEqual(saved, archive.redact_text(saved))
        for value in ("git@example.invalid:repo.git", "ssh://git@example.invalid/repo",
                      "https://example.invalid/repo?branch=main", "commit " + "a1b2" * 10,
                      "command --password-file private.txt --token-count 32 --name worker"):
            self.assertEqual(value, archive.redact_text(value))

    def test_actual_native_chunk_and_human_event_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            marker = "SYNTHETIC_SECRET_VALUE"
            event = {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                     "content": [{"text": '{"password": "' + marker + '"}'}]}}
            source = root / "rollout.jsonl"
            source.write_text(json.dumps(event) + "\n")
            target = root / "chunk.jsonl"
            self.assertEqual(chat_dump.append_chunk(source, 0, target)[1], 1)
            self.assertNotIn(marker, target.read_text())
            json.loads(target.read_text())
            events = human.read_new_events(source, {}, bootstrap=True)[1]
            self.assertNotIn(marker, json.dumps(events))
            report = human.fallback([entry["text"] for entry in events])
            self.assertNotIn(marker, json.dumps(report))

    def test_human_standalone_layout_import_without_pythonpath(self):
        path = Path(human.__file__).resolve()
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        result = subprocess.run([sys.executable, "-B", str(path), "--help"],
                                cwd="/tmp", env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class PrivateRepositoryTests(unittest.TestCase):
    def test_requires_explicit_coherent_private_evidence(self):
        failures = [SimpleNamespace(returncode=1, stdout=""),
                    SimpleNamespace(returncode=0, stdout="not json")]
        failures += [SimpleNamespace(returncode=0, stdout=json.dumps(value)) for value in
                     ({}, [], None, {"isPrivate": True}, {"visibility": "PRIVATE"},
                      {"visibility": "PRIVATE", "isPrivate": False},
                      {"visibility": "PUBLIC", "isPrivate": True},
                      {"visibility": "INTERNAL", "isPrivate": True})]
        for failure in failures:
            with self.subTest(failure=failure), patch.object(legacy, "run", return_value=failure):
                with self.assertRaisesRegex(legacy.PublisherError, "PRIVACY_UNVERIFIED"):
                    legacy.assert_private_repo("fixture")
        for exc in (FileNotFoundError(), subprocess.TimeoutExpired("fixture", 1)):
            with patch.object(legacy, "run", side_effect=exc):
                with self.assertRaises(legacy.PublisherError):
                    legacy.assert_private_repo("fixture")
        with patch.object(legacy, "run", return_value=SimpleNamespace(returncode=0,
                stdout='{"visibility":"PRIVATE","isPrivate":true}')):
            legacy.assert_private_repo("fixture")

    def test_both_publishers_stop_before_writes_on_unverified_privacy(self):
        args = SimpleNamespace(remote="fixture", state_dir="/nonexistent-fixture",
                               source_root="/nonexistent-fixture", data_repo="/nonexistent-fixture")
        with patch.object(legacy, "run", return_value=SimpleNamespace(returncode=1, stdout="")):
            ensure = Mock(side_effect=AssertionError("repository write"))
            with self.assertRaises(legacy.PublisherError):
                legacy.command_run(args, ensure_repo_fn=ensure)
            ensure.assert_not_called()
            with patch.object(chat_dump.publisher, "ensure_repo", ensure):
                with patch.object(Path, "mkdir", side_effect=AssertionError("state write")):
                    # Privacy must precede even creation of local publisher state.
                    with self.assertRaises(legacy.PublisherError):
                        chat_dump.command_run(args)


class DiagnosticDatabaseTests(unittest.TestCase):
    def test_historical_cells_free_pages_and_binary_omitted_without_source_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, target = root / "original.sqlite", root / "copy.sqlite"
            marker = "SYNTHETIC_DB_CREDENTIAL"
            with closing(sqlite3.connect(source)) as con:
                con.executescript('CREATE TABLE sessions (archive_id TEXT PRIMARY KEY, git_remote TEXT, count INTEGER);'
                                  'CREATE TABLE extra (id TEXT PRIMARY KEY, text TEXT, data BLOB) WITHOUT ROWID;'
                                  'CREATE TABLE json_values (id INTEGER PRIMARY KEY, diagnostic_json TEXT);'
                                  'CREATE TABLE deleted (text TEXT);')
                con.execute("INSERT INTO sessions VALUES (?,?,?)", ("one", "https://fixture:" + marker + "@example.invalid/repo", 42))
                con.execute("INSERT INTO extra VALUES (?,?,?)", ("one", 'credentials: "' + marker + '"', marker.encode()))
                con.execute("INSERT INTO deleted VALUES (?)", (marker * 1000,))
                con.execute("INSERT INTO json_values VALUES (?,?)", (1, json.dumps({"credentials": marker, "total_tokens": 42})))
                con.execute("DELETE FROM deleted")
                con.commit()
            original = source.read_bytes()
            archive.diagnostic_sqlite_copy(source, target)
            self.assertEqual(source.read_bytes(), original)
            self.assertNotIn(marker.encode(), target.read_bytes())
            with closing(sqlite3.connect(target)) as con:
                remote, count = con.execute("SELECT git_remote,count FROM sessions").fetchone()
                self.assertEqual(count, 42)
                self.assertIn("example.invalid/repo", remote)
                self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(con.execute("SELECT id FROM extra").fetchone()[0], "one")
                decoded = json.loads(con.execute("SELECT diagnostic_json FROM json_values").fetchone()[0])
                self.assertEqual(decoded["total_tokens"], 42)
                self.assertEqual(decoded["credentials"], "[REDACTED]")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)


class OwnedQuotaTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "synthetic-server"

    def child(self, body):
        self.path.write_text("#!" + sys.executable + "\n" + body)
        self.path.chmod(0o700)
        return SimpleNamespace(codex_bin=str(self.path), startup_timeout_sec=0.3,
                               source_timeout_sec=0.3, app_server_port=38655)

    def test_owned_child_handshake_notifications_and_cleanup(self):
        cfg = self.child('''import json,sys
for line in sys.stdin:
    request=json.loads(line)
    assert "jsonrpc" not in request
    if request["method"]=="initialize":
        print(json.dumps({"method":"notice","params":{}}),flush=True)
        print(json.dumps({"id":request["id"],"result":{}}),flush=True)
    elif request["method"]=="initialized":
        initialized=True
    else:
        assert initialized and request["method"]=="account/rateLimits/read"
        print(json.dumps({"id":999,"result":{}}),flush=True)
        print(json.dumps({"id":request["id"],"result":{"rateLimits":{"fixture":42}}}),flush=True)
''')
        original = quota.AppServerManager
        managers = []
        def manager(config):
            value = original(config)
            managers.append(value)
            return value
        with patch.object(quota, "AppServerManager", side_effect=manager), \
             patch.object(quota, "http_ready", side_effect=AssertionError("loopback readiness")), \
             patch.object(quota, "WebSocketClient", side_effect=AssertionError("network transport")):
            self.assertEqual(quota.read_rate_limits(cfg), {"rateLimits": {"fixture": 42}})
        proc = managers[0].process
        self.assertIsNotNone(proc.poll())
        self.assertTrue(proc.stdin.closed and proc.stdout.closed)

    def test_bad_timeout_eof_and_oversized_responses_fail_closed_and_reap(self):
        bodies = ["import time; time.sleep(10)", "pass", "print('not-json',flush=True)",
                  "print('[]',flush=True)", "print('x'*1000,flush=True)"]
        for body in bodies:
            with self.subTest(body=body):
                cfg = self.child(body)
                manager = quota.AppServerManager(cfg)
                with patch.object(quota, "AppServerManager", return_value=manager), \
                     patch.object(quota.StdioClient, "MAX_MESSAGE_BYTES", 512):
                    with self.assertRaises(quota.SourceError):
                        quota.read_rate_limits(cfg)
                self.assertIsNotNone(manager.process.poll())
                self.assertTrue(manager.process.stdout.closed)


if __name__ == "__main__":
    unittest.main()
