#!/usr/bin/env python3
from __future__ import annotations

import argparse
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


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return data


def _goal_usage(events: list[dict[str, Any]]) -> tuple[int | None, float | None]:
    """Return the highest structured Codex goal token/time counters present in a transcript."""
    token_values: list[int] = []
    time_values: list[float] = []
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
        try:
            if goal.get("tokensUsed") is not None:
                token_values.append(int(goal["tokensUsed"]))
            if goal.get("timeUsedSeconds") is not None:
                time_values.append(float(goal["timeUsedSeconds"]))
        except (TypeError, ValueError):
            continue
    return (max(token_values) if token_values else None, max(time_values) if time_values else None)


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
            }
        )
    return cycles


def _is_substantive(metrics: dict[str, Any]) -> bool:
    status = str(metrics.get("status") or "").upper()
    return (
        status in _EXPLICIT_RESULTS
        or int(metrics.get("tool_call_count") or 0) > 0
        or float(metrics.get("duration_seconds") or 0.0) >= 10.0
        or int(metrics.get("output_tokens") or 0) >= 50
    )


def _select_cycle(cycles: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not cycles:
        return None
    substantive = [cycle for cycle in cycles if _is_substantive(cycle["metrics"])]
    pool = substantive or cycles
    return max(
        pool,
        key=lambda cycle: (
            str(cycle["metrics"].get("timestamp_end_utc") or ""),
            int(cycle["metrics"].get("tool_call_count") or 0),
        ),
    )


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


def analyze_published_prompt(root: Path, prompt_id: str) -> dict[str, Any]:
    """Analyze one PROMPT_ID without letting a later trivial follow-up hide the real run."""
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
    historical = _historical_prompt_cycles(root, prompt_id)
    selected = _select_cycle(historical)
    selected_from_history = bool(
        selected
        and str(selected["metrics"].get("cycle_key") or "")
        != str(current_metrics.get("cycle_key") or "")
    )
    metrics = selected["metrics"] if selected else current_metrics
    events = selected["events"] if selected else current_events

    result = recent.enrich(metrics, events)
    goal_tokens, goal_seconds = _goal_usage(events)
    metrics_tokens = int(metrics.get("total_tokens") or 0)
    aggregate = _aggregate_substantive(historical) if historical else {
        "substantive_cycle_count": 1 if _is_substantive(current_metrics) else 0,
        "duration_seconds": float(current_metrics.get("duration_seconds") or 0.0),
        **{key: int(current_metrics.get(key) or 0) for key in _AGGREGATE_KEYS},
    }
    result.update(
        {
            "published_path": rel.as_posix(),
            "timestamp_end_utc": metrics.get("timestamp_end_utc") or latest.get("timestamp_end_utc"),
            "published_cycle_count": len(rows),
            "historical_unique_cycle_count": len(historical),
            "selected_from_git_history": selected_from_history,
            "selected_history_commit": selected.get("commit") if selected else None,
            "current_cycle_key": current_metrics.get("cycle_key"),
            "selected_cycle_key": metrics.get("cycle_key"),
            "transcript_available": bool(events),
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
    if len(rows) > 1:
        result.setdefault("findings", []).append(
            {
                "code": "published_prompt_has_multiple_cycles",
                "count": len(rows),
                "note": "multiple cycles share this PROMPT_ID; aggregate_substantive excludes trivial follow-ups",
            }
        )
    if selected_from_history:
        result.setdefault("findings", []).append(
            {
                "code": "latest_prompt_files_overwrote_substantive_cycle",
                "current_cycle_key": current_metrics.get("cycle_key"),
                "selected_cycle_key": metrics.get("cycle_key"),
                "note": "analysis recovered the latest substantive cycle from Git history",
            }
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyze one published Codex PROMPT_ID with metrics, transcript and Git-history recovery."
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
