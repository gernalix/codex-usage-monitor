#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

import codex_usage_publisher_base as _base
from codex_usage_publisher_base import *  # noqa: F401,F403


VERSION = "2026.09.16.1"
_ORIGINAL_LEGACY_PARSE_SESSION = _base._legacy_parse_session
_BASE_PARSE_SESSION = _base.parse_session


def _goal_completion(payload: dict[str, Any]) -> dict[str, Any] | None:
    raw = payload.get("output")
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    goal = parsed.get("goal") if isinstance(parsed, dict) else None
    if not isinstance(goal, dict) or str(goal.get("status") or "").lower() != "complete":
        return None
    return goal


def _recover_completed_goal_aborts(path: Path, con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Recover a completed /goal turn when the user interrupts after goal completion.

    Codex can emit a structured update_goal(status=complete) and only later a
    turn_aborted event. The legacy publisher only closes cycles on task_complete,
    so that otherwise drops the substantive task and its usage completely.
    """
    session_id = _base.legacy.archive.first_uuid_from_path(path) or path.stem
    chat_id: int | None = None
    session_cwd: str | None = None
    current: dict[str, Any] | None = None
    recovered: list[dict[str, Any]] = []
    raw_lines = path.read_text(encoding="utf-8", errors="replace")
    source_sha = _base.legacy.digest_text(raw_lines)

    for line_no, line in enumerate(raw_lines.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        top = str(obj.get("type") or "")
        ptype = str(payload.get("type") or "")
        ts = _base.legacy.parse_ts(obj.get("timestamp"))

        if top == "session_meta":
            session_id = str(payload.get("session_id") or payload.get("id") or session_id)
            if isinstance(payload.get("cwd"), str):
                session_cwd = payload["cwd"]
            chat_id = _base.legacy.get_chat_id(con, session_id)
        if chat_id is None:
            chat_id = _base.legacy.get_chat_id(con, session_id)

        if top == "turn_context":
            collab = payload.get("collaboration_mode") if isinstance(payload.get("collaboration_mode"), dict) else {}
            settings = collab.get("settings") if isinstance(collab.get("settings"), dict) else {}
            current = {
                "turn_id": str(payload.get("turn_id") or f"line-{line_no}"),
                "started_at_utc": _base.legacy.utc_stamp(ts) if ts else None,
                "model": payload.get("model") or settings.get("model"),
                "reasoning_effort": settings.get("reasoning_effort") or payload.get("effort"),
                "cwd": payload.get("cwd") or session_cwd,
                "repo_paths": [],
                "repo_write_paths": [],
                "prompt_text": "",
                "prompt_id": None,
                "prompt_id_source": None,
                "events": [],
                "tool_calls": 0,
                "tool_calls_by_type": {},
                "token": {},
                "quota_first": None,
                "quota_last": None,
                "goal_objective_call_ids": set(),
                "goal_complete": None,
            }

        event = {
            "timestamp_utc": _base.legacy.utc_stamp(ts) if ts else None,
            "top_type": top,
            "subtype": ptype or None,
            "role": payload.get("role") if isinstance(payload.get("role"), str) else None,
            "tool_name": payload.get("name") if ptype in {"function_call", "custom_tool_call"} else None,
            "content_text": _base.legacy.archive.redact_text(_base.legacy.extract_output_text(payload)),
        }
        if current is not None:
            current["events"].append(event)

            if ptype in {"function_call", "custom_tool_call"}:
                current["tool_calls"] += 1
                name = str(payload.get("name") or ptype)
                current["tool_calls_by_type"][name] = current["tool_calls_by_type"].get(name, 0) + 1
                payload_paths = (
                    _base.legacy.apply_patch_paths_from_payload(payload, current.get("cwd"))
                    if name == "apply_patch"
                    else _base.legacy.explicit_paths_from_payload(payload)
                )
                current["repo_paths"].extend(payload_paths)
                if name == "apply_patch":
                    current["repo_write_paths"].extend(payload_paths)

                if name in {"exec_command", "shell"}:
                    args = _base._json_obj(payload.get("arguments")) or _base._json_obj(payload.get("input"))
                    command = _base._command_text(args.get("cmd"))
                    call_id = payload.get("call_id")
                    if call_id and "goal-objective.md" in command:
                        current["goal_objective_call_ids"].add(str(call_id))

            if ptype == "message" and payload.get("role") == "user" and not current["prompt_text"]:
                current["prompt_text"] = _base.legacy.archive.redact_text(_base.legacy.extract_output_text(payload))
                current["prompt_id"] = _base.prompt_id_from_text(current["prompt_text"])
                if current["prompt_id"]:
                    current["prompt_id_source"] = "user_prompt_or_attachment"

            if ptype in {"function_call_output", "custom_tool_call_output"}:
                call_id = str(payload.get("call_id") or "")
                if not current["prompt_id"] and call_id in current["goal_objective_call_ids"]:
                    prompt_id = _base._literal_prompt_id(str(payload.get("output") or ""))
                    if prompt_id:
                        current["prompt_id"] = prompt_id
                        current["prompt_id_source"] = "goal_objective_output"
                goal = _goal_completion(payload)
                if goal is not None:
                    current["goal_complete"] = goal

            if top == "event_msg" and ptype == "token_count":
                current["token"] = _base.legacy.token_usage(payload)
                quota = _base.legacy.quota_from_event(payload)
                if quota:
                    if current["quota_first"] is None:
                        current["quota_first"] = quota
                    current["quota_last"] = quota

        if top == "event_msg" and ptype == "turn_aborted" and current is not None and current.get("goal_complete"):
            goal = current["goal_complete"]
            turn_id = str(payload.get("turn_id") or current["turn_id"])
            goal_started = _base.legacy.parse_ts(goal.get("createdAt"))
            goal_completed = _base.legacy.parse_ts(goal.get("updatedAt"))
            q_first = current.get("quota_first") or {}
            q_last = current.get("quota_last") or {}
            try:
                goal_tokens = int(goal.get("tokensUsed")) if goal.get("tokensUsed") is not None else None
            except (TypeError, ValueError):
                goal_tokens = None
            try:
                goal_seconds = float(goal.get("timeUsedSeconds")) if goal.get("timeUsedSeconds") is not None else None
            except (TypeError, ValueError):
                goal_seconds = None

            metrics = {
                "schema": "codex-usage.prompt-metrics.v1",
                "cycle_key": _base.legacy.digest_text(f"{session_id}:{turn_id}:goal-complete-turn-aborted")[:24],
                "prompt_id": current.get("prompt_id"),
                "prompt_id_source": current.get("prompt_id_source"),
                "chat_id": chat_id,
                "native_session_id": session_id,
                "turn_id": turn_id,
                "source_path": str(path),
                "timestamp_start_utc": _base.legacy.utc_stamp(goal_started) if goal_started else current.get("started_at_utc"),
                "timestamp_end_utc": _base.legacy.utc_stamp(goal_completed) if goal_completed else (_base.legacy.utc_stamp(ts) if ts else None),
                "duration_seconds": goal_seconds,
                "model": current.get("model"),
                "reasoning_effort": current.get("reasoning_effort"),
                "turn_count": 1,
                "tool_call_count": current.get("tool_calls"),
                "tool_calls_by_type": current.get("tool_calls_by_type"),
                "input_tokens": None,
                "cached_input_tokens": None,
                "uncached_input_tokens": None,
                "output_tokens": None,
                "reasoning_output_tokens": None,
                "total_tokens": goal_tokens,
                "cache_ratio": None,
                "quota_weekly_before": q_first.get("weekly_remaining_percent"),
                "quota_weekly_after": q_last.get("weekly_remaining_percent"),
                "quota_delta_observed": (
                    q_last.get("weekly_remaining_percent") - q_first.get("weekly_remaining_percent")
                    if q_last.get("weekly_remaining_percent") is not None and q_first.get("weekly_remaining_percent") is not None
                    else None
                ),
                "quota_reset_at_utc": q_last.get("weekly_reset_at_utc"),
                "prompt_text_redacted": current.get("prompt_text") or None,
                "final_response_redacted": None,
                "status": "PASS",
                "completion_state": "goal_complete_turn_aborted",
                "goal_reported_tokens": goal_tokens,
                "goal_reported_time_seconds": goal_seconds,
                "repo_project": current.get("cwd"),
                "repo_paths": current.get("repo_paths") or [],
                "repo_write_paths": current.get("repo_write_paths") or [],
            }
            recovered.append(
                {
                    "metrics": metrics,
                    "events": current["events"],
                    "final_event_id": f"{path}:goal-complete-turn-aborted:{turn_id}",
                    "source_sha256": source_sha,
                }
            )
            current = None

    return recovered


def _legacy_parse_session_with_goal_recovery(
    path: Path,
    con: sqlite3.Connection,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cycles, events = _ORIGINAL_LEGACY_PARSE_SESSION(path, con)
    existing_turn_ids = {str(cycle["metrics"].get("turn_id")) for cycle in cycles}
    for cycle in _recover_completed_goal_aborts(path, con):
        if str(cycle["metrics"].get("turn_id")) not in existing_turn_ids:
            cycles.append(cycle)
    cycles.sort(
        key=lambda cycle: (
            str(cycle["metrics"].get("timestamp_start_utc") or ""),
            str(cycle["metrics"].get("timestamp_end_utc") or ""),
            str(cycle["metrics"].get("cycle_key") or ""),
        )
    )
    return cycles, events


def parse_session(path: Path, con: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cycles, events = _BASE_PARSE_SESSION(path, con)
    last_prompt_id: str | None = None
    last_status: str | None = None
    changed = False

    for cycle in cycles:
        metrics = cycle["metrics"]
        before = (
            metrics.get("prompt_id"),
            metrics.get("status"),
            metrics.get("total_tokens"),
            metrics.get("prompt_id_rejected_from_final_response"),
        )
        prompt_text = str(metrics.get("prompt_text_redacted") or "")
        final_text = str(metrics.get("final_response_redacted") or "")
        prompt_id = metrics.get("prompt_id")
        prompt_id_from_user = _base.prompt_id_from_text(prompt_text)
        final_prompt_id = _base.prompt_id_from_text(final_text)
        is_goal_prompt = prompt_text.lstrip().startswith("/goal") or prompt_text.lstrip().startswith(_base._GOAL_PREFIX)
        inherited_blocked_followup = bool(
            last_prompt_id
            and last_status == "BLOCKED"
            and _base._is_blocked_followup(prompt_text)
        )

        # A prompt ID merely mentioned by the assistant does not identify the
        # user's new prompt. This prevents questions such as "what prompt id did
        # you work on?" from overwriting the substantive prompt's published files.
        if (
            prompt_id
            and prompt_text.strip()
            and not prompt_id_from_user
            and not is_goal_prompt
            and not inherited_blocked_followup
            and final_prompt_id == str(prompt_id)
        ):
            metrics["prompt_id"] = None
            metrics["prompt_id_rejected_from_final_response"] = True

        if metrics.get("completion_state") == "goal_complete_turn_aborted":
            metrics["status"] = "PASS"
            if metrics.get("goal_reported_tokens") is not None:
                metrics["total_tokens"] = metrics["goal_reported_tokens"]

        after = (
            metrics.get("prompt_id"),
            metrics.get("status"),
            metrics.get("total_tokens"),
            metrics.get("prompt_id_rejected_from_final_response"),
        )
        if before != after:
            changed = True

        fingerprint = _base._fingerprint(cycle)
        cycle["source_sha256"] = fingerprint
        cycle["cycle_sha256"] = fingerprint
        cycle["fingerprint_schema"] = _base.FINGERPRINT_SCHEMA

        if metrics.get("prompt_id"):
            last_prompt_id = str(metrics["prompt_id"])
        else:
            last_prompt_id = None
        last_status = str(metrics.get("status") or "UNKNOWN")

    if changed:
        _base._should_export = True
    return cycles, events


# Keep every existing public/private test hook and CLI behavior on the mature
# implementation while patching only the two attribution/recovery defects.
_base.VERSION = VERSION
_base._legacy_parse_session = _legacy_parse_session_with_goal_recovery
_base._recover_completed_goal_aborts = _recover_completed_goal_aborts
_base._goal_completion = _goal_completion
_base.parse_session = parse_session
_base.legacy.parse_session = parse_session


if __name__ == "__main__":
    raise SystemExit(_base.main())

# Imports should receive the patched mature module itself so monkeypatch-based
# tests keep targeting the globals used by its functions.
sys.modules[__name__] = _base
