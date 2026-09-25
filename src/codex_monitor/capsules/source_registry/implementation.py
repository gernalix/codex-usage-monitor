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
