#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
from typing import Any


MEMORY_MARKER = "/.codex/memories/MEMORY.md"
ADB_INSTALL_RE = re.compile(r"\badb\s+(?P<before_install>[^;&|\n]*?)\binstall\b", re.I)


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


def _command_text(event: dict[str, Any]) -> str:
    if event.get("tool_name") not in {"exec_command", "shell"}:
        return ""
    args = _json_obj(event.get("content_text"))
    command = args.get("cmd")
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    return " ".join(command.split()) if isinstance(command, str) else ""


def _unscoped_adb_installs(command: str) -> int:
    count = 0
    for match in ADB_INSTALL_RE.finditer(command):
        before_install = match.group("before_install")
        if not re.search(r"(?:^|\s)-s\s+\S+", before_install):
            count += 1
    return count


def analyze(metrics: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    repo_paths = [str(value) for value in metrics.get("repo_paths") or []]
    unique_repo_paths = list(dict.fromkeys(repo_paths))
    commands = [command for event in events if (command := _command_text(event))]
    memory_reads = sum(MEMORY_MARKER in command for command in commands)
    unscoped_adb_installs = sum(_unscoped_adb_installs(command) for command in commands)
    trace_processor_commands = sum("trace_processor" in command for command in commands)

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
    if unscoped_adb_installs:
        findings.append({"code": "unscoped_adb_install", "count": unscoped_adb_installs})
    if trace_processor_commands >= 3:
        findings.append({"code": "trace_processor_discovery_churn", "count": trace_processor_commands})

    tool_calls = int(metrics.get("tool_call_count") or 0)
    input_tokens = int(metrics.get("input_tokens") or 0)
    cached = int(metrics.get("cached_input_tokens") or 0)
    uncached = int(metrics.get("uncached_input_tokens") or max(0, input_tokens - cached))
    duration = float(metrics.get("duration_seconds") or 0.0)
    cache_ratio = (cached / input_tokens) if input_tokens else None
    uncached_share = (uncached / input_tokens) if input_tokens else None
    tool_calls_per_minute = (tool_calls * 60.0 / duration) if duration > 0 else None

    if tool_calls >= 40:
        findings.append({"code": "high_tool_call_count", "value": tool_calls})
    if uncached >= 50_000:
        findings.append({"code": "high_uncached_input_tokens", "value": uncached})
    if tool_calls >= 40 and uncached < 10_000:
        findings.append(
            {
                "code": "roundtrip_heavy_cached_session",
                "tool_calls": tool_calls,
                "uncached_input_tokens": uncached,
            }
        )

    return {
        "prompt_id": metrics.get("prompt_id"),
        "total_tokens": metrics.get("total_tokens"),
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "uncached_input_tokens": uncached,
        "cache_ratio": cache_ratio,
        "uncached_input_share": uncached_share,
        "tool_call_count": tool_calls,
        "tool_calls_per_minute": tool_calls_per_minute,
        "repo_path_count": len(repo_paths),
        "unique_repo_path_count": len(unique_repo_paths),
        "memory_read_count": memory_reads,
        "unscoped_adb_install_count": unscoped_adb_installs,
        "trace_processor_command_count": trace_processor_commands,
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
