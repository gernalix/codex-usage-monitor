#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from scripts import analyze_prompt_efficiency as base
from scripts import analyze_recent_prompts as recent


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


def analyze_published_prompt(root: Path, prompt_id: str) -> dict[str, Any]:
    """Analyze the latest published cycle for one PROMPT_ID, including its transcript."""
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
    metrics = _load_json(metrics_path)
    events = base.load_jsonl(transcript_path)
    result = recent.enrich(metrics, events)
    goal_tokens, goal_seconds = _goal_usage(events)
    metrics_tokens = int(metrics.get("total_tokens") or 0)
    result.update(
        {
            "published_path": rel.as_posix(),
            "timestamp_end_utc": latest.get("timestamp_end_utc"),
            "published_cycle_count": len(rows),
            "transcript_available": transcript_path.is_file(),
            "goal_reported_tokens": goal_tokens,
            "goal_reported_time_seconds": goal_seconds,
            "goal_vs_metrics_token_delta": (goal_tokens - metrics_tokens) if goal_tokens is not None else None,
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
                "note": "published prompts/<id> files represent the latest cycle; use the local cost index for cross-attempt totals",
            }
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyze one published Codex PROMPT_ID with metrics and transcript findings."
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
