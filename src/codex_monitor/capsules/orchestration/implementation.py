from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import subprocess
from typing import Any

DEFAULT_ROADMAP_REPO = Path.home() / "projects/codex-roadmap"
DEFAULT_ROADMAP_DB = DEFAULT_ROADMAP_REPO / "roadmap.sqlite"
DEFAULT_HISTORY_DB = Path.home() / ".local/share/prompt-history/prompt_history.sqlite"
DEFAULT_GLOBAL_CHECKPOINT = DEFAULT_ROADMAP_REPO / "operations/task-state/CHATGPT-20260924-GLOBAL-RECOVERY.md"


class C2OrchestratorError(RuntimeError):
    pass


def _connect_readonly(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise C2OrchestratorError(f"missing database: {resolved}")
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return closing(conn)


def runnable_prompts(
    roadmap_db: Path = DEFAULT_ROADMAP_DB,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    with _connect_readonly(roadmap_db) as conn:
        rows = conn.execute(
            """SELECT prompt_id,title,project_id,project_name,repo,prompt_type,
                      model,reasoning,megavault_mode,queue_position,current_path,status
                 FROM v_runnable_prompts
                ORDER BY CASE WHEN queue_position IS NULL THEN 1 ELSE 0 END,
                         queue_position, created_at, prompt_id
                LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
    return [dict(row) for row in rows]


def _extract_next_action(text: str) -> str | None:
    match = re.search(r"(?ms)^## Next action\s*\n(.*?)(?=^##\s|\Z)", text)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def checkpoint_next_action(
    prompt_id: str,
    roadmap_repo: Path = DEFAULT_ROADMAP_REPO,
) -> dict[str, str | None]:
    state_dir = roadmap_repo.expanduser() / "operations/task-state"
    exact = state_dir / f"{prompt_id}.md"
    candidates = [exact] if exact.is_file() else []
    if not candidates and state_dir.is_dir():
        needle_patterns = (
            f"PROMPT_ID: {prompt_id}",
            f"PROMPT_ID={prompt_id}",
            f"PROMPT_ID `{prompt_id}`",
        )
        for path in sorted(state_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(needle in text for needle in needle_patterns):
                candidates.append(path)

    for path in candidates:
        text = path.read_text(encoding="utf-8", errors="replace")
        next_action = _extract_next_action(text)
        if next_action:
            return {
                "prompt_id": str(prompt_id),
                "state_file": str(path),
                "next_action": next_action,
            }
    return {
        "prompt_id": str(prompt_id),
        "state_file": str(candidates[0]) if candidates else None,
        "next_action": None,
    }


def global_checkpoint(
    roadmap_repo: Path = DEFAULT_ROADMAP_REPO,
) -> dict[str, str | None]:
    path = roadmap_repo.expanduser() / "operations/task-state/CHATGPT-20260924-GLOBAL-RECOVERY.md"
    if not path.is_file():
        return {"state_file": str(path), "next_action": None, "updated": None}
    text = path.read_text(encoding="utf-8", errors="replace")
    updated_match = re.search(r"(?m)^Updated:\s*(.+)$", text)
    return {
        "state_file": str(path),
        "next_action": _extract_next_action(text),
        "updated": updated_match.group(1).strip() if updated_match else None,
    }


def _fts_query(text: str) -> str:
    tokens = re.findall(r"[\w-]{3,}", text, flags=re.UNICODE)
    if not tokens:
        raise C2OrchestratorError("search text has no usable terms")
    return " OR ".join(
        '"' + token.replace('"', '""') + '"' for token in tokens[:24]
    )


def context_search(
    text: str,
    history_db: Path = DEFAULT_HISTORY_DB,
    *,
    limit: int = 12,
    source: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["prompt_fts MATCH ?"]
    params: list[Any] = [_fts_query(text)]
    if source:
        clauses.append("p.source=?")
        params.append(source)
    params.append(max(1, int(limit)))
    with _connect_readonly(history_db) as conn:
        rows = conn.execute(
            f"""SELECT p.prompt_uid,p.prompt_id,p.source,p.conversation_id,
                       p.message_id,p.role,p.title,p.created_at,p.repo,p.task_type,
                       snippet(prompt_fts,2,'[',']',' … ',32) AS snippet,
                       bm25(prompt_fts,2.0,5.0,1.5,1.5) AS lexical_rank
                  FROM prompt_fts
                  JOIN prompts p ON p.prompt_uid=prompt_fts.prompt_uid
                 WHERE {' AND '.join(clauses)}
                 ORDER BY lexical_rank
                 LIMIT ?""",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def _history_status(history_db: Path) -> dict[str, Any]:
    with _connect_readonly(history_db) as conn:
        sources = [
            dict(row)
            for row in conn.execute(
                """SELECT source,COUNT(*) AS records,MAX(ingested_at) AS last_ingested_at
                     FROM source_records GROUP BY source ORDER BY source"""
            )
        ]
        prompts = int(conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0])
    return {"prompts": prompts, "sources": sources}


def orchestrator_status(
    roadmap_db: Path = DEFAULT_ROADMAP_DB,
    roadmap_repo: Path = DEFAULT_ROADMAP_REPO,
    history_db: Path = DEFAULT_HISTORY_DB,
) -> dict[str, Any]:
    with _connect_readonly(roadmap_db) as conn:
        counts = {
            str(row["status"]): int(row["count"])
            for row in conn.execute(
                "SELECT status,COUNT(*) AS count FROM prompts GROUP BY status"
            )
        }
        running_rows = conn.execute(
            """SELECT prompt_id,title,project_id,project_name,repo,queue_position
                 FROM prompts WHERE status='running'
                 ORDER BY updated_at,prompt_id"""
        ).fetchall()
    running = []
    for row in running_rows:
        item = dict(row)
        item["checkpoint"] = checkpoint_next_action(
            str(row["prompt_id"]), roadmap_repo
        )
        running.append(item)
    return {
        "roadmap": {
            "status_counts": counts,
            "running": running,
            "runnable": runnable_prompts(roadmap_db, limit=10),
        },
        "global_checkpoint": global_checkpoint(roadmap_repo),
        "history": _history_status(history_db),
    }


def claim_prompt(
    prompt_id: str,
    roadmap_repo: Path = DEFAULT_ROADMAP_REPO,
    *,
    timeout: float = 120.0,
) -> dict[str, Any]:
    script = roadmap_repo.expanduser() / "tools/roadmap_start.py"
    if not script.is_file():
        raise C2OrchestratorError(f"missing single-writer claim helper: {script}")
    result = subprocess.run(
        [
            "python3",
            str(script),
            "--repo",
            str(roadmap_repo.expanduser()),
            "--prompt-id",
            str(prompt_id),
            "--timeout",
            str(float(timeout)),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=max(5.0, float(timeout) + 10.0),
    )
    payload_text = (result.stdout or result.stderr).strip()
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise C2OrchestratorError(
            f"roadmap claim returned invalid JSON: {payload_text[:500]}"
        ) from exc
    if result.returncode != 0 or payload.get("status") != "ok":
        raise C2OrchestratorError(
            str(payload.get("error") or payload.get("reason") or payload)
        )
    return payload


def finish_prompt(
    prompt_id: str,
    result: str,
    roadmap_repo: Path = DEFAULT_ROADMAP_REPO,
    *,
    confirm_executed: bool = True,
    timeout: float = 120.0,
) -> dict[str, Any]:
    normalized = str(result).upper()
    allowed = {"PASS", "BLOCKED", "FAIL", "CANCELLED", "UNKNOWN"}
    if normalized not in allowed:
        raise C2OrchestratorError(f"invalid result: {result}")
    script = roadmap_repo.expanduser() / "tools/roadmap_finish.py"
    if not script.is_file():
        raise C2OrchestratorError(f"missing single-writer finish helper: {script}")
    command = [
        "python3",
        str(script),
        "--repo",
        str(roadmap_repo.expanduser()),
        "--prompt-id",
        str(prompt_id),
        "--result",
        normalized,
    ]
    if confirm_executed:
        command.append("--confirm-executed")
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
        timeout=max(5.0, float(timeout) + 10.0),
    )
    payload_text = (completed.stdout or completed.stderr).strip()
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise C2OrchestratorError(
            f"roadmap finish returned invalid JSON: {payload_text[:500]}"
        ) from exc
    if completed.returncode != 0:
        raise C2OrchestratorError(
            str(payload.get("error") or payload.get("reason") or payload)
        )
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="C2 orchestration over canonical roadmap/checkpoints/history"
    )
    parser.add_argument("--roadmap-repo", default=str(DEFAULT_ROADMAP_REPO))
    parser.add_argument("--roadmap-db", default=str(DEFAULT_ROADMAP_DB))
    parser.add_argument("--history-db", default=str(DEFAULT_HISTORY_DB))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")

    runnable = sub.add_parser("runnable")
    runnable.add_argument("--limit", type=int, default=20)

    next_action = sub.add_parser("next")
    next_action.add_argument("prompt_id")

    search = sub.add_parser("search")
    search.add_argument("text")
    search.add_argument("--source")
    search.add_argument("--limit", type=int, default=12)

    claim = sub.add_parser("claim")
    claim.add_argument("prompt_id")
    claim.add_argument("--timeout", type=float, default=120.0)

    finish = sub.add_parser("finish")
    finish.add_argument("prompt_id")
    finish.add_argument("result", choices=("PASS", "BLOCKED", "FAIL", "CANCELLED", "UNKNOWN"))
    finish.add_argument("--timeout", type=float, default=120.0)
    finish.add_argument("--no-confirm-executed", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    roadmap_repo = Path(args.roadmap_repo)
    roadmap_db = Path(args.roadmap_db)
    history_db = Path(args.history_db)
    try:
        if args.command == "status":
            payload = orchestrator_status(roadmap_db, roadmap_repo, history_db)
        elif args.command == "runnable":
            payload = runnable_prompts(roadmap_db, limit=args.limit)
        elif args.command == "next":
            payload = checkpoint_next_action(args.prompt_id, roadmap_repo)
        elif args.command == "search":
            payload = context_search(
                args.text, history_db, limit=args.limit, source=args.source
            )
        elif args.command == "claim":
            payload = claim_prompt(
                args.prompt_id, roadmap_repo, timeout=args.timeout
            )
        elif args.command == "finish":
            payload = finish_prompt(
                args.prompt_id,
                args.result,
                roadmap_repo,
                confirm_executed=not args.no_confirm_executed,
                timeout=args.timeout,
            )
        else:
            raise AssertionError(args.command)
    except (C2OrchestratorError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
