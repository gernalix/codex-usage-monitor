from __future__ import annotations

import re


TERMINAL_STATUSES = {
    "PASS",
    "FIXED",
    "BLOCKED",
    "FAIL",
    "PARTIAL",
    "WAITING_FOR_EVENT",
    "BLOCKED_REPO_PUBLIC",
}
_STATUS_ALTERNATION = "|".join(sorted(TERMINAL_STATUSES, key=len, reverse=True))
_EXPLICIT_STATUS_RE = re.compile(
    rf"\b(?:RESULT|STATUS|VERIFICA|VERIFICATION|ESITO)\s*[:=]\s*[`*_~]*\s*(?:(?:goal\s+marcato|goal\s+marked|status)\s+)?({_STATUS_ALTERNATION})\b",
    re.I,
)


def status_from_final(text: str) -> str:
    """Extract the terminal task status from a Codex final response."""
    normalized = (text or "").replace(r"\_", "_")
    match = _EXPLICIT_STATUS_RE.search(normalized)
    if match:
        return match.group(1).upper()

    first = next((line.strip() for line in normalized.splitlines() if line.strip()), "")
    first = first.strip("`*_~ ")
    first = first.upper()
    return first if first in TERMINAL_STATUSES else "UNKNOWN"
