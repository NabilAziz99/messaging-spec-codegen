"""Outbox file I/O.

Iteration 5: append-on-queue, read-on-flush, atomic-rewrite for status
updates. Status update strategy is rewrite-via-temp-file + os.replace
(atomic on POSIX and Windows).
"""
from __future__ import annotations

import json
import os
from typing import Any


def outbox_path(name: str) -> str:
    """Per-user outbox path, relative to cwd."""
    return os.path.join(os.getcwd(), f"outbox-{name}.jsonl")


def append_pending(name: str, msg_id: str, to: str, text: str) -> None:
    """Append one `pending` row to the outbox file (used by /send while OFFLINE)."""
    row = {"id": msg_id, "to": to, "text": text, "status": "pending"}
    line = json.dumps(row, separators=(",", ":")) + "\n"
    with open(outbox_path(name), "a", encoding="utf-8") as f:
        f.write(line)


def read_all(name: str) -> list[dict[str, Any]]:
    """Read all rows in file order. Returns [] if the file doesn't exist."""
    path = outbox_path(name)
    if not os.path.exists(path):
        return []
    rows: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                # Iteration 5: ignore malformed rows. (Robust corruption
                # handling is FUTURE.md.)
                continue
    return rows


def rewrite_all(name: str, rows: list[dict[str, Any]]) -> None:
    """Atomic rewrite: write to .tmp, then rename over the original.

    Used to update row statuses (pending → sent) during outbox flush.
    """
    path = outbox_path(name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    os.replace(tmp, path)
