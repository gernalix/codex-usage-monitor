#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from codex_monitor.credentials import credential_path


DEFAULT_ENV = "CODEX_USAGE_KUMA_PUSH_URL"
DEFAULT_TIMEOUT_SEC = 10.0


class KumaPushError(RuntimeError):
    pass


def build_push_url(base_url: str, *, status: str = "up", message: str = "OK", ping_ms: float | None = None) -> str:
    raw = (base_url or "").strip()
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or "/api/push/" not in parsed.path:
        raise KumaPushError("invalid Uptime Kuma push URL")
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query["status"] = status
    query["msg"] = message[:250]
    if ping_ms is None:
        query.pop("ping", None)
    else:
        query["ping"] = f"{max(0.0, float(ping_ms)):g}"
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment)
    )


def push(url: str, *, message: str, timeout_sec: float = DEFAULT_TIMEOUT_SEC, ping_ms: float | None = None) -> None:
    request_url = build_push_url(url, status="up", message=message, ping_ms=ping_ms)
    request = urllib.request.Request(
        request_url,
        headers={"User-Agent": "codex-usage-monitor/kuma-heartbeat"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            body = response.read(4096)
            if not 200 <= response.status < 300:
                raise KumaPushError(f"Uptime Kuma returned HTTP {response.status}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise KumaPushError(f"Uptime Kuma push failed: {type(exc).__name__}") from exc

    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise KumaPushError("Uptime Kuma returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise KumaPushError("Uptime Kuma rejected the heartbeat")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send one success heartbeat to an Uptime Kuma Push monitor")
    parser.add_argument("--env", default=DEFAULT_ENV, help="environment variable containing the secret Push URL")
    parser.add_argument("--message", default="codex-usage-monitor cycle ok")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument("--strict", action="store_true", help="fail when the URL is missing or the push cannot be delivered")
    args = parser.parse_args(argv)

    url = os.getenv(args.env, "").strip()
    if not url:
        legacy_env = Path(os.getenv("CODEX_USAGE_ENV_FILE", str(Path.home() / ".config/codex-usage-monitor/codex-usage-monitor.env")))
        env_file = credential_path("codex-usage-monitor.env", legacy_env)
        if env_file.is_file():
            for raw in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() == args.env:
                    url = value.strip().strip('"').strip("'")
                    break
    if not url:
        if args.strict:
            print(f"{args.env} is not configured", file=sys.stderr)
            return 2
        return 0

    try:
        push(url, message=args.message, timeout_sec=max(1.0, args.timeout))
    except KumaPushError as exc:
        print(str(exc), file=sys.stderr)
        return 1 if args.strict else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
