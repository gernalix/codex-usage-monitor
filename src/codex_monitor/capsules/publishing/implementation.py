#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
import sqlite3
from typing import Any

from . import base as _base
from .base import (
    ATTACHMENTS_ROOT,
    DEFAULT_DATA_REMOTE,
    DEFAULT_DATA_REPO,
    DEFAULT_QUOTA_DB,
    DEFAULT_SOURCE_ROOT,
    DEFAULT_STATE_DIR,
    FINGERPRINT_SCHEMA,
    GUARD_METADATA_KEYS,
    PublisherError,
    _apply_patch_write_paths_from_event,
    _fingerprint,
    _full_fingerprint,
    _repo_roots,
    _run_git,
    assert_private_repo,
    build_parser,
    chat_metrics,
    classify_git_repo,
    command_run,
    command_status,
    connect_state,
    dedupe_cycle_keys,
    digest_text,
    ensure_repo,
    git_ok,
    json_obj,
    prompt_dir,
    prompt_id_from_text,
    quota_index,
    record_git_completion_guard,
    run,
    send_batch_telegram,
    source_sha,
    status_from_final,
    utc_stamp,
    write_json,
    write_jsonl,
)


VERSION = "2026.09.24.1"
_ORIGINAL_LEGACY_PARSE_SESSION = _base._legacy_parse_session
_BASE_PARSE_SESSION = _base.parse_session
_BASE_EXPORT_REPO = _base.export_repo


def _goal_completion(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    goal = parsed.get("goal") if isinstance(parsed, dict) else None
    if not isinstance(goal, dict) or str(goal.get("status") or "").lower() != "complete":
        return None
    return goal


_ROADMAP_START_PROMPT_RE = re.compile(r'"prompt_id"\s*:\s*"(\d{6})"')
_ROADMAP_START_RUNNING_RE = re.compile(r'"roadmap_status"\s*:\s*"running"')
_ROADMAP_START_BRANCH_RE = re.compile(r'"task_branch"\s*:\s*"task/(\d{6})"')


def _roadmap_started_prompt_id(events: list[dict[str, Any]]) -> str | None:
    """Return the prompt explicitly claimed by roadmap_start in this cycle.

    Goal continuations may inherit the previous prompt id before they read or
    register a new goal. A successful roadmap_start output is stronger evidence
    than that inherited identity.
    """
    for event in events:
        if str(event.get("subtype") or "") not in {
            "function_call_output",
            "custom_tool_call_output",
        }:
            continue
        text = str(event.get("content_text") or "")
        if not _ROADMAP_START_RUNNING_RE.search(text):
            continue
        prompt = _ROADMAP_START_PROMPT_RE.search(text)
        branch = _ROADMAP_START_BRANCH_RE.search(text)
        if not prompt:
            continue
        prompt_id = prompt.group(1)
        if branch and branch.group(1) != prompt_id:
            continue
        return prompt_id
    return None


def _recover_completed_goal_aborts(
    path: Path,
    con: sqlite3.Connection,
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Recover a /goal completed before the same turn was later interrupted.

    Recovery intentionally consumes the normalized event stream already produced
    by the legacy parser. This keeps the publisher's one-read-per-rollout
    invariant while avoiding any dependency on an attachment that may already
    have disappeared by publication time.
    """
    session_id = _base.legacy.archive.first_uuid_from_path(path) or path.stem
    chat_id = _base.legacy.get_chat_id(con, session_id)
    current: dict[str, Any] | None = None
    recovered: list[dict[str, Any]] = []

    for event in events:
        top = str(event.get("top_type") or "")
        subtype = str(event.get("subtype") or "")
        timestamp = event.get("timestamp_utc")

        if top == "turn_context":
            current = {
                "started_at_utc": timestamp,
                "prompt_text": "",
                "prompt_id": None,
                "prompt_id_source": None,
                "events": [],
                "tool_calls": 0,
                "tool_calls_by_type": {},
                "repo_paths": [],
                "waiting_for_goal_objective_output": False,
                "goal_complete": None,
            }

        if current is None:
            continue
        current["events"].append(event)

        if subtype == "message" and event.get("role") == "user" and not current["prompt_text"]:
            current["prompt_text"] = str(event.get("content_text") or "")
            current["prompt_id"] = _base.prompt_id_from_text(current["prompt_text"])
            if current["prompt_id"]:
                current["prompt_id_source"] = "user_prompt_or_attachment"

        if subtype in {"function_call", "custom_tool_call"}:
            current["tool_calls"] += 1
            name = str(event.get("tool_name") or subtype)
            current["tool_calls_by_type"][name] = current["tool_calls_by_type"].get(name, 0) + 1
            args = _base._json_obj(event.get("content_text"))
            for key in ("workdir", "cwd", "path", "file"):
                value = args.get(key)
                if isinstance(value, str) and value.startswith("/"):
                    current["repo_paths"].append(value)
            if name in {"exec_command", "shell"}:
                command = _base._command_text(args.get("cmd"))
                if "goal-objective.md" in command:
                    current["waiting_for_goal_objective_output"] = True

        if subtype in {"function_call_output", "custom_tool_call_output"}:
            text = str(event.get("content_text") or "")
            if current["waiting_for_goal_objective_output"]:
                prompt_id = _base._literal_prompt_id(text)
                if prompt_id:
                    current["prompt_id"] = prompt_id
                    current["prompt_id_source"] = "goal_objective_output"
                current["waiting_for_goal_objective_output"] = False
            goal = _goal_completion(text)
            if goal is not None:
                current["goal_complete"] = goal

        if top == "event_msg" and subtype == "turn_aborted" and current.get("goal_complete"):
            goal = current["goal_complete"]
            goal_started = _base.legacy.parse_ts(goal.get("createdAt"))
            goal_completed = _base.legacy.parse_ts(goal.get("updatedAt"))
            try:
                goal_tokens = int(goal.get("tokensUsed")) if goal.get("tokensUsed") is not None else None
            except (TypeError, ValueError):
                goal_tokens = None
            try:
                goal_seconds = float(goal.get("timeUsedSeconds")) if goal.get("timeUsedSeconds") is not None else None
            except (TypeError, ValueError):
                goal_seconds = None

            stable_seed = f"{session_id}:{current.get('started_at_utc')}:{current.get('prompt_id')}:goal-complete-turn-aborted"
            synthetic_turn_id = f"goal-abort-{_base.legacy.digest_text(stable_seed)[:16]}"
            metrics = {
                "schema": "codex-usage.prompt-metrics.v1",
                "cycle_key": _base.legacy.digest_text(stable_seed)[:24],
                "prompt_id": current.get("prompt_id"),
                "prompt_id_source": current.get("prompt_id_source"),
                "chat_id": chat_id,
                "native_session_id": session_id,
                "turn_id": synthetic_turn_id,
                "source_path": str(path),
                "timestamp_start_utc": _base.legacy.utc_stamp(goal_started) if goal_started else current.get("started_at_utc"),
                "timestamp_end_utc": _base.legacy.utc_stamp(goal_completed) if goal_completed else timestamp,
                "duration_seconds": goal_seconds,
                "model": None,
                "reasoning_effort": None,
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
                "quota_weekly_before": None,
                "quota_weekly_after": None,
                "quota_delta_observed": None,
                "quota_reset_at_utc": None,
                "prompt_text_redacted": current.get("prompt_text") or None,
                "final_response_redacted": None,
                "status": "PASS",
                "completion_state": "goal_complete_turn_aborted",
                "goal_reported_tokens": goal_tokens,
                "goal_reported_time_seconds": goal_seconds,
                "repo_project": None,
                "repo_paths": _base._stable_unique(current.get("repo_paths") or []),
                "repo_write_paths": [],
            }
            recovered.append(
                {
                    "metrics": metrics,
                    "events": list(current["events"]),
                    "final_event_id": f"{path}:goal-complete-turn-aborted:{synthetic_turn_id}",
                    "source_sha256": _base.legacy.digest_text(
                        json.dumps(current["events"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    ),
                }
            )
            current = None

    return recovered


def _legacy_parse_session_with_goal_recovery(
    path: Path,
    con: sqlite3.Connection,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cycles, events = _ORIGINAL_LEGACY_PARSE_SESSION(path, con)
    for cycle in _recover_completed_goal_aborts(path, con, events):
        cycle_key = str(cycle["metrics"].get("cycle_key") or "")
        if not any(str(existing["metrics"].get("cycle_key") or "") == cycle_key for existing in cycles):
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
    cycles, events = _BASE_PARSE_SESSION(
        path,
        con,
        legacy_parse_session_fn=_legacy_parse_session_with_goal_recovery,
    )
    last_prompt_id: str | None = None
    last_status: str | None = None
    active_goal_prompt_id: str | None = None
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
            last_prompt_id and last_status == "BLOCKED" and _base._is_blocked_followup(prompt_text)
        )

        if is_goal_prompt:
            started_prompt_id = _roadmap_started_prompt_id(cycle.get("events") or [])
            if started_prompt_id:
                metrics["prompt_id"] = started_prompt_id
                metrics["prompt_id_source"] = "roadmap_start_output"
                prompt_id = started_prompt_id
                active_goal_prompt_id = started_prompt_id
            elif active_goal_prompt_id:
                metrics["prompt_id"] = active_goal_prompt_id
                metrics["prompt_id_source"] = "active_goal_continuation"
                prompt_id = active_goal_prompt_id
            elif "Tokens used: 0" in prompt_text and not prompt_id_from_user:
                # A fresh goal has started but has not yet produced an
                # authoritative prompt claim. Do not inherit the prior goal.
                metrics["prompt_id"] = None
                prompt_id = None
        else:
            active_goal_prompt_id = None

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
        _base.mark_export_required()
    return cycles, events


def _stable_prompt_dir(metrics: dict[str, Any]) -> Path:
    prompt_id = metrics.get("prompt_id")
    cycle_key = str(metrics["cycle_key"])
    if prompt_id:
        return Path("prompts") / str(prompt_id) / "cycles" / cycle_key
    return Path("prompts/unassigned") / cycle_key


def _stable_layout_missing(repo: Path, cycles: list[dict[str, Any]]) -> bool:
    return any(
        cycle["metrics"].get("prompt_id")
        and not (repo / _stable_prompt_dir(cycle["metrics"]) / "metrics.json").is_file()
        for cycle in cycles
    )


def _remove_reassigned_cycle_dirs(repo: Path, cycles: list[dict[str, Any]]) -> int:
    index_path = repo / "index/prompts.jsonl"
    if not index_path.is_file():
        return 0
    previous: dict[str, str] = {}
    for line in index_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("cycle_key") and row.get("path"):
            previous[str(row["cycle_key"])] = str(row["path"])

    removed = 0
    for cycle in cycles:
        metrics = cycle["metrics"]
        key = str(metrics["cycle_key"])
        old_rel = previous.get(key)
        new_rel = _stable_prompt_dir(metrics).as_posix()
        if not old_rel or old_rel == new_rel:
            continue
        old_path = repo / old_rel
        if (
            old_path.is_dir()
            and old_rel.startswith("prompts/")
            and "/cycles/" in old_rel
        ):
            shutil.rmtree(old_path)
            removed += 1
    return removed


def export_repo(
    repo: Path,
    cycles: list[dict[str, Any]],
    chat_events_by_id: dict[int, list[dict[str, Any]]],
    quota_db: Path,
) -> None:
    """Publish every assigned prompt cycle without overwriting earlier runs.

    The historical flat `prompts/<PROMPT_ID>/metrics.json` and transcript remain
    as a compatibility alias for the latest cycle, while the prompt index points
    at immutable `cycles/<cycle_key>` paths. A missing stable layout forces a
    one-time backfill even when no source cycle changed in this publisher run.
    """
    reassigned_removed = _remove_reassigned_cycle_dirs(repo, cycles)
    migration_needed = _stable_layout_missing(repo, cycles) or bool(reassigned_removed)
    _BASE_EXPORT_REPO(repo, cycles, chat_events_by_id, quota_db)
    if not _base.export_required() and not migration_needed:
        return

    prompt_rows: list[dict[str, Any]] = []
    latest_by_prompt: dict[str, dict[str, Any]] = {}
    for cycle in cycles:
        metrics = cycle["metrics"]
        rel = _stable_prompt_dir(metrics)
        prompt_id = metrics.get("prompt_id")
        if prompt_id:
            _base.write_json(repo / rel / "metrics.json", metrics)
            _base.write_jsonl(repo / rel / "transcript.jsonl", cycle["events"])
            prompt_key = str(prompt_id)
            previous = latest_by_prompt.get(prompt_key)
            ordering = (
                str(metrics.get("timestamp_end_utc") or ""),
                str(metrics.get("cycle_key") or ""),
            )
            if previous is None or ordering > previous["ordering"]:
                latest_by_prompt[prompt_key] = {"cycle": cycle, "ordering": ordering}

        prompt_rows.append(
            {
                "prompt_id": prompt_id,
                "chat_id": metrics["chat_id"],
                "cycle_key": metrics["cycle_key"],
                "native_session_id": metrics["native_session_id"],
                "timestamp_end_utc": metrics.get("timestamp_end_utc"),
                "status": metrics.get("status"),
                "path": rel.as_posix(),
            }
        )

    # Preserve the legacy flat path as an explicit latest-cycle alias so callers
    # that have not migrated to the cycle-aware index continue to work.
    for prompt_id, selected in latest_by_prompt.items():
        cycle = selected["cycle"]
        flat = Path("prompts") / prompt_id
        _base.write_json(repo / flat / "metrics.json", cycle["metrics"])
        _base.write_jsonl(repo / flat / "transcript.jsonl", cycle["events"])

    sorted_rows = sorted(
        prompt_rows,
        key=lambda row: (row.get("timestamp_end_utc") or "", str(row.get("cycle_key"))),
    )
    _base.write_jsonl(repo / "index/prompts.jsonl", sorted_rows)

    latest_path = repo / "index/latest.json"
    if latest_path.is_file():
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        if isinstance(latest, dict):
            latest["latest_prompt"] = sorted_rows[-1] if sorted_rows else None
            _base.write_json(latest_path, latest)


def command_run(args) -> int:
    return _base.command_run(args, parse_session_fn=parse_session, export_repo_fn=export_repo)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return command_run(args)
        return int(command_status(args))
    except PublisherError as exc:
        return _base.legacy.publisher_error_result(exc)
    except BlockingIOError:
        print(json.dumps({"status": "locked"}, sort_keys=True))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
