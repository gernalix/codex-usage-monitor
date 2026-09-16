"""Public publishing capsule API."""

from . import base as _base
from . import implementation as _implementation
from . import legacy
from .base import (
    ATTACHMENTS_ROOT, FINGERPRINT_SCHEMA, PublisherError,
    _apply_patch_write_paths_from_event, _exec_write_paths_from_event,
    _full_fingerprint, _guard_candidate_cycles, _legacy_send_batch_telegram,
    _notify_cycle_objects, _repo_root, _should_export, chat_metrics,
    classify_git_repo, connect_state, prompt_id_from_text,
    record_git_completion_guard, run,
)
from .implementation import (
    _recover_completed_goal_aborts, command_run, export_repo, main, parse_session,
)
from .status import status_from_final
from .base import subprocess

# Keep the existing parser pipeline intact while making terminal-status parsing
# canonical for both base and implementation entrypoints.
_base.status_from_final = status_from_final
_implementation.status_from_final = status_from_final

__all__ = [
    "ATTACHMENTS_ROOT", "FINGERPRINT_SCHEMA", "PublisherError",
    "_apply_patch_write_paths_from_event", "_exec_write_paths_from_event",
    "_full_fingerprint", "_guard_candidate_cycles", "_legacy_send_batch_telegram",
    "_notify_cycle_objects", "_recover_completed_goal_aborts", "_repo_root",
    "_should_export", "chat_metrics", "classify_git_repo", "command_run",
    "connect_state", "export_repo", "legacy", "main", "parse_session",
    "prompt_id_from_text", "record_git_completion_guard", "run", "status_from_final",
    "subprocess",
]
