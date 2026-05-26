"""Server state + per-frame handlers.

State is module-level (single-process server). Handlers mutate it
directly. The dispatch glue lives in server.py.
"""
from __future__ import annotations

import logging

from protocol import (
    CLOSE_SUPERSEDED,
    NAME_PATTERN,
    send_frame,
)

logger = logging.getLogger("server")


# ── State (single-process, in-memory) ──────────────────────────────────

# Currently-connected users. Maps name → websocket.
connections: dict[str, object] = {}


# ── Handlers ───────────────────────────────────────────────────────────

async def handle_login(ws, frame: dict) -> str:
    """Process a login frame. Returns the registered name."""
    name = frame["name"]
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise ValueError(f"login: invalid name {name!r}")

    # Kick any existing connection for this name.
    existing = connections.get(name)
    if existing is not None and existing is not ws:
        logger.info("supersede: kicking previous session for %r", name)
        try:
            await existing.close(code=CLOSE_SUPERSEDED, reason="superseded")
        except Exception:
            pass

    connections[name] = ws
    await send_frame(ws, {"type": "login_ok", "name": name})
    logger.info("login ok: %r", name)
    return name


def on_disconnect(name: str | None, ws) -> None:
    """Called from server.py when a connection closes. Removes from
    `connections` if this socket still owns the slot."""
    if name is not None and connections.get(name) is ws:
        del connections[name]
        logger.info("disconnect: %r", name)
