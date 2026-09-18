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
_FIRST_LINE_STATUS_RE = re.compile(
    rf"^({_STATUS_ALTERNATION})\b(?:\s*[:—–-]\s*.*)?$",
    re.I,
)
_REPORT_GATE_RE = re.compile(
    r"^(?:[-*]\s*)?(?:TEST(?:S)?|UNIT|SMOKE|DEPLOY|RUN(?:S)?|COMANDO\s+REALE|REAL\s+COMMAND|VERIFICA|VERIFICATION|CHECK(?:S)?)"
    r"\s*[:=]\s*[`*_~]*\s*(PASS|FAIL|BLOCKED|NOT_RUN|UNKNOWN)\b",
    re.I,
)
_REPORT_CLEAR_RE = re.compile(
    r"^(?:[-*]\s*)?(?:PROBLEMI\s+RESIDUI|RESIDUAL\s+ISSUES|BLOCKER)\s*[:=]\s*(?:NESSUNO|NONE)\b",
    re.I,
)


def _status_from_structured_report(text: str) -> str:
    pass_count = 0
    has_clear_residuals = False
    has_nonpass_gate = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        gate = _REPORT_GATE_RE.match(line)
        if gate:
            if gate.group(1).upper() == "PASS":
                pass_count += 1
            else:
                has_nonpass_gate = True
        if _REPORT_CLEAR_RE.match(line):
            has_clear_residuals = True
    if pass_count >= 2 and has_clear_residuals and not has_nonpass_gate:
        return "PASS"
    return "UNKNOWN"


def status_from_final(text: str) -> str:
    """Extract the terminal task status from a Codex final response."""
    normalized = (text or "").replace(r"\_", "_")
    match = _EXPLICIT_STATUS_RE.search(normalized)
    if match:
        return match.group(1).upper()

    first = next((line.strip() for line in normalized.splitlines() if line.strip()), "")
    first = first.strip("`*_~ ")
    match = _FIRST_LINE_STATUS_RE.match(first)
    if match:
        return match.group(1).upper()
    first = first.upper()
    if first in TERMINAL_STATUSES:
        return first
    return _status_from_structured_report(normalized)
