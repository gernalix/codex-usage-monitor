from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import codex_usage_publisher as publisher


class GoalAbortRecoveryTest(unittest.TestCase):
    def test_completed_goal_survives_later_abort_and_followup_does_not_steal_prompt_id(self) -> None:
        session_id = "01a0a93b-8941-7d52-b53e-7f5523e84275"
        goal_turn = "01a0a93b-c71a-71a2-b616-7e9d6d9765d4"
        followup_turn = "01a0a945-9999-7000-8000-000000000001"
        rows = [
            {
                "timestamp": "2026-09-16T08:01:00Z",
                "type": "session_meta",
                "payload": {"id": session_id, "cwd": "/tmp/project"},
            },
            {
                "timestamp": "2026-09-16T08:01:02Z",
                "type": "turn_context",
                "payload": {"turn_id": goal_turn, "model": "gpt-5.5", "effort": "medium", "cwd": "/tmp/project"},
            },
            {
                "timestamp": "2026-09-16T08:01:02Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "/goal Read the Codex goal objective file at /home/test/.codex/attachments/deadbeef/goal-objective.md before continuing.\n",
                        }
                    ],
                },
            },
            {
                "timestamp": "2026-09-16T08:01:07Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "objective-read",
                    "arguments": json.dumps(
                        {
                            "cmd": "sed -n '1,220p' /home/test/.codex/attachments/deadbeef/goal-objective.md",
                            "workdir": "/tmp/project",
                        }
                    ),
                },
            },
            {
                "timestamp": "2026-09-16T08:01:08Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "objective-read",
                    "output": "PROMPT_ID=529184 | project_id=49 | MegaVault=STRICT\n",
                },
            },
            {
                "timestamp": "2026-09-16T08:08:55Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "update_goal",
                    "call_id": "goal-complete",
                    "arguments": json.dumps({"status": "complete"}),
                },
            },
            {
                "timestamp": "2026-09-16T08:08:56Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "goal-complete",
                    "output": json.dumps(
                        {
                            "goal": {
                                "threadId": session_id,
                                "status": "complete",
                                "tokensUsed": 126386,
                                "timeUsedSeconds": 473,
                                "createdAt": 1789545662,
                                "updatedAt": 1789546135,
                            }
                        }
                    ),
                },
            },
            {
                "timestamp": "2026-09-16T08:08:56Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 80,
                            "output_tokens": 5,
                            "reasoning_output_tokens": 2,
                            "total_tokens": 105,
                        }
                    },
                },
            },
            {
                "timestamp": "2026-09-16T08:11:43Z",
                "type": "event_msg",
                "payload": {
                    "type": "turn_aborted",
                    "turn_id": goal_turn,
                    "started_at": 1789545662,
                    "completed_at": 1789546303,
                    "duration_ms": 640823,
                    "reason": "interrupted",
                },
            },
            {
                "timestamp": "2026-09-16T08:13:10Z",
                "type": "turn_context",
                "payload": {"turn_id": followup_turn, "model": "gpt-5.5", "effort": "medium", "cwd": "/tmp/project"},
            },
            {
                "timestamp": "2026-09-16T08:13:10Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "qual è il prompt id su cui hai lavorato?\n"}],
                },
            },
            {
                "timestamp": "2026-09-16T08:13:14Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Ho lavorato su `PROMPT_ID=529184`. Goal completato; uso finale 126386 token.",
                        }
                    ],
                    "internal_chat_message_metadata_passthrough": {"turn_id": followup_turn},
                },
            },
            {
                "timestamp": "2026-09-16T08:13:14Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": followup_turn,
                    "started_at": 1789546390,
                    "completed_at": 1789546394,
                    "duration_ms": 4000,
                    "last_agent_message": "Ho lavorato su `PROMPT_ID=529184`. Goal completato; uso finale 126386 token.",
                },
            },
        ]

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rollout = root / f"rollout-{session_id}.jsonl"
            rollout.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            with publisher.connect_state(root / "state") as con:
                cycles, _events = publisher.parse_session(rollout, con)

        self.assertEqual(len(cycles), 2)
        recovered = next(cycle for cycle in cycles if cycle["metrics"].get("completion_state") == "goal_complete_turn_aborted")
        followup = next(cycle for cycle in cycles if cycle["metrics"].get("turn_id") == followup_turn)

        self.assertEqual(recovered["metrics"]["prompt_id"], "529184")
        self.assertEqual(recovered["metrics"]["prompt_id_source"], "goal_objective_output")
        self.assertEqual(recovered["metrics"]["total_tokens"], 126386)
        self.assertEqual(recovered["metrics"]["duration_seconds"], 473.0)
        self.assertEqual(recovered["metrics"]["status"], "PASS")

        self.assertIsNone(followup["metrics"]["prompt_id"])
        self.assertTrue(followup["metrics"]["prompt_id_rejected_from_final_response"])


if __name__ == "__main__":
    unittest.main()
