"""Wire-protocol validation for the server.

Constants and frame-parsing helpers. Mirrors the client's protocol.py
on the other side — kept as a separate file (intentional duplication)
because the server is a separate process.
"""
from __future__ import annotations

import json
import re

# Validation
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

# Server endpoint
WS_PATH = "/ws"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# WebSocket close codes
CLOSE_SUPERSEDED = 4000
CLOSE_INVALID_FRAME = 4001

# Required key set per inbound frame type (exact match — extras are rejected).
EXPECTED_KEYS = {
    "login":      {"type", "name"},
    "send":       {"type", "id", "to", "text"},
    "deliver_ok": {"type", "id"},
}


def parse_frame(raw: str) -> dict:
    """Parse + shape-validate one inbound frame. Raise ValueError with
    a spec-pinned reason string on any violation."""
    try:
        frame = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("invalid json")
    if not isinstance(frame, dict):
        raise ValueError("invalid frame")
    frame_type = frame.get("type")
    if not isinstance(frame_type, str):
        raise ValueError("invalid frame")
    expected = EXPECTED_KEYS.get(frame_type)
    if expected is None:
        raise ValueError("invalid frame")
    if set(frame.keys()) != expected:
        raise ValueError("invalid frame")
    return frame


async def send_frame(ws, frame: dict) -> None:
    """Compact-JSON send."""
    await ws.send(json.dumps(frame, separators=(",", ":")))
