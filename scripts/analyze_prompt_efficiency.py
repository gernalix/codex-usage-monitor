#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any


MEMORY_MARKER = "/.codex/memories/MEMORY.md"


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def load_jsonl(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def analyze(metrics: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    repo_paths = [str(value) for value in metrics.get("repo_paths") or []]
    unique_repo_paths = list(dict.fromkeys(repo_paths))
    commands: list[str] = []
    memory_reads = 0
    for event in events:
        if event.get("tool_name") not in {"exec_command", "shell"}:
            continue
        args = _json_obj(event.get("content_text"))
        command = args.get("cmd")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        if not isinstance(command, str) or not command.strip():
            continue
        normalized = " ".join(command.split())
        commands.append(normalized)
        if MEMORY_MARKER in normalized:
            memory_reads += 1

    repeated = [
        {"command": command, "count": count}
        for command, count in Counter(commands).most_common()
        if count > 1
    ]
    findings: list[dict[str, Any]] = []
    duplicates = len(repo_paths) - len(unique_repo_paths)
    if duplicates:
        findings.append({"code": "duplicate_repo_paths", "count": duplicates})
    if memory_reads:
        findings.append({"code": "memory_reads", "count": memory_reads})
    if repeated:
        findings.append({"code": "repeated_exact_commands", "count": sum(item["count"] - 1 for item in repeated)})

    tool_calls = int(metrics.get("tool_call_count") or 0)
    uncached = int(metrics.get("uncached_input_tokens") or 0)
    if tool_calls >= 40:
        findings.append({"code": "high_tool_call_count", "value": tool_calls})
    if uncached >= 50_000:
        findings.append({"code": "high_uncached_input_tokens", "value": uncached})

    return {
        "prompt_id": metrics.get("prompt_id"),
        "total_tokens": metrics.get("total_tokens"),
        "input_tokens": metrics.get("input_tokens"),
        "cached_input_tokens": metrics.get("cached_input_tokens"),
        "uncached_input_tokens": metrics.get("uncached_input_tokens"),
        "tool_call_count": tool_calls,
        "repo_path_count": len(repo_paths),
        "unique_repo_path_count": len(unique_repo_paths),
        "memory_read_count": memory_reads,
        "repeated_exact_commands": repeated,
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize token/tool efficiency from published Codex metrics.")
    parser.add_argument("metrics", type=Path, help="Path to a prompt metrics.json file")
    parser.add_argument("--transcript", type=Path, help="Optional matching transcript.jsonl")
    args = parser.parse_args(argv)

    metrics = json.loads(args.metrics.read_text(encoding="utf-8"))
    if not isinstance(metrics, dict):
        parser.error("metrics JSON must contain an object")
    result = analyze(metrics, load_jsonl(args.transcript))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
