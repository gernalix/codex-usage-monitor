from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import codex_usage_publisher as publisher


class GoalObjectiveSourcePrecedenceTest(unittest.TestCase):
    def test_goal_objective_output_overrides_earlier_prompt_id_source(self) -> None:
        session_id = "01a0a93b-8941-7d52-b53e-7f5523e84275"
        events = [
            {
                "timestamp_utc": "2026-09-16T08:01:02Z",
                "top_type": "turn_context",
                "subtype": None,
                "role": None,
                "tool_name": None,
                "content_text": "",
            },
            {
                "timestamp_utc": "2026-09-16T08:01:02Z",
                "top_type": "response_item",
                "subtype": "message",
                "role": "user",
                "tool_name": None,
                "content_text": "PROMPT_ID=529184 /goal wrapper whose attachment was already resolvable",
            },
            {
                "timestamp_utc": "2026-09-16T08:01:07Z",
                "top_type": "response_item",
                "subtype": "function_call",
                "role": None,
                "tool_name": "exec_command",
                "content_text": json.dumps({"cmd": "cat /home/test/.codex/attachments/deadbeef/goal-objective.md"}),
            },
            {
                "timestamp_utc": "2026-09-16T08:01:08Z",
                "top_type": "response_item",
                "subtype": "function_call_output",
                "role": None,
                "tool_name": None,
                "content_text": "PROMPT_ID=529184 | project_id=49 | MegaVault=STRICT",
            },
            {
                "timestamp_utc": "2026-09-16T08:08:56Z",
                "top_type": "response_item",
                "subtype": "function_call_output",
                "role": None,
                "tool_name": None,
                "content_text": json.dumps(
                    {
                        "goal": {
                            "status": "complete",
                            "tokensUsed": 126386,
                            "timeUsedSeconds": 473,
                            "createdAt": 1789545662,
                            "updatedAt": 1789546135,
                        }
                    }
                ),
            },
            {
                "timestamp_utc": "2026-09-16T08:11:43Z",
                "top_type": "event_msg",
                "subtype": "turn_aborted",
                "role": None,
                "tool_name": None,
                "content_text": "",
            },
        ]

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rollout = root / f"rollout-{session_id}.jsonl"
            with publisher.connect_state(root / "state") as con:
                recovered = publisher._recover_completed_goal_aborts(rollout, con, events)

        self.assertEqual(len(recovered), 1)
        metrics = recovered[0]["metrics"]
        self.assertEqual(metrics["prompt_id"], "529184")
        self.assertEqual(metrics["prompt_id_source"], "goal_objective_output")
        self.assertEqual(metrics["total_tokens"], 126386)
        self.assertEqual(metrics["duration_seconds"], 473.0)


if __name__ == "__main__":
    unittest.main()
