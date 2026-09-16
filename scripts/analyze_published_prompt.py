#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess
from typing import Any

from scripts import analyze_prompt_efficiency as base
from scripts import analyze_recent_prompts as recent


_EXPLICIT_RESULTS = {"PASS", "BLOCKED", "FAIL"}
_AGGREGATE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
    "tool_call_count",
)
_TOOL_CALL_SUBTYPES = {"function_call", "custom_tool_call"}


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return data


def _goal_usage(events: list[dict[str, Any]]) -> tuple[int | None, float | None]:
    """Return structured goal usage without double-counting continuation snapshots."""
    counters: dict[str, dict[str, float]] = {}
    for event in events:
        text = event.get("content_text")
        if not isinstance(text, str) or not text.lstrip().startswith("{"):
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        goal = payload.get("goal") if isinstance(payload, dict) else None
        if not isinstance(goal, dict):
            continue
        key = str(goal.get("threadId") or "__unknown_goal__")
        counter = counters.setdefault(key, {"tokens": 0.0, "seconds": 0.0})
        try:
            if goal.get("tokensUsed") is not None:
                counter["tokens"] = max(counter["tokens"], float(goal["tokensUsed"]))
            if goal.get("timeUsedSeconds") is not None:
                counter["seconds"] = max(counter["seconds"], float(goal["timeUsedSeconds"]))
        except (TypeError, ValueError):
            continue
    if not counters:
        return None, None
    token_total = int(sum(counter["tokens"] for counter in counters.values()))
    seconds_total = sum(counter["seconds"] for counter in counters.values())
    return token_total, seconds_total


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )


def _git_text(root: Path, commit: str, rel: str) -> str | None:
    result = _run_git(root, "show", f"{commit}:{rel}")
    return result.stdout if result.returncode == 0 else None


def _parse_jsonl_text(text: str | None) -> list[dict[str, Any]]:
    if not text:
        return []
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _stored_prompt_cycles(root: Path, prompt_id: str) -> list[dict[str, Any]]:
    """Load collision-safe prompt cycles from the current published tree."""
    cycles_dir = root / f"prompts/{prompt_id}/cycles"
    if not cycles_dir.is_dir():
        return []
    cycles: list[dict[str, Any]] = []
    for cycle_dir in sorted(path for path in cycles_dir.iterdir() if path.is_dir()):
        metrics_path = cycle_dir / "metrics.json"
        if not metrics_path.is_file():
            continue
        try:
            metrics = _load_json(metrics_path)
        except (RuntimeError, json.JSONDecodeError):
            continue
        if str(metrics.get("prompt_id") or "") != str(prompt_id):
            continue
        cycles.append(
            {
                "metrics": metrics,
                "events": base.load_jsonl(cycle_dir / "transcript.jsonl"),
                "commit": None,
                "source": "published_cycle",
            }
        )
    return cycles


def _historical_prompt_cycles(root: Path, prompt_id: str) -> list[dict[str, Any]]:
    """Recover unique prompt cycles from Git history, newest revision of each cycle first."""
    if not (root / ".git").exists():
        return []
    metrics_rel = f"prompts/{prompt_id}/metrics.json"
    transcript_rel = f"prompts/{prompt_id}/transcript.jsonl"
    history = _run_git(root, "log", "--format=%H", "--", metrics_rel)
    if history.returncode != 0:
        return []

    cycles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for commit in history.stdout.splitlines():
        metrics_text = _git_text(root, commit, metrics_rel)
        if not metrics_text:
            continue
        try:
            metrics = json.loads(metrics_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(metrics, dict) or str(metrics.get("prompt_id") or "") != str(prompt_id):
            continue
        cycle_key = str(metrics.get("cycle_key") or f"commit:{commit}")
        if cycle_key in seen:
            continue
        seen.add(cycle_key)
        cycles.append(
            {
                "metrics": metrics,
                "events": _parse_jsonl_text(_git_text(root, commit, transcript_rel)),
                "commit": commit,
                "source": "git_history",
            }
        )
    return cycles


def _merge_cycles(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for cycle in group:
            metrics = cycle["metrics"]
            key = str(metrics.get("cycle_key") or f"fallback:{metrics.get('timestamp_end_utc')}:{metrics.get('tool_call_count')}")
            if key in seen:
                continue
            seen.add(key)
            merged.append(cycle)
    return merged


def _is_substantive(metrics: dict[str, Any]) -> bool:
    status = str(metrics.get("status") or "").upper()
    return (
        status in _EXPLICIT_RESULTS
        or int(metrics.get("tool_call_count") or 0) > 0
        or float(metrics.get("duration_seconds") or 0.0) >= 10.0
        or int(metrics.get("output_tokens") or 0) >= 50
    )


def _select_cycle(cycles: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Select the primary work cycle rather than a later bookkeeping continuation."""
    if not cycles:
        return None
    substantive = [cycle for cycle in cycles if _is_substantive(cycle["metrics"])]
    pool = substantive or cycles
    return max(
        pool,
        key=lambda cycle: (
            int(cycle["metrics"].get("tool_call_count") or 0),
            float(cycle["metrics"].get("duration_seconds") or 0.0),
            int(cycle["metrics"].get("output_tokens") or 0),
            str(cycle["metrics"].get("timestamp_end_utc") or ""),
        ),
    )


def _terminal_cycle(cycles: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not cycles:
        return None
    return max(cycles, key=lambda cycle: str(cycle["metrics"].get("timestamp_end_utc") or ""))


def _aggregate_substantive(cycles: list[dict[str, Any]]) -> dict[str, Any]:
    selected = [cycle for cycle in cycles if _is_substantive(cycle["metrics"])]
    totals = {
        key: sum(int(cycle["metrics"].get(key) or 0) for cycle in selected)
        for key in _AGGREGATE_KEYS
    }
    return {
        "substantive_cycle_count": len(selected),
        "duration_seconds": sum(float(cycle["metrics"].get("duration_seconds") or 0.0) for cycle in selected),
        **totals,
    }


def _parse_timestamp(value: Any) -> dt.datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError:
        return None


def _native_tool_events(root: Path, metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """Recover redacted tool inputs from the already-published complete native chat dump."""
    session_id = str(metrics.get("native_session_id") or "")
    if not session_id:
        return []
    sources = root / "native-sessions" / session_id / "sources"
    if not sources.is_dir():
        return []
    start = _parse_timestamp(metrics.get("timestamp_start_utc"))
    end = _parse_timestamp(metrics.get("timestamp_end_utc"))
    events: list[dict[str, Any]] = []
    for chunk in sorted(sources.glob("*/chunks/*.jsonl")):
        for row in base.load_jsonl(chunk):
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            ptype = str(payload.get("type") or "")
            if ptype not in _TOOL_CALL_SUBTYPES:
                continue
            timestamp = _parse_timestamp(row.get("timestamp"))
            if start is not None and timestamp is not None and timestamp < start:
                continue
            if end is not None and timestamp is not None and timestamp > end:
                continue
            raw_input = payload.get("arguments")
            if raw_input is None:
                raw_input = payload.get("input")
            if isinstance(raw_input, (dict, list)):
                content = json.dumps(raw_input, ensure_ascii=False, sort_keys=True)
            elif isinstance(raw_input, str):
                content = raw_input
            else:
                content = ""
            events.append(
                {
                    "timestamp_utc": row.get("timestamp"),
                    "top_type": row.get("type"),
                    "subtype": ptype,
                    "role": payload.get("role") if isinstance(payload.get("role"), str) else None,
                    "tool_name": payload.get("name") if isinstance(payload.get("name"), str) else None,
                    "content_text": content,
                }
            )
    return events


def _with_native_tool_inputs(
    root: Path,
    metrics: dict[str, Any],
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    native = _native_tool_events(root, metrics)
    if not native:
        return events, 0
    without_empty_calls = [event for event in events if event.get("subtype") not in _TOOL_CALL_SUBTYPES]
    return [*without_empty_calls, *native], len(native)


def analyze_published_prompt(root: Path, prompt_id: str) -> dict[str, Any]:
    """Analyze one PROMPT_ID without letting a short continuation hide the real work."""
    root = root.expanduser().resolve()
    index = root / "index/prompts.jsonl"
    rows = [
        row
        for row in base.load_jsonl(index)
        if str(row.get("prompt_id") or "") == str(prompt_id)
    ]
    if not rows:
        raise RuntimeError(f"PROMPT_ID={prompt_id} not found in {index}")
    rows.sort(key=lambda row: str(row.get("timestamp_end_utc") or ""))
    latest = rows[-1]
    rel = Path(str(latest.get("path") or ""))
    metrics_path = root / rel / "metrics.json"
    transcript_path = root / rel / "transcript.jsonl"
    if not metrics_path.is_file():
        raise RuntimeError(f"metrics missing for PROMPT_ID={prompt_id}: {metrics_path}")

    current_metrics = _load_json(metrics_path)
    current_events = base.load_jsonl(transcript_path)
    stored_cycles = _stored_prompt_cycles(root, prompt_id)
    git_cycles = _historical_prompt_cycles(root, prompt_id)
    cycles = _merge_cycles(stored_cycles, git_cycles)
    selected = _select_cycle(cycles)
    terminal = _terminal_cycle(cycles)
    selected_source = selected.get("source") if selected else "flat_current"
    selected_from_history = bool(
        selected
        and selected_source == "git_history"
        and str(selected["metrics"].get("cycle_key") or "")
        != str(current_metrics.get("cycle_key") or "")
    )
    metrics = selected["metrics"] if selected else current_metrics
    events = selected["events"] if selected else current_events
    events, native_tool_input_events = _with_native_tool_inputs(root, metrics, events)

    result = recent.enrich(metrics, events)
    all_goal_events = [event for cycle in cycles for event in cycle["events"]] if cycles else current_events
    goal_tokens, goal_seconds = _goal_usage(all_goal_events)
    metrics_tokens = int(metrics.get("total_tokens") or 0)
    aggregate = _aggregate_substantive(cycles) if cycles else {
        "substantive_cycle_count": 1 if _is_substantive(current_metrics) else 0,
        "duration_seconds": float(current_metrics.get("duration_seconds") or 0.0),
        **{key: int(current_metrics.get(key) or 0) for key in _AGGREGATE_KEYS},
    }
    terminal_metrics = terminal["metrics"] if terminal else current_metrics
    result.update(
        {
            "published_path": rel.as_posix(),
            "timestamp_end_utc": metrics.get("timestamp_end_utc") or latest.get("timestamp_end_utc"),
            "published_cycle_count": len(rows),
            "stored_cycle_count": len(stored_cycles),
            "historical_unique_cycle_count": len(cycles),
            "selected_from_git_history": selected_from_history,
            "selected_source": selected_source,
            "selected_history_commit": selected.get("commit") if selected else None,
            "current_cycle_key": current_metrics.get("cycle_key"),
            "selected_cycle_key": metrics.get("cycle_key"),
            "terminal_cycle_key": terminal_metrics.get("cycle_key"),
            "transcript_available": bool(events),
            "native_tool_input_event_count": native_tool_input_events,
            "native_session_dump_used": native_tool_input_events > 0,
            "goal_reported_tokens": goal_tokens,
            "goal_reported_time_seconds": goal_seconds,
            "goal_vs_metrics_token_delta": (goal_tokens - metrics_tokens) if goal_tokens is not None else None,
            "aggregate_substantive": aggregate,
        }
    )
    if goal_tokens is not None and goal_tokens != metrics_tokens:
        result.setdefault("findings", []).append(
            {
                "code": "goal_metrics_token_delta",
                "goal_reported_tokens": goal_tokens,
                "metrics_total_tokens": metrics_tokens,
                "delta": goal_tokens - metrics_tokens,
            }
        )
    aggregate_tokens = int(aggregate.get("total_tokens") or 0)
    if goal_tokens is not None and aggregate_tokens > goal_tokens:
        result.setdefault("findings", []).append(
            {
                "code": "cycle_token_sum_overcounts_goal_context",
                "cycle_total_tokens_sum": aggregate_tokens,
                "goal_reported_tokens": goal_tokens,
                "overcount": aggregate_tokens - goal_tokens,
                "note": "cycle token snapshots overlap; prefer structured goal usage for the goal total",
            }
        )
    if len(rows) > 1 or len(cycles) > 1:
        result.setdefault("findings", []).append(
            {
                "code": "published_prompt_has_multiple_cycles",
                "count": max(len(rows), len(cycles)),
                "note": "multiple cycles share this PROMPT_ID; primary work cycle is selected by work performed",
            }
        )
    if selected and str(metrics.get("cycle_key") or "") != str(current_metrics.get("cycle_key") or ""):
        result.setdefault("findings", []).append(
            {
                "code": "latest_prompt_files_overwrote_substantive_cycle",
                "current_cycle_key": current_metrics.get("cycle_key"),
                "selected_cycle_key": metrics.get("cycle_key"),
                "note": "analysis selected the primary work cycle instead of the latest flat alias",
            }
        )
    if selected and terminal and str(metrics.get("cycle_key") or "") != str(terminal_metrics.get("cycle_key") or ""):
        result.setdefault("findings", []).append(
            {
                "code": "terminal_followup_not_primary_work_cycle",
                "primary_cycle_key": metrics.get("cycle_key"),
                "terminal_cycle_key": terminal_metrics.get("cycle_key"),
            }
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyze one published Codex PROMPT_ID with metrics, transcript and cycle recovery."
    )
    parser.add_argument("published_root", type=Path, help="Local checkout of the private codex-usage repository")
    parser.add_argument("prompt_id")
    args = parser.parse_args(argv)
    try:
        result = analyze_published_prompt(args.published_root, args.prompt_id)
    except RuntimeError as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())