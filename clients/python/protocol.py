"""Wire-protocol helpers.

Pure functions and constants — no I/O, no state. Used by connection.py
and client.py. Each iteration may add new frame helpers here as the
spec defines new frame types.
"""
from __future__ import annotations

import json
import re
from typing import Any

# Validation
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

# WebSocket close codes (server → client)
CLOSE_NORMAL = 1000
CLOSE_SUPERSEDED = 4000

# Default endpoint
DEFAULT_URL = "ws://127.0.0.1:8765/ws"

# Help text — kept here so the format is one place. There are NO
# `/offline` or `/online` commands; the client transitions on real
# WebSocket events (the world of unreliable networks).
HELP_TEXT = (
    "/login <name>              identify yourself\n"
    "/send <recipient> <text>   send a message (queues if offline)\n"
    "/help                      show this list\n"
    "/quit                      exit"
)


def encode_frame(obj: dict[str, Any]) -> str:
    """Compact JSON encoding — matches the server's wire output."""
    return json.dumps(obj, separators=(",", ":"))


def is_valid_name(s: str) -> bool:
    return isinstance(s, str) and bool(NAME_RE.match(s))
