#!/usr/bin/env bash
set -euo pipefail

root="${1:-.}"
matches="$(
  grep -RInE \
    --exclude-dir=.git \
    --exclude-dir=.venv \
    --exclude='*.pyc' \
    --exclude='*.db' \
    --exclude='*.sqlite' \
    --exclude='*.sqlite3' \
    --exclude='*.db-wal' \
    --exclude='*.db-shm' \
    --exclude='*.sqlite-wal' \
    --exclude='*.sqlite-shm' \
    '(sk-[A-Za-z0-9_-]{20,}|[0-9]{6,}:[A-Za-z0-9_-]{20,}|gh[opsu]_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|AKIA[0-9A-Z]{16})' \
    "$root" || true
)"

if [[ -n "$matches" ]]; then
  printf '%s\n' "$matches"
  exit 1
fi

printf 'secret_scan=0\n'
