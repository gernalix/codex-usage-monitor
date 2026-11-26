"""Retry an interrupted private publication before producing the next commit."""
import json
import subprocess
import time

from . import legacy


def _receipt(state):
    return state / "push-retry.json"


def check_backoff(state):
    path = _receipt(state)
    try:
        receipt = json.loads(path.read_text())
    except FileNotFoundError:
        return 0
    remaining = receipt["retry_after"] - time.time()
    if remaining > 0:
        raise legacy.PublisherError(f"Git publication pending; next retry in {int(remaining) + 1}s")
    return receipt["attempts"]


def push(repo, state, git_ok):
    attempts = check_backoff(state) + 1
    delay = min(21600, 300 * 2 ** min(attempts - 1, 7))
    path = _receipt(state)
    def record(delay_seconds):
        legacy.atomic_write(path, json.dumps({"attempts": attempts, "retry_after": time.time() + delay_seconds}))
    # A timer or power interruption can terminate the process before Python
    # handles the error. Keep the outstanding transaction across that exit.
    record(delay)
    try:
        git_ok(["git", "push", "origin", "main"], repo, timeout=1200)
    except (legacy.PublisherError, OSError, subprocess.TimeoutExpired) as exc:
        record(delay)
        raise legacy.PublisherError(f"Git publication failed; retry in {delay}s ({type(exc).__name__}): {exc}") from exc
    path.unlink()


def pending(repo, state, run):
    if _receipt(state).exists():
        return True
    if not (repo / ".git").exists():
        return False
    # Also recover commits left by older deployed versions, before their
    # unchanged-input fast path can mark them as published.
    ahead = run(["git", "rev-list", "--count", "refs/remotes/origin/main..refs/heads/main"], repo)
    return ahead.returncode == 0 and int(ahead.stdout.strip()) > 0
