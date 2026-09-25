from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import time
from typing import Any


@dataclass(frozen=True)
class SourceSpec:
    key: str
    owner: str
    category: str
    path: str
    access: str
    integration: str
    default_enabled: bool
    personal_content: bool
    rationale: str


def _home(path: str) -> str:
    return str(Path(path).expanduser())


SOURCE_REGISTRY = (
    SourceSpec(
        "roadmap", "codex-roadmap", "canonical_lifecycle",
        _home("~/projects/codex-roadmap/roadmap.sqlite"),
        "mutations_via_single_writer", "direct_read",
        True, False, "Canonical task lifecycle, dependencies, queue and execution metadata.",
    ),
    SourceSpec(
        "roadmap_checkpoints", "codex-roadmap", "operational_checkpoints",
        _home("~/projects/codex-roadmap/operations/task-state"),
        "read_only", "direct_read",
        True, False, "Durable task checklists and Next action handoffs.",
    ),
    SourceSpec(
        "prompt_history", "prompt-history", "derived_conversation_evidence",
        _home("~/.local/share/prompt-history/prompt_history.sqlite"),
        "read_only", "direct_read",
        True, False, "Normalized rebuildable ChatGPT/Codex evidence warehouse.",
    ),
    SourceSpec(
        "codex_usage", "codex-usage", "derived_execution_telemetry",
        _home("~/projects/codex-usage"),
        "read_only", "direct_read",
        True, False, "Published costs, prompt cycles, native-session indexes and quota telemetry.",
    ),
    SourceSpec(
        "codex_session_archive", "C2", "native_session_evidence",
        _home("~/.local/share/codex-session-archive"),
        "read_only", "direct_read",
        True, False, "Local exact/normalized Codex session evidence owned by C2.",
    ),
    SourceSpec(
        "switcher_state", "chrome-codex-switcher", "ui_context",
        _home("~/.local/state/chrome-codex-switcher/state.sqlite3"),
        "read_only", "direct_read",
        True, False, "Prompt/tab associations useful for orchestration context.",
    ),
    SourceSpec(
        "github_autosync", "github-autosync", "control_dependency",
        _home("~/projects/github-autosync"),
        "delegate_only", "helper_calls",
        True, False, "Per-repository single-writer and integration mechanics stay externally owned.",
    ),
    SourceSpec(
        "chatgpt_exporter", "siraht/ChatGPTExporter", "producer_archive",
        _home("~/Documents/ChatGPT"),
        "read_only", "via_prompt_history",
        False, True, "C2 consumes normalized ChatGPT history through prompt-history, not directly.",
    ),
    SourceSpec(
        "fedora_system_monitor", "fedora-system-monitor", "system_health",
        _home("~/projects/fedora-system-monitor"),
        "read_only", "task_scoped",
        False, False, "Use for diagnostics/health tasks; do not duplicate its telemetry into C2.",
    ),
    SourceSpec(
        "chatgpt_rdc_supervisor", "chatgpt-rdc-supervisor", "control_dependency",
        _home("~/projects/chatgpt-rdc-supervisor"),
        "delegate_only", "task_scoped",
        False, False, "May supervise ChatGPT browser workers; not a canonical task/data store.",
    ),
    SourceSpec(
        "personalhub", "PersonalHub", "personal_application_data",
        _home("~/projects/PersonalHub"),
        "excluded", "explicit_task_only",
        False, True, "Application data is unrelated to generic C2 orchestration unless explicitly requested.",
    ),
    SourceSpec(
        "whatsapp_exporter", "whatsapp-exporter", "personal_messages",
        _home("~/projects/whatsapp-exporter"),
        "excluded", "explicit_task_only",
        False, True, "Private message archives must not enter C2 by default.",
    ),
    SourceSpec(
        "telegram_history", "telegram-notification-history", "personal_messages",
        _home("~/projects/telegram-notification-history"),
        "excluded", "explicit_task_only",
        False, True, "Telegram archives are task-scoped personal content, not orchestration context.",
    ),
    SourceSpec(
        "discord_exporter", "discord-exporter", "personal_messages",
        _home("~/projects/discord-exporter"),
        "excluded", "explicit_task_only",
        False, True, "Discord archives are excluded from automatic C2 ingestion.",
    ),
    SourceSpec(
        "grindr_exporter", "grindr-web-exporter", "personal_messages",
        _home("~/projects/grindr-web-exporter"),
        "excluded", "explicit_task_only",
        False, True, "Grindr archives are excluded from automatic C2 ingestion.",
    ),
)


def source_specs(*, default_only: bool = False) -> list[SourceSpec]:
    items = SOURCE_REGISTRY
    if default_only:
        items = tuple(item for item in items if item.default_enabled)
    return list(items)
def _status(spec: SourceSpec) -> dict[str, Any]:
    path = Path(spec.path)
    payload = asdict(spec)
    payload["exists"] = path.exists()
    payload["kind"] = "directory" if path.is_dir() else "file" if path.is_file() else "missing"
    try:
        stat = path.stat()
    except OSError:
        payload["age_seconds"] = None
    else:
        payload["age_seconds"] = max(0.0, time.time() - stat.st_mtime)
    return payload


def registry_status(*, default_only: bool = False) -> list[dict[str, Any]]:
    return [_status(item) for item in source_specs(default_only=default_only)]


def sharing_policy() -> dict[str, list[str]]:
    return {
        "default_direct": [
            item.key for item in SOURCE_REGISTRY
            if item.default_enabled and item.integration == "direct_read"
        ],
        "default_delegate": [
            item.key for item in SOURCE_REGISTRY
            if item.default_enabled and item.integration == "helper_calls"
        ],
        "via_derived_store": [
            item.key for item in SOURCE_REGISTRY
            if item.integration == "via_prompt_history"
        ],
        "explicit_task_only": [
            item.key for item in SOURCE_REGISTRY
            if item.integration == "explicit_task_only"
        ],
    }
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect C2 data/control source policy")
    parser.add_argument("command", choices=("list", "status", "policy"), nargs="?", default="status")
    parser.add_argument("--default-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "list":
        payload: Any = [asdict(item) for item in source_specs(default_only=args.default_only)]
    elif args.command == "status":
        payload = registry_status(default_only=args.default_only)
    else:
        payload = sharing_policy()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
