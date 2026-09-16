#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
from typing import Any


MEMORY_MARKER = "/.codex/memories/MEMORY.md"
ROADMAP_META_MARKERS = (
    "/codex-roadmap/README.md",
    "/codex-roadmap/roadmap.md",
    "/codex-roadmap/spiegazioni.md",
    "/codex-roadmap/STANDARD_PROMPT.md",
)
ROUNDTRIP_HEAVY_TOOL_CALL_THRESHOLD = 30
HIGH_TOOL_CALL_RATE_THRESHOLD = 8.0
LARGE_TOOL_OUTPUT_TOKEN_THRESHOLD = 5_000
DEFAULT_COST_DB = Path.home() / ".local/share/codex-session-archive/index/task_costs.sqlite"
ADB_INSTALL_RE = re.compile(r"\badb\s+(?P<before_install>[^;&|\n]*?)\binstall\b", re.I)
TOOL_OUTPUT_TOKEN_RE = re.compile(r"\boriginal token count:\s*(\d+)\b", re.I)
TRUNCATED_TOOL_OUTPUT_RE = re.compile(r"(?:warning:\s*)?truncated output|\.\.\.\s*\(truncated\)", re.I)
PROCESS_EXIT_RE = re.compile(r"\bProcess exited with code\s+(-?\d+)\b", re.I)
SQLITE_READONLY_RE = re.compile(r"attempt to write a readonly database", re.I)
PYTHON_IMPORT_RE = re.compile(r"ModuleNotFoundError:\s*No module named", re.I)
SCHEMA_PROBE_RE = re.compile(r"(?:\bpragma\s+table_info\s*\(|(?:^|[\s'\"])\.tables(?:[\s'\"]|$))", re.I)
REPORTED_STATUS_RE = re.compile(
    r"(?im)^\s*STATUS\s*:\s*(PASS|FAIL|BLOCKED|PARTIAL|WAITING_FOR_EVENT|BLOCKED_REPO_PUBLIC)\b"
)
JOURNAL_SEGMENT_RE = re.compile(r"\bjournalctl\b(?P<args>[^;|\n]*)(?=[;|\n]|$)", re.I)


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


def load_prompt_attempts(db_path: Path, prompt_id: str) -> list[dict[str, Any]]:
    """Read every completed cost row for one PROMPT_ID without reparsing rollouts."""
    db_path = db_path.expanduser().resolve()
    if not db_path.is_file():
        raise RuntimeError(f"cost database not found: {db_path}; run codex_task_costs.py to rebuild it")
    uri = f"file:{db_path}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as con:
            con.row_factory = sqlite3.Row
            exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prompt_costs'"
            ).fetchone()
            if exists is None:
                raise RuntimeError("prompt_costs table missing; run codex_task_costs.py to rebuild metrics")
            return [
                dict(row)
                for row in con.execute(
                    """
                    SELECT * FROM prompt_costs
                    WHERE prompt_id=? AND COALESCE(completion_state, '') <> 'eof_incomplete'
                    ORDER BY first_timestamp_utc, source_path, prompt_seq
                    """,
                    (prompt_id,),
                ).fetchall()
            ]
    except sqlite3.Error as exc:
        raise RuntimeError(f"cannot read cost database: {exc}") from exc


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


def _roadmap_meta_read(command: str) -> bool:
    return any(marker in command for marker in ROADMAP_META_MARKERS)


def _tool_output_token_count(event: dict[str, Any]) -> int | None:
    if event.get("subtype") not in {"function_call_output", "custom_tool_call_output"}:
        return None
    text = event.get("content_text")
    if not isinstance(text, str):
        return None
    match = TOOL_OUTPUT_TOKEN_RE.search(text)
    return int(match.group(1)) if match else None


def _truncated_tool_output(event: dict[str, Any]) -> bool:
    if event.get("subtype") not in {"function_call_output", "custom_tool_call_output"}:
        return False
    text = event.get("content_text")
    return bool(isinstance(text, str) and TRUNCATED_TOOL_OUTPUT_RE.search(text))


def _tool_failure_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "failed_commands": 0,
        "sqlite_readonly_failures": 0,
        "python_import_failures": 0,
    }
    for event in events:
        if event.get("subtype") not in {"function_call_output", "custom_tool_call_output"}:
            continue
        text = event.get("content_text")
        if not isinstance(text, str):
            continue
        match = PROCESS_EXIT_RE.search(text)
        if match and int(match.group(1)) != 0:
            counts["failed_commands"] += 1
        if SQLITE_READONLY_RE.search(text):
            counts["sqlite_readonly_failures"] += 1
        if PYTHON_IMPORT_RE.search(text):
            counts["python_import_failures"] += 1
    return counts


def _broad_boot_journal_scans(command: str) -> int:
    count = 0
    for match in JOURNAL_SEGMENT_RE.finditer(command):
        args = f" {match.group('args')} "
        if not re.search(r"(?:^|\s)-b(?:\s|$)", args):
            continue
        if any(marker in args for marker in (" --since", " --until", " --lines", " -n ", " --cursor", " --after-cursor")):
            continue
        if re.search(r"(?:^|\s)(?:-u|--unit)(?:=|\s)", args):
            continue
        count += 1
    return count


def _reported_status(metrics: dict[str, Any]) -> str | None:
    final = metrics.get("final_response_redacted")
    if not isinstance(final, str):
        return None
    match = REPORTED_STATUS_RE.search(final)
    return match.group(1).upper() if match else None


def analyze(metrics: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    repo_paths = [str(value) for value in metrics.get("repo_paths") or []]
    unique_repo_paths = list(dict.fromkeys(repo_paths))
    commands = [command for event in events if (command := _command_text(event))]
    memory_reads = sum(MEMORY_MARKER in command for command in commands)
    roadmap_meta_reads = sum(_roadmap_meta_read(command) for command in commands)
    avoidable_context_reads = memory_reads + roadmap_meta_reads
    unscoped_adb_installs = sum(_unscoped_adb_installs(command) for command in commands)
    trace_processor_commands = sum("trace_processor" in command for command in commands)
    broad_boot_journal_scans = sum(_broad_boot_journal_scans(command) for command in commands)
    schema_probe_commands = sum(bool(SCHEMA_PROBE_RE.search(command)) for command in commands)
    failure_counts = _tool_failure_counts(events)

    tool_output_counts = [
        count
        for event in events
        if (count := _tool_output_token_count(event)) is not None
    ]
    large_tool_outputs = [count for count in tool_output_counts if count >= LARGE_TOOL_OUTPUT_TOKEN_THRESHOLD]
    truncated_tool_outputs = sum(_truncated_tool_output(event) for event in events)

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
    if roadmap_meta_reads:
        findings.append({"code": "roadmap_meta_reads", "count": roadmap_meta_reads})
    if avoidable_context_reads:
        findings.append({"code": "avoidable_context_reads", "count": avoidable_context_reads})
    if repeated:
        findings.append({"code": "repeated_exact_commands", "count": sum(item["count"] - 1 for item in repeated)})
    if unscoped_adb_installs:
        findings.append({"code": "unscoped_adb_install", "count": unscoped_adb_installs})
    if trace_processor_commands >= 3:
        findings.append({"code": "trace_processor_discovery_churn", "count": trace_processor_commands})
    if broad_boot_journal_scans:
        findings.append({"code": "broad_boot_journal_scans", "count": broad_boot_journal_scans})
    if schema_probe_commands >= 2:
        findings.append({"code": "schema_discovery_churn", "count": schema_probe_commands})
    if failure_counts["failed_commands"] >= 3:
        findings.append({"code": "failed_command_churn", "count": failure_counts["failed_commands"]})
    if failure_counts["sqlite_readonly_failures"] >= 2:
        findings.append({"code": "sqlite_readonly_retry", "count": failure_counts["sqlite_readonly_failures"]})
    if failure_counts["python_import_failures"]:
        findings.append({"code": "python_import_retry", "count": failure_counts["python_import_failures"]})
    if large_tool_outputs:
        findings.append(
            {
                "code": "large_tool_outputs",
                "count": len(large_tool_outputs),
                "max_tokens": max(large_tool_outputs),
            }
        )
    if truncated_tool_outputs:
        findings.append({"code": "truncated_tool_outputs", "count": truncated_tool_outputs})

    tool_calls = int(metrics.get("tool_call_count") or 0)
    input_tokens = int(metrics.get("input_tokens") or 0)
    cached = int(metrics.get("cached_input_tokens") or 0)
    uncached = int(metrics.get("uncached_input_tokens") or max(0, input_tokens - cached))
    output_tokens = int(metrics.get("output_tokens") or 0)
    reasoning_output_tokens = int(metrics.get("reasoning_output_tokens") or 0)
    duration = float(metrics.get("duration_seconds") or 0.0)
    cache_ratio = (cached / input_tokens) if input_tokens else None
    uncached_share = (uncached / input_tokens) if input_tokens else None
    tool_calls_per_minute = (tool_calls * 60.0 / duration) if duration > 0 else None
    stored_status = str(metrics.get("status") or "").upper() or None
    reported_status = _reported_status(metrics)

    if tool_calls >= 40:
        findings.append({"code": "high_tool_call_count", "value": tool_calls})
    if tool_calls >= 20 and tool_calls_per_minute is not None and tool_calls_per_minute >= HIGH_TOOL_CALL_RATE_THRESHOLD:
        findings.append({"code": "high_tool_call_rate", "value": round(tool_calls_per_minute, 3)})
    if uncached >= 50_000:
        findings.append({"code": "high_uncached_input_tokens", "value": uncached})
    if tool_calls >= ROUNDTRIP_HEAVY_TOOL_CALL_THRESHOLD and uncached < 10_000:
        findings.append(
            {
                "code": "roundtrip_heavy_cached_session",
                "tool_calls": tool_calls,
                "uncached_input_tokens": uncached,
            }
        )
    if stored_status and reported_status and stored_status != reported_status:
        findings.append(
            {
                "code": "status_parse_mismatch",
                "stored_status": stored_status,
                "reported_status": reported_status,
            }
        )

    tool_calls_by_type = metrics.get("tool_calls_by_type")
    if not isinstance(tool_calls_by_type, dict):
        tool_calls_by_type = {}

    return {
        "prompt_id": metrics.get("prompt_id"),
        "total_tokens": metrics.get("total_tokens"),
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "uncached_input_tokens": uncached,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "duration_seconds": duration,
        "cache_ratio": cache_ratio,
        "uncached_input_share": uncached_share,
        "tool_call_count": tool_calls,
        "tool_calls_by_type": tool_calls_by_type,
        "tool_calls_per_minute": tool_calls_per_minute,
        "repo_path_count": len(repo_paths),
        "unique_repo_path_count": len(unique_repo_paths),
        "memory_read_count": memory_reads,
        "roadmap_meta_read_count": roadmap_meta_reads,
        "avoidable_context_read_count": avoidable_context_reads,
        "unscoped_adb_install_count": unscoped_adb_installs,
        "trace_processor_command_count": trace_processor_commands,
        "broad_boot_journal_scan_count": broad_boot_journal_scans,
        "schema_probe_command_count": schema_probe_commands,
        "failed_command_count": failure_counts["failed_commands"],
        "sqlite_readonly_failure_count": failure_counts["sqlite_readonly_failures"],
        "python_import_failure_count": failure_counts["python_import_failures"],
        "tool_output_tokens_observed": sum(tool_output_counts),
        "max_tool_output_tokens": max(tool_output_counts, default=0),
        "large_tool_output_count": len(large_tool_outputs),
        "truncated_tool_output_count": truncated_tool_outputs,
        "stored_status": stored_status,
        "reported_status": reported_status,
        "repeated_exact_commands": repeated,
        "findings": findings,
    }


def aggregate_attempts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate all completed attempts for a PROMPT_ID into one efficiency view."""
    if not rows:
        return {"prompt_id": None, "attempt_count": 0, "attempts": [], "findings": []}

    def sum_int(key: str) -> int:
        return sum(int(row.get(key) or 0) for row in rows)

    prompt_ids = list(dict.fromkeys(str(row.get("prompt_id")) for row in rows if row.get("prompt_id")))
    input_tokens = sum_int("input_tokens")
    cached = sum_int("cached_input_tokens")
    uncached = sum_int("uncached_input_tokens")
    output_tokens = sum_int("output_tokens")
    reasoning_output_tokens = sum_int("reasoning_output_tokens")
    total_tokens = sum_int("total_tokens")
    tool_calls = sum_int("tool_call_count")
    duration = sum(float(row.get("duration_seconds") or 0.0) for row in rows)
    cache_ratio = (cached / input_tokens) if input_tokens else None
    uncached_share = (uncached / input_tokens) if input_tokens else None
    models = list(dict.fromkeys(str(row.get("model")) for row in rows if row.get("model")))
    reasoning_efforts = list(
        dict.fromkeys(str(row.get("reasoning_effort")) for row in rows if row.get("reasoning_effort"))
    )

    findings: list[dict[str, Any]] = []
    if len(rows) > 1:
        findings.append({"code": "multiple_prompt_attempts", "count": len(rows)})
    if len(rows) > 1 and tool_calls >= ROUNDTRIP_HEAVY_TOOL_CALL_THRESHOLD and uncached < 10_000:
        findings.append(
            {
                "code": "multi_attempt_roundtrip_churn",
                "attempts": len(rows),
                "tool_calls": tool_calls,
                "uncached_input_tokens": uncached,
            }
        )

    attempts = [
        {
            "started_at_utc": row.get("first_timestamp_utc"),
            "completion_state": row.get("completion_state"),
            "model": row.get("model"),
            "reasoning_effort": row.get("reasoning_effort"),
            "total_tokens": int(row.get("total_tokens") or 0),
            "uncached_input_tokens": int(row.get("uncached_input_tokens") or 0),
            "tool_call_count": int(row.get("tool_call_count") or 0),
            "duration_seconds": float(row.get("duration_seconds") or 0.0),
        }
        for row in rows
    ]

    return {
        "prompt_id": prompt_ids[0] if len(prompt_ids) == 1 else prompt_ids,
        "attempt_count": len(rows),
        "models": models,
        "reasoning_efforts": reasoning_efforts,
        "total_tokens": total_tokens,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "uncached_input_tokens": uncached,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "duration_seconds": duration,
        "cache_ratio": cache_ratio,
        "uncached_input_share": uncached_share,
        "tool_call_count": tool_calls,
        "tool_calls_per_attempt": tool_calls / len(rows),
        "attempts": attempts,
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize token/tool efficiency from published or indexed Codex metrics.")
    parser.add_argument("metrics", nargs="?", type=Path, help="Path to one prompt metrics.json file")
    parser.add_argument("--transcript", type=Path, help="Optional matching transcript.jsonl")
    parser.add_argument("--prompt-id", help="Aggregate every completed indexed attempt for this PROMPT_ID")
    parser.add_argument("--cost-db", type=Path, default=DEFAULT_COST_DB, help="prompt_costs SQLite index")
    args = parser.parse_args(argv)

    if args.prompt_id:
        if args.metrics is not None or args.transcript is not None:
            parser.error("--prompt-id cannot be combined with metrics/transcript paths")
        try:
            rows = load_prompt_attempts(args.cost_db, args.prompt_id)
        except RuntimeError as exc:
            parser.exit(2, f"error: {exc}\n")
        if not rows:
            parser.exit(1, f"no completed rows for PROMPT_ID={args.prompt_id}\n")
        result = aggregate_attempts(rows)
    else:
        if args.metrics is None:
            parser.error("provide metrics.json or --prompt-id")
        metrics = json.loads(args.metrics.read_text(encoding="utf-8"))
        if not isinstance(metrics, dict):
            parser.error("metrics JSON must contain an object")
        result = analyze(metrics, load_jsonl(args.transcript))

    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
