#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter, deque
import json
from pathlib import Path
import re
from typing import Any

from scripts import analyze_prompt_efficiency as base


EXIT_CODE_RE = re.compile(r"\bProcess exited with code\s+(\d+)\b", re.I)
CONTEXT_GUARD_RE = re.compile(
    r"(?is)(?:non\s+(?:rileggere|leggere)|do\s+not\s+(?:re-?read|read)).{0,240}"
    r"(?:MEMORY\.md|roadmap|README|spiegazioni|MegaVault)",
)


def _is_tool_call(event: dict[str, Any]) -> bool:
    return bool(event.get("tool_name")) and event.get("subtype") in {"function_call", "custom_tool_call"}


def _is_tool_output(event: dict[str, Any]) -> bool:
    return event.get("subtype") in {"function_call_output", "custom_tool_call_output"}


def tool_roundtrip_count(events: list[dict[str, Any]]) -> int:
    """Count model→tool batches, not individual tool calls.

    Consecutive tool calls emitted before the first tool output belong to one round-trip.
    """
    count = 0
    batch_open = False
    for event in events:
        if _is_tool_call(event):
            if not batch_open:
                count += 1
                batch_open = True
            continue
        if _is_tool_output(event):
            batch_open = False
    return count


def command_outcomes(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair exec/shell calls with their ordered tool outputs and retain non-zero exits."""
    pending: deque[str] = deque()
    failures: list[dict[str, Any]] = []
    for event in events:
        if _is_tool_call(event) and event.get("tool_name") in {"exec_command", "shell"}:
            args = base._json_obj(event.get("content_text"))
            command = args.get("cmd")
            if isinstance(command, list):
                command = " ".join(str(part) for part in command)
            pending.append(" ".join(command.split()) if isinstance(command, str) else "")
            continue
        if not _is_tool_output(event) or not pending:
            continue
        command = pending.popleft()
        text = event.get("content_text")
        if not isinstance(text, str):
            continue
        match = EXIT_CODE_RE.search(text)
        if match and int(match.group(1)) != 0:
            failures.append({"exit_code": int(match.group(1)), "command": command[:500]})
    return failures


def explicit_context_guard(events: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(event.get("content_text"), str) and CONTEXT_GUARD_RE.search(str(event.get("content_text")))
        for event in events
    )


def enrich(metrics: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    result = base.analyze(metrics, events)
    calls = int(result.get("tool_call_count") or 0)
    duration = float(result.get("duration_seconds") or 0.0)
    fresh = int(result.get("uncached_input_tokens") or 0)
    roundtrips = tool_roundtrip_count(events)
    if roundtrips == 0 and calls:
        # Older/partial transcripts may omit tool output records; retain a conservative proxy.
        roundtrips = calls
    failures = command_outcomes(events)
    calls_per_roundtrip = (calls / roundtrips) if roundtrips else None
    roundtrips_per_minute = (roundtrips * 60.0 / duration) if duration > 0 else None
    guard = explicit_context_guard(events)

    findings = list(result.get("findings") or [])
    if roundtrips >= 20 and fresh < 10_000:
        findings.append(
            {
                "code": "high_roundtrip_count",
                "roundtrips": roundtrips,
                "fresh_input_tokens": fresh,
            }
        )
    if roundtrips >= 20 and calls_per_roundtrip is not None and calls_per_roundtrip <= 1.4 and fresh < 10_000:
        findings.append(
            {
                "code": "sequential_roundtrip_churn",
                "roundtrips": roundtrips,
                "tool_calls_per_roundtrip": round(calls_per_roundtrip, 3),
            }
        )
    if duration >= 150 and roundtrips <= 10:
        findings.append(
            {
                "code": "wait_dominated_session",
                "duration_seconds": duration,
                "roundtrips": roundtrips,
            }
        )
    if failures:
        findings.append({"code": "failed_tool_commands", "count": len(failures)})
    if guard and int(result.get("avoidable_context_read_count") or 0) > 0:
        findings.append(
            {
                "code": "explicit_context_guard_violation",
                "count": int(result.get("avoidable_context_read_count") or 0),
            }
        )

    result.update(
        {
            "tool_roundtrip_count": roundtrips,
            "tool_calls_per_roundtrip": calls_per_roundtrip,
            "roundtrips_per_minute": roundtrips_per_minute,
            "failed_command_count": len(failures),
            "failed_commands": failures,
            "explicit_context_guard": guard,
            "findings": findings,
        }
    )
    return result


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected object: {path}")
    return data


def _prompt_rows(root: Path, recent_unique: int) -> list[dict[str, Any]]:
    index = root / "index/prompts.jsonl"
    rows = base.load_jsonl(index)
    rows.sort(key=lambda row: str(row.get("timestamp_end_utc") or ""), reverse=True)
    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
    for row in rows:
        prompt_id = row.get("prompt_id")
        if prompt_id is None:
            continue
        prompt_id = str(prompt_id)
        if prompt_id in seen:
            continue
        seen.add(prompt_id)
        selected.append(row)
        if len(selected) >= recent_unique:
            break
    return selected


def analyze_recent(root: Path, *, recent_unique: int = 30, top: int = 10) -> dict[str, Any]:
    root = root.expanduser().resolve()
    candidates: list[dict[str, Any]] = []
    missing: list[str] = []
    for index_row in _prompt_rows(root, recent_unique):
        rel = Path(str(index_row.get("path") or ""))
        metrics_path = root / rel / "metrics.json"
        transcript_path = root / rel / "transcript.jsonl"
        if not metrics_path.is_file():
            missing.append(str(index_row.get("prompt_id")))
            continue
        metrics = _load_json(metrics_path)
        events = base.load_jsonl(transcript_path)
        analysis = enrich(metrics, events)
        analysis["timestamp_end_utc"] = index_row.get("timestamp_end_utc")
        candidates.append(analysis)

    max_roundtrips = max((int(row.get("tool_roundtrip_count") or 0) for row in candidates), default=1) or 1
    max_duration = max((float(row.get("duration_seconds") or 0.0) for row in candidates), default=1.0) or 1.0
    for row in candidates:
        roundtrip_score = int(row.get("tool_roundtrip_count") or 0) / max_roundtrips
        duration_score = float(row.get("duration_seconds") or 0.0) / max_duration
        row["selection_score"] = max(roundtrip_score, duration_score)
    candidates.sort(
        key=lambda row: (
            float(row.get("selection_score") or 0.0),
            int(row.get("tool_roundtrip_count") or 0),
            float(row.get("duration_seconds") or 0.0),
        ),
        reverse=True,
    )
    selected = candidates[:top]

    total_input = sum(int(row.get("input_tokens") or 0) for row in selected)
    total_cached = sum(int(row.get("cached_input_tokens") or 0) for row in selected)
    total_fresh = sum(int(row.get("uncached_input_tokens") or 0) for row in selected)
    total_calls = sum(int(row.get("tool_call_count") or 0) for row in selected)
    total_roundtrips = sum(int(row.get("tool_roundtrip_count") or 0) for row in selected)
    total_duration = sum(float(row.get("duration_seconds") or 0.0) for row in selected)
    finding_counts = Counter(
        str(item.get("code"))
        for row in selected
        for item in row.get("findings") or []
        if isinstance(item, dict) and item.get("code")
    )

    return {
        "published_root": str(root),
        "recent_unique_considered": len(candidates),
        "top_n": len(selected),
        "missing_metrics": missing,
        "window": {
            "newest": max((str(row.get("timestamp_end_utc") or "") for row in candidates), default=None),
            "oldest": min((str(row.get("timestamp_end_utc") or "") for row in candidates), default=None),
        },
        "totals": {
            "input_tokens": total_input,
            "cached_input_tokens": total_cached,
            "fresh_input_tokens": total_fresh,
            "cache_ratio": (total_cached / total_input) if total_input else None,
            "tool_call_count": total_calls,
            "tool_roundtrip_count": total_roundtrips,
            "duration_seconds": total_duration,
        },
        "common_findings": dict(finding_counts.most_common()),
        "prompts": selected,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rank recent published Codex prompts by real tool round-trips or wall-clock duration."
    )
    parser.add_argument("published_root", type=Path, help="Local checkout of the private codex-usage repository")
    parser.add_argument("--recent-unique", type=int, default=30)
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args(argv)
    if args.recent_unique < 1 or args.top < 1:
        parser.error("--recent-unique and --top must be >= 1")
    result = analyze_recent(args.published_root, recent_unique=args.recent_unique, top=args.top)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
