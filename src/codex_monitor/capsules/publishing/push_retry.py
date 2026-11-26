"""Retry an interrupted private publication before producing the next commit."""
import json
import re
import subprocess
import time

from . import legacy


def _receipt(state):
    return state / "push-retry.json"


def check_backoff(state, repo):
    path = _receipt(state)
    try:
        receipt = json.loads(path.read_text())
    except FileNotFoundError:
        return 0
    rejected = receipt.get("rejected_tip")
    if rejected:
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", rejected):
            raise legacy.PublisherError("Invalid credential quarantine receipt")
        history = legacy.run(["git", "merge-base", "--is-ancestor", rejected, "refs/heads/main"], repo)
        if history.returncode != 1:
            raise legacy.PublisherError("Publication quarantined by the credential check. "
                                        "Review the unpublished history before retrying.")
        # Replacing the rejected history releases the hold; the ordinary
        # pre-push hook must still inspect the replacement in full.
        return 0
    remaining = receipt["retry_after"] - time.time()
    if remaining > 0:
        raise legacy.PublisherError(f"Git publication pending; next retry in {int(remaining) + 1}s")
    return receipt["attempts"]


def push(repo, state, git_ok):
    attempts = check_backoff(state, repo) + 1
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
        rejection = re.search(r"Gitleaks ha fermato il push\. possibile credenziale nei commit verso ([0-9a-f]{12})\.", str(exc))
        if rejection:
            head = legacy.run(["git", "rev-parse", "--verify", "refs/heads/main"], repo)
            if head.returncode == 0 and head.stdout.strip().startswith(rejection[1]):
                legacy.atomic_write(path, json.dumps({"attempts": attempts, "retry_after": 0,
                                                      "rejected_tip": head.stdout.strip()}))
                raise legacy.PublisherError("Credential check rejected the outgoing history; publication quarantined") from exc
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
