#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_ARCHIVE_ROOT = Path.home() / ".local/share/codex-session-archive"
DEFAULT_VAULT_NAME = "vault"
LOCAL_TZ = ZoneInfo("Europe/Copenhagen")
PROMPT_RE = re.compile(r"\bPROMPT_ID\s*[:=]\s*([A-Za-z0-9_.-]+)\b", re.I)
PROJECT_RE = re.compile(r"\bproject_id\s*[:=]\s*([0-9]+)\b", re.I)
SENSITIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b"), "[OPENAI_KEY_REDACTED]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"), "[GITHUB_TOKEN_REDACTED]"),
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"), "[TELEGRAM_TOKEN_REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"), "[JWT_REDACTED]"),
)
BOTTLENECK_RE = re.compile(
    r"\b(error|failed|failure|fatal|exception|traceback|timeout|timed out|blocked|blocker|"
    r"cannot|can't|unable|not found|no such|missing|invalid|corrupt|collision|conflict|"
    r"retry|retries|workaround|fallback|interrupted|incomplete|stale|drift|bottleneck|"
    r"slow|latency|rate limit|quota|lock|contention|hang|stuck|oom|out of memory|"
    r"permission denied|nonzero|exit code [1-9]|code=[1-9]|status=[1-9])\b",
    re.I,
)
INJECTED_USER_PREFIXES = ("# AGENTS.md instructions", "<environment_context>")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def redact(text: str) -> str:
    out = text.replace("\x00", " ")
    for pattern, replacement in SENSITIVE_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def clean(text: str, limit: int | None = None) -> str:
    text = redact(text).strip()
    if limit and len(text) > limit:
        return text[:limit].rstrip() + "\n… [truncated in curated vault]"
    return text


def local_ts(value: str | None) -> str:
    if not value:
        return "unknown"
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(LOCAL_TZ).isoformat(timespec="seconds")
    except ValueError:
        return value


def wikilink(kind: str, ident: str, label: str | None = None) -> str:
    target = f"{kind}/{ident}"
    return f"[[{target}|{label or ident}]]"


def event_text(event: dict[str, Any]) -> str:
    value = event.get("content_text")
    return value if isinstance(value, str) else ""


def is_real_user_message(event: dict[str, Any]) -> bool:
    if event.get("top_type") != "response_item" or event.get("subtype") != "message" or event.get("role") != "user":
        return False
    text = event_text(event).lstrip()
    return bool(text) and not text.startswith(INJECTED_USER_PREFIXES)


def assistant_message(event: dict[str, Any]) -> bool:
    return event.get("top_type") == "response_item" and event.get("subtype") == "message" and event.get("role") == "assistant"


def read_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                events.append(obj)
    return events


def latest_prompt_id(text: str, current: str | None) -> str | None:
    matches = PROMPT_RE.findall(text)
    return matches[-1] if matches else current


def extract_tool_summary(event: dict[str, Any]) -> str:
    if event.get("subtype") != "function_call":
        return ""
    text = event_text(event)
    if not text:
        payload = event.get("payload_redacted")
        if isinstance(payload, dict):
            args = payload.get("arguments")
            if isinstance(args, str):
                text = args
    text = clean(text, 1800)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            cmd = obj.get("cmd")
            if isinstance(cmd, str):
                return clean(cmd, 1200)
    except Exception:
        pass
    return text


def detect_bottleneck(event: dict[str, Any]) -> tuple[str, str] | None:
    top = str(event.get("top_type") or "")
    subtype = str(event.get("subtype") or "")
    text = event_text(event)
    if not text:
        return None
    candidate = False
    reason = ""
    if top == "response_item" and subtype == "function_call_output":
        if re.search(r"Process exited with code [1-9][0-9]*", text, re.I):
            candidate, reason = True, "non-zero tool exit"
    elif top == "response_item" and subtype == "reasoning" and BOTTLENECK_RE.search(text):
        candidate, reason = True, "exposed reasoning summary indicates friction"
    elif assistant_message(event) and BOTTLENECK_RE.search(text):
        candidate, reason = True, "assistant progress indicates friction"
    if not candidate:
        return None
    return reason, clean(text, 1600)


def find_final_assistant_before(events: list[dict[str, Any]], idx: int) -> int | None:
    for pos in range(idx - 1, -1, -1):
        if assistant_message(events[pos]) and event_text(events[pos]).strip():
            return pos
        if is_real_user_message(events[pos]):
            break
    return None


def parse_session(path: Path) -> dict[str, Any]:
    events = read_events(path)
    session_id = path.stem
    if events and isinstance(events[0].get("session_id"), str):
        session_id = events[0]["session_id"]
    first_ts = next((e.get("timestamp_utc") for e in events if e.get("timestamp_utc")), None)
    last_ts = next((e.get("timestamp_utc") for e in reversed(events) if e.get("timestamp_utc")), None)

    prompt_ids: list[str] = []
    project_ids: list[str] = []
    current_prompt: str | None = None
    user_items: list[dict[str, Any]] = []
    assistant_items: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    bottlenecks: list[dict[str, Any]] = []
    final_indexes: set[int] = set()

    for idx, event in enumerate(events):
        text = event_text(event)
        if is_real_user_message(event):
            current_prompt = latest_prompt_id(text, current_prompt)
            if current_prompt and current_prompt not in prompt_ids:
                prompt_ids.append(current_prompt)
            for project_id in PROJECT_RE.findall(text):
                if project_id not in project_ids:
                    project_ids.append(project_id)
            user_items.append({"idx": idx, "prompt_id": current_prompt, "timestamp": event.get("timestamp_utc"), "text": clean(text)})
        if assistant_message(event):
            assistant_items.append({"idx": idx, "prompt_id": current_prompt, "timestamp": event.get("timestamp_utc"), "text": clean(text, 12000)})
        tool = extract_tool_summary(event)
        if tool:
            actions.append({"idx": idx, "prompt_id": current_prompt, "timestamp": event.get("timestamp_utc"), "tool": event.get("tool_name") or "tool", "text": tool})
        hit = detect_bottleneck(event)
        if hit:
            reason, excerpt = hit
            bottlenecks.append({"idx": idx, "prompt_id": current_prompt, "timestamp": event.get("timestamp_utc"), "reason": reason, "excerpt": excerpt})
        if event.get("top_type") == "event_msg" and event.get("subtype") in {"task_complete", "turn_complete"}:
            final_idx = find_final_assistant_before(events, idx)
            if final_idx is not None:
                final_indexes.add(final_idx)

    reports = [item for item in assistant_items if item["idx"] in final_indexes]
    progress = [item for item in assistant_items if item["idx"] not in final_indexes]
    bottlenecks = [item for item in bottlenecks if item["idx"] not in final_indexes]
    if not reports and assistant_items:
        reports = [assistant_items[-1]]
        progress = assistant_items[:-1]

    return {
        "session_id": session_id,
        "source": str(path),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "prompt_ids": prompt_ids,
        "project_ids": project_ids,
        "user_items": user_items,
        "progress": progress,
        "actions": actions,
        "reports": reports,
        "bottlenecks": bottlenecks,
    }


def backlink_line(meta: dict[str, Any], prompt_id: str | None = None) -> str:
    links = [wikilink("chats", meta["session_id"], f"chat {meta['session_id']}")]
    if prompt_id:
        links.append(wikilink("prompts", prompt_id, f"prompt {prompt_id}"))
    links.extend(wikilink("projects", pid, f"project {pid}") for pid in meta["project_ids"])
    return " · ".join(links)


def render_chat(meta: dict[str, Any]) -> str:
    sid = meta["session_id"]
    lines = [
        f"# Chat {sid}", "",
        f"- Chat: {wikilink('chats', sid, sid)}",
        f"- Started: {local_ts(meta['first_ts'])}",
        f"- Ended: {local_ts(meta['last_ts'])}",
        "- Prompts: " + (", ".join(wikilink("prompts", p, p) for p in meta["prompt_ids"]) or "none detected"),
        "- Projects: " + (", ".join(wikilink("projects", p, p) for p in meta["project_ids"]) or "none detected"),
        "", "## Prompts", "",
    ]
    for item in meta["user_items"]:
        pid = item["prompt_id"] or f"event-{item['idx']}"
        label = wikilink("prompts", pid, "PROMPT_ID=" + pid) if item["prompt_id"] else "User prompt"
        lines += [f"### {local_ts(item['timestamp'])} — {label}", "", item["text"], ""]
    lines += ["## Assistant progress", ""]
    for item in meta["progress"]:
        if item["text"]:
            lines += [f"### {local_ts(item['timestamp'])}", "", item["text"], ""]
    lines += ["## Actions", ""]
    for item in meta["actions"]:
        prompt_link = wikilink("prompts", item["prompt_id"], item["prompt_id"]) if item["prompt_id"] else "no prompt id"
        lines += [f"- **{local_ts(item['timestamp'])} · {item['tool']}** ({prompt_link})", "", "```text", item["text"], "```", ""]
    lines += ["## Bottleneck signals", ""]
    if meta["bottlenecks"]:
        for number, item in enumerate(meta["bottlenecks"], 1):
            bid = f"{sid}-{item['idx']}"
            label = f"B{number}: {item['reason']}"
            lines.append(f"- {wikilink('bottlenecks', bid, label)} — {local_ts(item['timestamp'])}")
    else:
        lines.append("- None detected by the conservative heuristic.")
    lines += ["", "## Final reports", ""]
    for number, item in enumerate(meta["reports"], 1):
        rid = f"{sid}-{item['idx']}"
        lines.append(f"- {wikilink('reports', rid, f'Report {number}')} — {local_ts(item['timestamp'])}")
    lines += ["", "## Navigation", "", "- " + backlink_line(meta), ""]
    return "\n".join(lines)


def render_prompt(meta: dict[str, Any], prompt_id: str) -> str:
    user = [x for x in meta["user_items"] if x["prompt_id"] == prompt_id]
    progress = [x for x in meta["progress"] if x["prompt_id"] == prompt_id]
    actions = [x for x in meta["actions"] if x["prompt_id"] == prompt_id]
    reports = [x for x in meta["reports"] if x["prompt_id"] == prompt_id]
    bottlenecks = [x for x in meta["bottlenecks"] if x["prompt_id"] == prompt_id]
    lines = [f"# PROMPT_ID={prompt_id}", "", f"- Chat: {wikilink('chats', meta['session_id'])}", "- Projects: " + (", ".join(wikilink("projects", p) for p in meta["project_ids"]) or "none detected"), ""]
    for item in user:
        lines += [f"## Prompt — {local_ts(item['timestamp'])}", "", item["text"], ""]
    if progress:
        lines += ["## Progress", ""]
        for item in progress:
            lines += [f"### {local_ts(item['timestamp'])}", "", item["text"], ""]
    if actions:
        lines += ["## Actions", ""]
        for item in actions:
            lines += [f"- **{local_ts(item['timestamp'])} · {item['tool']}**", "", "```text", item["text"], "```", ""]
    lines += ["## Bottleneck signals", ""]
    if bottlenecks:
        for item in bottlenecks:
            bid = f"{meta['session_id']}-{item['idx']}"
            lines.append(f"- {wikilink('bottlenecks', bid, item['reason'])} — {local_ts(item['timestamp'])}")
    else:
        lines.append("- None detected by the conservative heuristic.")
    lines += ["", "## Final reports", ""]
    if reports:
        for item in reports:
            rid = f"{meta['session_id']}-{item['idx']}"
            lines.append(f"- {wikilink('reports', rid, rid)}")
    else:
        lines.append("- No final report confidently associated with this prompt.")
    lines += ["", "## Navigation", "", "- " + backlink_line(meta, prompt_id), ""]
    return "\n".join(lines)


def render_report(meta: dict[str, Any], item: dict[str, Any]) -> tuple[str, str]:
    rid = f"{meta['session_id']}-{item['idx']}"
    pid = item["prompt_id"]
    lines = [f"# Final report {rid}", "", f"- Timestamp: {local_ts(item['timestamp'])}", f"- Chat: {wikilink('chats', meta['session_id'])}"]
    if pid:
        lines.append(f"- Prompt: {wikilink('prompts', pid)}")
    if meta["project_ids"]:
        lines.append("- Projects: " + ", ".join(wikilink("projects", p) for p in meta["project_ids"]))
    related = [x for x in meta["bottlenecks"] if x["prompt_id"] == pid]
    if related:
        lines.append("- Bottlenecks: " + ", ".join(wikilink("bottlenecks", f"{meta['session_id']}-{x['idx']}") for x in related))
    lines += ["", "## Report", "", item["text"], "", "## Navigation", "", "- " + backlink_line(meta, pid), ""]
    return rid, "\n".join(lines)


def render_bottleneck(meta: dict[str, Any], item: dict[str, Any]) -> tuple[str, str]:
    bid = f"{meta['session_id']}-{item['idx']}"
    pid = item["prompt_id"]
    lines = [f"# Bottleneck signal {bid}", "", f"- Timestamp: {local_ts(item['timestamp'])}", f"- Reason: {item['reason']}", f"- Chat: {wikilink('chats', meta['session_id'])}"]
    if pid:
        lines.append(f"- Prompt: {wikilink('prompts', pid)}")
    if meta["project_ids"]:
        lines.append("- Projects: " + ", ".join(wikilink("projects", p) for p in meta["project_ids"]))
    reports = [x for x in meta["reports"] if x["prompt_id"] == pid]
    if reports:
        lines.append("- Final report: " + ", ".join(wikilink("reports", f"{meta['session_id']}-{x['idx']}") for x in reports))
    lines += ["", "## Evidence", "", "```text", item["excerpt"], "```", "", "## Follow-up", "", "This is a signal, not a diagnosis. Investigate separately if it is recurrent, costly, or blocks progress.", "", "## Navigation", "", "- " + backlink_line(meta, pid), ""]
    return bid, "\n".join(lines)


def rebuild_indexes(vault: Path, sessions: dict[str, dict[str, Any]]) -> None:
    chats = sorted(sessions.values(), key=lambda m: m.get("first_ts") or "")
    index = ["# Codex Curated Vault", "", "Generated from normalized Codex session events. Raw/private reasoning is not copied here.", "", "## Chats", ""]
    for meta in chats:
        suffix = f" — projects {', '.join(meta['project_ids'])}" if meta["project_ids"] else ""
        index.append(f"- {wikilink('chats', meta['session_id'])} — {local_ts(meta['first_ts'])}{suffix}")
    index += ["", "## Prompts", ""]
    for meta in chats:
        for pid in meta["prompt_ids"]:
            index.append(f"- {wikilink('prompts', pid)} — {local_ts(meta['first_ts'])} — {wikilink('chats', meta['session_id'])}")
    atomic_write(vault / "index.md", "\n".join(index).rstrip() + "\n")

    projects: dict[str, list[dict[str, Any]]] = {}
    for meta in chats:
        for pid in meta["project_ids"]:
            projects.setdefault(pid, []).append(meta)
    for project_id, metas in projects.items():
        lines = [f"# Project {project_id}", "", "## Chats", ""]
        for meta in metas:
            lines.append(f"- {wikilink('chats', meta['session_id'])} — {local_ts(meta['first_ts'])}")
        lines += ["", "## Prompts", ""]
        for meta in metas:
            for prompt_id in meta["prompt_ids"]:
                lines.append(f"- {wikilink('prompts', prompt_id)} — {wikilink('chats', meta['session_id'])}")
        lines += ["", "## Final reports", ""]
        for meta in metas:
            for item in meta["reports"]:
                rid = f"{meta['session_id']}-{item['idx']}"
                lines.append(f"- {wikilink('reports', rid, rid)} — {local_ts(item['timestamp'])}")
        lines += ["", "## Bottleneck signals", ""]
        for meta in metas:
            for item in meta["bottlenecks"]:
                bid = f"{meta['session_id']}-{item['idx']}"
                lines.append(f"- {wikilink('bottlenecks', bid, item['reason'])} — {local_ts(item['timestamp'])}")
        atomic_write(vault / "projects" / f"{project_id}.md", "\n".join(lines).rstrip() + "\n")


def write_session(vault: Path, meta: dict[str, Any]) -> None:
    atomic_write(vault / "chats" / f"{meta['session_id']}.md", render_chat(meta))
    for pid in meta["prompt_ids"]:
        atomic_write(vault / "prompts" / f"{pid}.md", render_prompt(meta, pid))
    for item in meta["reports"]:
        rid, text = render_report(meta, item)
        atomic_write(vault / "reports" / f"{rid}.md", text)
    for item in meta["bottlenecks"]:
        bid, text = render_bottleneck(meta, item)
        atomic_write(vault / "bottlenecks" / f"{bid}.md", text)


def fingerprint(path: Path) -> str:
    st = path.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


def load_state(path: Path) -> dict[str, Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def command_build(args: argparse.Namespace) -> int:
    archive_root = Path(args.archive_root).expanduser()
    normalized = archive_root / "normalized/sessions"
    vault = Path(args.vault_root).expanduser() if args.vault_root else archive_root / DEFAULT_VAULT_NAME
    state_path = vault / ".state.json"
    old = load_state(state_path)
    old_files = old.get("files") if isinstance(old.get("files"), dict) else {}
    new_files: dict[str, Any] = {}
    sessions: dict[str, dict[str, Any]] = {}

    if not normalized.exists():
        raise SystemExit(f"normalized session directory not found: {normalized}")

    if args.rebuild and vault.exists():
        for name in ("chats", "prompts", "projects", "reports", "bottlenecks"):
            shutil.rmtree(vault / name, ignore_errors=True)
        old_files = {}

    parsed = 0
    reused = 0
    for path in sorted(normalized.glob("*.jsonl")):
        key = path.name
        fp = fingerprint(path)
        prior = old_files.get(key) if isinstance(old_files.get(key), dict) else None
        if prior and prior.get("fingerprint") == fp and isinstance(prior.get("meta"), dict):
            meta = prior["meta"]
            reused += 1
        else:
            meta = parse_session(path)
            write_session(vault, meta)
            parsed += 1
        sessions[meta["session_id"]] = meta
        new_files[key] = {"fingerprint": fp, "meta": meta}

    rebuild_indexes(vault, sessions)
    state = {
        "schema": "codex-curated-vault.v1",
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "archive_root": str(archive_root),
        "vault_root": str(vault),
        "files": new_files,
    }
    atomic_write(state_path, json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps({"status": "ok", "sessions": len(sessions), "parsed": parsed, "reused": reused, "vault_root": str(vault)}, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a compact, linked Markdown vault from normalized Codex sessions")
    parser.add_argument("--archive-root", default=str(DEFAULT_ARCHIVE_ROOT))
    parser.add_argument("--vault-root")
    parser.add_argument("--rebuild", action="store_true", help="Reparse all normalized sessions and regenerate derived notes")
    return parser


def main() -> int:
    return command_build(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
