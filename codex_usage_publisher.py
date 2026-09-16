#!/usr/bin/env python3
"""Compatibility adapter for the publishing capsule."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from codex_monitor.capsules.publishing.api import (
    ATTACHMENTS_ROOT, FINGERPRINT_SCHEMA, PublisherError,
    _apply_patch_write_paths_from_event, _exec_write_paths_from_event,
    _full_fingerprint, _guard_candidate_cycles, _legacy_send_batch_telegram,
    _notify_cycle_objects, _recover_completed_goal_aborts, _repo_root,
    _should_export, chat_metrics, classify_git_repo, command_run, connect_state,
    export_repo, legacy, main, parse_session, prompt_id_from_text,
    record_git_completion_guard, run, status_from_final, subprocess,
)
if __name__ == "__main__":
    raise SystemExit(main())
