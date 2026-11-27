"""Reuse the event text in an existing export only with matching local provenance."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import unicodedata


def rules_digest() -> str:
    package = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    digest.update(repr((sys.version, unicodedata.unidata_version)).encode())
    for source in sorted(package.rglob("*.py")):
        digest.update(os.fsencode(source.relative_to(package)) + b"\0")
        digest.update(hashlib.sha256(source.read_bytes()).digest())
    return digest.hexdigest()


class VerifiedTranscripts:
    def __init__(self, repo: Path, generation: str):
        self.repo = repo
        self.generation = generation
        self.parsed = 0
        self.reused = 0
        self.repair_export = False
        self.pending = {}

    def snapshot_generation(self) -> str:
        # An edited export must also wake a publisher with unchanged native inputs.
        digest = hashlib.sha256(self.generation.encode())
        root = self.repo / "chats"
        if root.is_dir():
            for chat in sorted(root.iterdir()):
                if not chat.name.isascii() or not chat.name.isdecimal():
                    continue
                try:
                    info = (chat / "transcript.jsonl").lstat()
                except FileNotFoundError:
                    continue
                digest.update(repr((chat.name, info.st_dev, info.st_ino,
                                    info.st_size, info.st_ctime_ns)).encode())
        return digest.hexdigest()

    def load(self, con: sqlite3.Connection, path: Path, source_hash: str) -> list[str] | None:
        self.parsed += 1
        row = con.execute("""SELECT generation, source_hash, chat_id, export_size, export_hash
            FROM verified_transcripts WHERE source_path=?""", (str(path),)).fetchone()
        if row is None or row["source_hash"] != source_hash:
            return None
        if row["generation"] != self.generation:
            self.repair_export = True
            return None

        chat_id = row["chat_id"]
        if type(chat_id) is not int or chat_id < 1:
            return None
        try:
            data = self.read_matching(chat_id, row["export_size"], row["export_hash"])
            if data is None:
                self.repair_export = True
                return None
            # Only hashes of this parser's own canonical JSONL can reach this point.
            texts = [json.loads(line)["content_text"] for line in data.split(b"\n") if line]
        except (OSError, ValueError, KeyError, TypeError):
            self.repair_export = True
            return None
        self.reused += 1
        return texts

    def read_matching(self, chat_id: int, size: int, expected: str) -> bytes | None:
        export = self.repo / "chats" / str(chat_id) / "transcript.jsonl"

        def open_regular(name, flags):
            return os.open(name, flags | os.O_NONBLOCK | os.O_NOFOLLOW)

        try:
            with open(export, "rb", opener=open_regular) as handle:
                info = os.fstat(handle.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                        or info.st_size != size):
                    return None
                data = handle.read(size + 1)
        except OSError:
            return None
        return data if hashlib.sha256(data).hexdigest() == expected else None

    def remember(self, path: Path, source_hash: str, chat_id: int, data: bytes) -> None:
        self.pending[str(path)] = (source_hash, chat_id, len(data), hashlib.sha256(data).hexdigest())

    def confirm(self, con: sqlite3.Connection) -> None:
        # Incomplete turns may deliberately stay unpublished. Only retain a receipt
        # for a result that was actually available in the ordinary export.
        for path, (source_hash, chat_id, size, expected) in self.pending.items():
            if self.read_matching(chat_id, size, expected) is not None:
                con.execute("""INSERT OR REPLACE INTO verified_transcripts
                    (source_path, generation, source_hash, chat_id, export_size, export_hash)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (path, self.generation, source_hash, chat_id, size, expected))
        self.pending.clear()
