#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from scripts import analyze_published_prompt as published


_NUMERIC_FIELDS = (
    "total_tokens",
    "input_tokens",
    "cached_input_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "tool_call_count",
    "duration_seconds",
    "cache_ratio",
)


def _num(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _summary(metrics: dict[str, Any]) -> dict[str, Any]:
    uncached = int(metrics.get("uncached_input_tokens") or 0)
    output = int(metrics.get("output_tokens") or 0)
    return {
        "cycle_key": metrics.get("cycle_key"),
        "status": str(metrics.get("status") or "UNKNOWN").upper(),
        "timestamp_start_utc": metrics.get("timestamp_start_utc"),
        "timestamp_end_utc": metrics.get("timestamp_end_utc"),
        "native_session_id": metrics.get("native_session_id"),
        "chat_id": metrics.get("chat_id"),
        "model": metrics.get("model"),
        "reasoning_effort": metrics.get("reasoning_effort"),
        "duration_seconds": metrics.get("duration_seconds"),
        "total_tokens": metrics.get("total_tokens"),
        "input_tokens": metrics.get("input_tokens"),
        "cached_input_tokens": metrics.get("cached_input_tokens"),
        "uncached_input_tokens": metrics.get("uncached_input_tokens"),
        "output_tokens": metrics.get("output_tokens"),
        "reasoning_output_tokens": metrics.get("reasoning_output_tokens"),
        "tool_call_count": metrics.get("tool_call_count"),
        "cache_ratio": metrics.get("cache_ratio"),
        # Useful efficiency proxy, not an OpenAI billing/quota metric.
        "uncached_plus_output_tokens": uncached + output,
    }


def _delta(first: dict[str, Any], terminal: dict[str, Any]) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    for field in _NUMERIC_FIELDS:
        before = _num(first.get(field))
        after = _num(terminal.get(field))
        absolute = after - before
        item: dict[str, Any] = {"before": first.get(field), "after": terminal.get(field), "delta": absolute}
        if before:
            item["pct_change"] = (absolute / before) * 100.0
        else:
            item["pct_change"] = None
        delta[field] = item
    before_proxy = int(first.get("uncached_plus_output_tokens") or 0)
    after_proxy = int(terminal.get("uncached_plus_output_tokens") or 0)
    proxy_delta = after_proxy - before_proxy
    delta["uncached_plus_output_tokens"] = {
        "before": before_proxy,
        "after": after_proxy,
        "delta": proxy_delta,
        "pct_change": (proxy_delta / before_proxy) * 100.0 if before_proxy else None,
        "note": "efficiency proxy only; not an OpenAI billing/quota metric",
    }
    return delta


def analyze_lifecycle(root: Path, prompt_id: str) -> dict[str, Any]:
    root = root.expanduser().resolve()
    cycles = published._merge_cycles(
        published._stored_prompt_cycles(root, prompt_id),
        published._historical_prompt_cycles(root, prompt_id),
    )
    if not cycles:
        raise RuntimeError(f"PROMPT_ID={prompt_id} has no recoverable cycles")
    cycles.sort(key=lambda cycle: str(cycle["metrics"].get("timestamp_end_utc") or ""))
    substantive = [cycle for cycle in cycles if published._is_substantive(cycle["metrics"])] or cycles
    summaries = [_summary(cycle["metrics"]) for cycle in substantive]
    first = summaries[0]
    terminal = summaries[-1]
    statuses = [item["status"] for item in summaries]
    unique_statuses = list(dict.fromkeys(statuses))
    same_native_session = len({item.get("native_session_id") for item in summaries if item.get("native_session_id")}) <= 1
    same_chat = len({item.get("chat_id") for item in summaries if item.get("chat_id") is not None}) <= 1
    return {
        "prompt_id": str(prompt_id),
        "substantive_cycle_count": len(summaries),
        "status_lifecycle": statuses,
        "status_transition": " -> ".join(unique_statuses),
        "first_cycle": first,
        "terminal_cycle": terminal,
        "terminal_vs_first": _delta(first, terminal),
        "same_native_session": same_native_session,
        "same_chat": same_chat,
        "continued_in_same_chat": len(summaries) > 1 and same_native_session and same_chat,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarize retry/lifecycle efficiency for one multi-cycle published PROMPT_ID."
    )
    parser.add_argument("published_root", type=Path)
    parser.add_argument("prompt_id")
    args = parser.parse_args(argv)
    try:
        result = analyze_lifecycle(args.published_root, args.prompt_id)
    except RuntimeError as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
