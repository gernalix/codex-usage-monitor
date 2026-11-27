import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_monitor.capsules.publishing import implementation as publisher, legacy
from codex_monitor.capsules.publishing.verified_transcripts import VerifiedTranscripts
from tests.test_usage_publisher import session_rows, write_jsonl


class VerifiedTranscriptTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.repo = root / "repo"
        self.source = root / "input.jsonl"
        write_jsonl(self.source, session_rows("session-id"))
        self.con = publisher.connect_state(root / "state")
        self.addCleanup(self.con.close)
        self.initial = self.cache()
        self.expected = self.parse(self.initial)
        chat_id = self.expected[0][0]["metrics"]["chat_id"]
        self.export = self.repo / "chats" / str(chat_id) / "transcript.jsonl"
        legacy.write_jsonl(self.export, self.expected[1])
        self.initial.confirm(self.con)
        self.con.commit()

    def cache(self, version="rules-1"):
        return VerifiedTranscripts(self.repo, version)

    def parse(self, cache):
        return publisher.parse_session(self.source, self.con, transcripts=cache)

    def test_reopen_and_reuse_all_events_with_live_metrics(self):
        cache = self.cache()
        with mock.patch.object(legacy.archive, "redact_text", wraps=legacy.archive.redact_text) as clean:
            self.assertEqual(self.parse(cache), self.expected)
        self.assertEqual(cache.reused, 1)
        self.assertEqual(clean.call_count, 1)  # Independent final-response normalization.
        self.assertFalse(cache.repair_export)

    def test_change_to_raw_bytes_even_if_normalized_text_is_identical(self):
        self.source.write_bytes(self.source.read_bytes().replace(b"\n", b"\r\n"))
        cache = self.cache()
        self.assertEqual(self.parse(cache), self.expected)
        self.assertEqual(cache.reused, 0)
        self.assertFalse(cache.repair_export)

    def test_changed_rules_and_missing_or_damaged_export_force_full_cleaning(self):
        before = self.cache().snapshot_generation()
        for condition in ("new rules", "damaged", "missing", "symlink"):
            with self.subTest(condition=condition):
                if condition == "damaged":
                    self.export.write_bytes(self.export.read_bytes().replace(b"do it", b"wrong"))
                elif condition == "missing":
                    self.export.unlink()
                elif condition == "symlink":
                    self.export.symlink_to(self.source)
                cache = self.cache("rules-2" if condition == "new rules" else "rules-1")
                self.assertEqual(self.parse(cache), self.expected)
                self.assertTrue(cache.repair_export)
                self.assertEqual(cache.reused, 0)
                self.assertNotEqual(cache.snapshot_generation(), before)

    def test_persist_only_confirmed_export_and_no_transcript_text(self):
        self.con.execute("DELETE FROM verified_transcripts")
        self.con.commit()
        self.export.write_bytes(b"unrelated\n")
        cache = self.cache()
        self.parse(cache)
        cache.confirm(self.con)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM verified_transcripts").fetchone()[0], 0)
        self.export.unlink()
        legacy.write_jsonl(self.export, self.expected[1])
        self.parse(cache)
        cache.confirm(self.con)
        row = dict(self.con.execute("SELECT * FROM verified_transcripts").fetchone())
        self.assertEqual(set(row), {"source_path", "generation", "source_hash", "export_hash", "export_size", "chat_id"})
        self.assertEqual(len(row["source_hash"]), 64)

    def test_fragment_masking_cannot_seed_a_receipt_for_different_event_text(self):
        self.con.execute("DELETE FROM verified_transcripts")
        self.con.commit()
        cache = self.cache()
        with mock.patch.object(legacy.archive, "has_sourcegraph_context", return_value=True), \
             mock.patch.object(legacy.archive, "redact_bare_sourcegraph_tokens", return_value={"masked": True}):
            self.parse(cache)
        self.assertFalse(cache.pending)
        self.assertEqual(cache.reused, 0)

    def test_normal_entry_point_repairs_a_modified_export_even_with_idle_sources(self):
        import contextlib
        import io
        import json
        from codex_monitor.capsules.publishing import base

        root = self.repo.parent / "entry"
        source = root / "sessions"
        repo = root / "repo"
        write_jsonl(source / "one.jsonl", session_rows("one"))
        write_jsonl(source / "two.jsonl", session_rows("two", prompt_id="234567"))
        base.run(["git", "init", "-b", "main", str(repo)])
        base.run(["git", "config", "user.email", "test@example.invalid"], repo)
        base.run(["git", "config", "user.name", "Test"], repo)
        args = publisher.build_parser().parse_args(["--source-root", str(source), "--state-dir",
                                                    str(root / "state"), "--data-repo", str(repo), "run"])
        def local_git(cmd, cwd, timeout=120):
            if cmd[:2] != ["git", "push"]:
                base.run(cmd, cwd)

        with mock.patch.object(base, "assert_private_repo"), mock.patch.object(base, "ensure_repo"), \
             mock.patch.object(base, "send_batch_telegram"), mock.patch.object(base, "git_ok", local_git):
            self.assertEqual(publisher.command_run(args), 0)
            export = repo / "chats/1/transcript.jsonl"
            original = export.read_bytes()
            export.write_bytes(original.replace(b"do it", b"other"))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(publisher.command_run(args), 0)
            self.assertEqual(export.read_bytes(), original)
            report = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(report["status"], "noop")
            self.assertEqual((report["transcripts_checked"], report["transcripts_reused"]), (2, 1))
