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


# Increment whenever derived published fields can change for an otherwise unchanged
# native rollout (for example terminal-status or prompt-identity parsing). Including
# this version in the stable cycle fingerprint makes an existing published cycle
# flow through the normal pending/publish transaction exactly once. Unlike the old
# fingerprint-schema migration shortcut, state is not advanced before Git publish
# succeeds, so a failed backfill remains retryable.
PUBLICATION_SEMANTICS_VERSION = 6
_base.SOURCE_SCAN_GENERATION = (
    f"publication-semantics-v{PUBLICATION_SEMANTICS_VERSION}:"
    f"fingerprint-schema-{FINGERPRINT_SCHEMA}"
)
_RAW_CYCLE_FINGERPRINT = _base._fingerprint


def _publication_fingerprint(cycle):
    raw = _RAW_CYCLE_FINGERPRINT(cycle)
    return legacy.digest_text(
        f"publication-semantics-v{PUBLICATION_SEMANTICS_VERSION}:{raw}"
    )


# Keep the existing parser pipeline intact while making terminal-status parsing
# canonical for both base and implementation entrypoints. The semantic fingerprint
# is patched at the same public composition point so both parser layers calculate
# the same persisted fingerprint.
_base._fingerprint = _publication_fingerprint
_base.status_from_final = status_from_final
_implementation.status_from_final = status_from_final

__all__ = [
    "ATTACHMENTS_ROOT", "FINGERPRINT_SCHEMA", "PUBLICATION_SEMANTICS_VERSION",
    "PublisherError", "_apply_patch_write_paths_from_event",
    "_exec_write_paths_from_event", "_full_fingerprint", "_guard_candidate_cycles",
    "_legacy_send_batch_telegram", "_notify_cycle_objects",
    "_publication_fingerprint", "_recover_completed_goal_aborts", "_repo_root",
    "_should_export", "chat_metrics", "classify_git_repo", "command_run",
    "connect_state", "export_repo", "legacy", "main", "parse_session",
    "prompt_id_from_text", "record_git_completion_guard", "run", "status_from_final",
    "subprocess",
]
