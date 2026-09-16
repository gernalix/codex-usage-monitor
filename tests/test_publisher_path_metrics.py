from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_monitor.capsules.publishing import base as publisher


class PublisherPathMetricTests(unittest.TestCase):
    def test_apply_patch_resolves_existing_file_against_candidate_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            target = second / "module.py"
            target.write_text("VALUE = 1\n", encoding="utf-8")
            event = {
                "tool_name": "apply_patch",
                "content_text": json.dumps(
                    {
                        "patch": "*** Begin Patch\n*** Update File: module.py\n@@\n-VALUE = 1\n+VALUE = 2\n*** End Patch"
                    }
                ),
            }

            paths = publisher._apply_patch_write_paths_from_event(event, [first, second], None)

            self.assertEqual(paths, [str(target)])

    def test_ambiguous_add_file_is_not_misattributed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            event = {
                "tool_name": "apply_patch",
                "content_text": json.dumps(
                    {"patch": "*** Begin Patch\n*** Add File: new.py\n+VALUE = 1\n*** End Patch"}
                ),
            }

            paths = publisher._apply_patch_write_paths_from_event(event, [first, second], None)

            self.assertEqual(paths, [])

    def test_parse_session_dedupes_paths_and_prefers_single_write_repo(self) -> None:
        cycle = {
            "metrics": {
                "cycle_key": "cycle-1",
                "native_session_id": "session-1",
                "turn_id": "turn-1",
                "repo_paths": ["/repo/a", "/repo/a", "/repo/b"],
                "repo_write_paths": ["/repo/b/module.py"],
                "repo_project": "/repo/a",
                "final_response_redacted": "RESULT=PASS",
                "prompt_text_redacted": "PROMPT_ID=123456",
                "prompt_id": "123456",
            },
            "events": [],
            "source_sha256": "old",
            "final_event_id": "event-1",
        }

        def fake_repo_roots(paths: list[str]) -> list[Path]:
            if any(path.endswith("module.py") for path in paths):
                return [Path("/repo/b")]
            roots: list[Path] = []
            if "/repo/a" in paths:
                roots.append(Path("/repo/a"))
            if "/repo/b" in paths:
                roots.append(Path("/repo/b"))
            return roots

        with tempfile.TemporaryDirectory() as tmp, publisher.connect_state(Path(tmp)) as con, mock.patch.object(
            publisher, "_legacy_parse_session", return_value=([cycle], [])
        ), mock.patch.object(publisher, "_repo_roots", side_effect=fake_repo_roots):
            cycles, _events = publisher.parse_session(Path(tmp) / "ignored.jsonl", con)

        metrics = cycles[0]["metrics"]
        self.assertEqual(metrics["repo_paths"], ["/repo/a", "/repo/b"])
        self.assertEqual(metrics["repo_projects"], ["/repo/a", "/repo/b"])
        self.assertEqual(metrics["repo_write_projects"], ["/repo/b"])
        self.assertEqual(metrics["repo_project"], "/repo/b")
        self.assertNotIn("repo_write_paths", metrics)


if __name__ == "__main__":
    unittest.main()
