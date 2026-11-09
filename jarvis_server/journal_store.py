#!/usr/bin/env python3
"""Persistent local storage for strategy execution journal entries."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


class JournalStore:
    MAX_ENTRIES = 5000

    def __init__(self, path: str | None = None) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        self.path = Path(path) if path else repo_root / ".jarvis_journal.json"
        self._lock = threading.Lock()
        self._entries = self._read()

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError(f"Journal file contains invalid JSON: {self.path}") from error
        if not isinstance(data, list) or any(not isinstance(entry, dict) for entry in data):
            raise ValueError(f"Journal file must contain a JSON array of objects: {self.path}")
        entries = data[-self.MAX_ENTRIES :]
        live_entries = [
            entry for entry in entries
            if entry.get("dry_run") is not True and entry.get("source") not in ("simulation", "paper")
        ]
        if live_entries != entries:
            self._write(live_entries)
        return live_entries

    def _write(self, entries: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_name(self.path.name + ".tmp")
        temporary_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        os.chmod(temporary_path, 0o600)
        temporary_path.replace(self.path)

    def entries(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            result = self._entries if limit is None else self._entries[-limit:]
            return [dict(entry) for entry in result]

    def append(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._entries.append(dict(entry))
            self._entries = self._entries[-self.MAX_ENTRIES :]
            self._write(self._entries)
