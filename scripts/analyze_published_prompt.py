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
    result.update(
        {
            "published_path": rel.as_posix(),
            "timestamp_end_utc": latest.get("timestamp_end_utc"),
            "published_cycle_count": len(rows),
            "transcript_available": transcript_path.is_file(),
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
