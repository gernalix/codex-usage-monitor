"""Pure quota-state formatting and deduplication policy.

No network, filesystem, database writes or Telegram dependencies.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
from typing import Any
from zoneinfo import ZoneInfo

DISPLAY_TZ = ZoneInfo("Europe/Copenhagen")


def normalize_number(value: Any) -> str:
    if value is None:
        return "unavailable"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value).strip()
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def fmt_value(value: Any, suffix: str = "") -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, float) and value.is_integer():
        return f"{int(value)}{suffix}"
    return f"{value}{suffix}"


def format_display_datetime(value: Any) -> str:
    if not value:
        return "unavailable"
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(DISPLAY_TZ).strftime("%d-%m-%y %H:%M")


def quota_notification_state(row: sqlite3.Row) -> dict[str, str]:
    return {
        "weekly_remaining": normalize_number(row["weekly_remaining_percent"]),
        "weekly_reset": format_display_datetime(row["weekly_reset_at_utc"]),
        "usage_limit_resets_available": normalize_number(row["usage_limit_resets_available"]),
    }


def quota_state_event_key(state: dict[str, str]) -> str:
    payload = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return f"quota_change:v2:{hashlib.sha256(payload.encode()).hexdigest()}"


def quota_state_from_message(message: str) -> dict[str, str] | None:
    values: dict[str, str] = {}
    for line in message.splitlines():
        if line.startswith("Weekly remaining: "):
            raw = line.removeprefix("Weekly remaining: ")
            if "->" in raw:
                raw = raw.split("->", 1)[1].split("%", 1)[0].strip()
            values["weekly_remaining"] = normalize_number(raw.removesuffix("%"))
        elif line.startswith("Weekly reset: "):
            values["weekly_reset"] = line.removeprefix("Weekly reset: ").strip()
        elif line.startswith("Usage limit resets available: "):
            values["usage_limit_resets_available"] = normalize_number(
                line.removeprefix("Usage limit resets available: ").strip()
            )
    required = {"weekly_remaining", "weekly_reset", "usage_limit_resets_available"}
    return values if required <= values.keys() else None


def quota_change_is_noop(previous_state: dict[str, str], current_state: dict[str, str]) -> bool:
    try:
        previous_remaining = float(previous_state["weekly_remaining"])
        current_remaining = float(current_state["weekly_remaining"])
    except (KeyError, TypeError, ValueError):
        return False
    return previous_remaining == 100.0 and current_remaining == 100.0
