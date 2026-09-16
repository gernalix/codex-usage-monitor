#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
from typing import Any


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def _parse_value(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def parse_expectation(text: str) -> tuple[str, Any]:
    if "=" not in text:
        raise RuntimeError(f"invalid --expect {text!r}; expected KEY=VALUE")
    key, raw = text.split("=", 1)
    key = key.strip()
    if not key:
        raise RuntimeError(f"invalid --expect {text!r}; empty key")
    return key, _parse_value(raw)


def _unknown_field_message(key: str, available: list[str]) -> str:
    suggestion = difflib.get_close_matches(key, available, n=1, cutoff=0.45)
    suffix = f"; did you mean {suggestion[0]!r}?" if suggestion else ""
    return f"unknown metrics field {key!r}{suffix}"


def assert_published_prompt(
    published_root: Path,
    prompt_id: str,
    *,
    expectations: list[tuple[str, Any]],
    transcript_contains: list[str] | None = None,
    transcript_not_contains: list[str] | None = None,
) -> dict[str, Any]:
    root = published_root.expanduser().resolve()
    prompt_dir = root / "prompts" / str(prompt_id)
    metrics_path = prompt_dir / "metrics.json"
    transcript_path = prompt_dir / "transcript.jsonl"
    metrics = _load_object(metrics_path)

    if str(metrics.get("prompt_id") or "") != str(prompt_id):
        raise RuntimeError(
            f"prompt_id mismatch in {metrics_path}: {metrics.get('prompt_id')!r} != {prompt_id!r}"
        )

    available = sorted(metrics)
    unknown = [key for key, _expected in expectations if key not in metrics]
    if unknown:
        raise RuntimeError("; ".join(_unknown_field_message(key, available) for key in unknown))

    checked: dict[str, dict[str, Any]] = {}
    mismatches: list[dict[str, Any]] = []
    for key, expected in expectations:
        actual = metrics[key]
        ok = actual == expected
        checked[key] = {"actual": actual, "expected": expected, "ok": ok}
        if not ok:
            mismatches.append({"field": key, "actual": actual, "expected": expected})

    contains = transcript_contains or []
    not_contains = transcript_not_contains or []
    transcript_checks: list[dict[str, Any]] = []
    if contains or not_contains:
        try:
            transcript = transcript_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError as exc:
            raise RuntimeError(f"missing file: {transcript_path}") from exc
        for needle in contains:
            ok = needle in transcript
            transcript_checks.append({"kind": "contains", "text": needle, "ok": ok})
            if not ok:
                mismatches.append({"transcript": "missing", "text": needle})
        for needle in not_contains:
            ok = needle not in transcript
            transcript_checks.append({"kind": "not_contains", "text": needle, "ok": ok})
            if not ok:
                mismatches.append({"transcript": "unexpected", "text": needle})

    return {
        "ok": not mismatches,
        "prompt_id": str(prompt_id),
        "metrics_path": str(metrics_path),
        "transcript_path": str(transcript_path),
        "checked": checked,
        "transcript_checks": transcript_checks,
        "mismatches": mismatches,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Assert fields in a published codex-usage prompt without ad-hoc schema guesses. "
            "Unknown field names fail before value comparison and suggest the closest canonical field."
        )
    )
    parser.add_argument("published_root", type=Path)
    parser.add_argument("prompt_id")
    parser.add_argument("--expect", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--transcript-contains", action="append", default=[])
    parser.add_argument("--transcript-not-contains", action="append", default=[])
    args = parser.parse_args(argv)

    try:
        expectations = [parse_expectation(item) for item in args.expect]
        result = assert_published_prompt(
            args.published_root,
            args.prompt_id,
            expectations=expectations,
            transcript_contains=args.transcript_contains,
            transcript_not_contains=args.transcript_not_contains,
        )
    except RuntimeError as exc:
        parser.exit(2, f"error: {exc}\n")

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
