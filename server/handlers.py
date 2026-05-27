"""Server state + per-frame handlers.

State is module-level (single-process server). Handlers mutate it
directly. The dispatch glue lives in server.py.
"""
from __future__ import annotations

import logging
import time

from websockets.exceptions import ConnectionClosed

from protocol import (
    CLOSE_SUPERSEDED,
    NAME_PATTERN,
    UUID_PATTERN,
    send_frame,
)

logger = logging.getLogger("server")


# ── State (single-process, in-memory) ──────────────────────────────────

# Currently-connected users. Maps name → websocket.
connections: dict[str, object] = {}

# Per-user pending messages awaiting delivery. Each entry is the body
# of a `deliver` frame: {id, from, to, text, server_ts}. Messages stay
# in the inbox until a matching `deliver_ok` removes them.
inbox: dict[str, list[dict]] = {}

# Per-sender set of message ids the server has already accepted. Used
# to dedup retried `send` frames (Iteration 8). Dedup is on (sender, id)
# alone — same id with different to/text is still a duplicate.
seen_ids: dict[str, set[str]] = {}


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

    # Flush pending inbox (FIFO). Messages stay in the inbox until
    # deliver_ok removes them (at-least-once delivery).
    pending = inbox.get(name, [])
    for msg in list(pending):
        await send_frame(ws, {"type": "deliver", **msg})
    if pending:
        logger.info("flushed %d pending to %r", len(pending), name)

    return name


async def handle_send(ws, frame: dict, sender: str) -> None:
    """Validate, append to recipient inbox, optionally push, ack."""
    msg_id = frame["id"]
    to = frame["to"]
    text = frame["text"]

    if not isinstance(msg_id, str) or not UUID_PATTERN.match(msg_id):
        raise ValueError(f"send: invalid id {msg_id!r}")
    if not isinstance(to, str) or not NAME_PATTERN.match(to):
        raise ValueError(f"send: invalid recipient {to!r}")
    if not isinstance(text, str):
        raise ValueError("send: text must be a string")

    # Iteration 8: dedup on (sender, id). A duplicate still gets send_ok
    # (so the client can mark its outbox row sent) but is NOT enqueued
    # again — at-most-once delivery from any sender.
    seen = seen_ids.setdefault(sender, set())
    if msg_id in seen:
        logger.info("dedup: %s from %r (already seen)", msg_id, sender)
        await send_frame(ws, {"type": "send_ok", "id": msg_id})
        return

    msg = {
        "id":        msg_id,
        "from":      sender,
        "to":        to,
        "text":      text,
        "server_ts": int(time.time()),
    }

    # Append to recipient's inbox (canonical "accepted" step).
    inbox.setdefault(to, []).append(msg)
    seen.add(msg_id)

    # If recipient is online, also push immediately. Message stays in
    # the inbox until deliver_ok removes it.
    recipient_ws = connections.get(to)
    if recipient_ws is not None:
        try:
            await send_frame(recipient_ws, {"type": "deliver", **msg})
        except ConnectionClosed:
            logger.info("push to %r failed; message stays in inbox", to)

    await send_frame(ws, {"type": "send_ok", "id": msg_id})


async def handle_deliver_ok(frame: dict, recipient: str) -> None:
    """Remove the matching id from recipient's inbox. Silently ignore
    spurious / already-removed ids."""
    msg_id = frame["id"]
    if not isinstance(msg_id, str):
        return
    pending = inbox.get(recipient)
    if not pending:
        return
    inbox[recipient] = [m for m in pending if m["id"] != msg_id]


def on_disconnect(name: str | None, ws) -> None:
    """Called from server.py when a connection closes. Removes from
    `connections` if this socket still owns the slot; leaves inbox
    intact so un-acked messages re-deliver on next login."""
    if name is not None and connections.get(name) is ws:
        del connections[name]
        logger.info("disconnect: %r", name)
