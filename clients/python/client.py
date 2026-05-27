#!/usr/bin/env python3
"""Messaging CLI client (Python) — entry point + commands + state.

State is driven by network events, not user commands. There is no
`/offline` / `/online` — the WebSocket dies, we drop to OFFLINE
silently from the user's POV (printing `disconnected`) and a
background task auto-reconnects with backoff. When `login_ok` comes
back we print `reconnected` and flush the outbox.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import uuid
from typing import Any

from protocol import HELP_TEXT, DEFAULT_URL, encode_frame, is_valid_name
from connection import Connection
from outbox import append_pending, read_all, rewrite_all


# Auto-reconnect backoff schedule (seconds). After the list is
# exhausted, every subsequent attempt waits `BACKOFF_CAP`. The spec
# pins this so test timings are deterministic.
BACKOFF_SCHEDULE = [1.0, 2.0, 5.0]
BACKOFF_CAP = 10.0


def out(line: str) -> None:
    print(line)


def _backoff_for_attempt(attempt: int) -> float:
    if attempt < len(BACKOFF_SCHEDULE):
        return BACKOFF_SCHEDULE[attempt]
    return BACKOFF_CAP


class Client:
    """State machine + command dispatcher. Delegates WebSocket I/O to
    a `Connection`. The auto-reconnect loop is owned here (not in
    Connection) because reconnect state — name, status lines, outbox
    flush — is a client-layer concern."""

    def __init__(self) -> None:
        self.state: str = "INITIAL"   # INITIAL | LOGGED_IN | OFFLINE
        self.name: str | None = None
        url = os.environ.get("MSG_SERVER", DEFAULT_URL)
        self.conn = Connection(
            url=url,
            on_deliver=self._on_deliver,
            on_disconnect=self._on_disconnect,
        )
        self._reconnect_task: asyncio.Task[None] | None = None

    # ── Connection callbacks ────────────────────────────────────────

    async def _on_deliver(self, frame: dict[str, Any]) -> None:
        try:
            line = f"[{frame['from']} → {frame['to']}]  {frame['text']}"
        except KeyError:
            return
        out(line)
        await self.conn.send({"type": "deliver_ok", "id": frame["id"]})

    def _on_disconnect(self, superseded: bool) -> None:
        """Reader-loop callback: the WS just closed unexpectedly.

        - 4000 (superseded) → terminal: clear name, go INITIAL, stop.
        - any other close while LOGGED_IN → transient: go OFFLINE,
          spawn the reconnect loop.
        - close while already OFFLINE or INITIAL → ignore (reconnect
          loop's own attempts close the WS as they retry).
        """
        if self.state != "LOGGED_IN":
            return
        if superseded:
            out("disconnected: superseded by another session")
            self.state = "INITIAL"
            self.name = None
            return
        out("disconnected")
        self.state = "OFFLINE"
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    # ── Auto-reconnect loop ─────────────────────────────────────────

    async def _reconnect_loop(self) -> None:
        """Background task: retry connect+login forever (or until 4000).

        The spec's invariants this loop enforces:
        - Backoff schedule: 1s, 2s, 5s, then 10s capped.
        - Cached name persists across the loop (we re-login as it).
        - On 4000 mid-attempt → terminal: print supersede line, go
          INITIAL, clear name, exit the loop.
        - On success → print `reconnected`, flush outbox, exit the loop.
        """
        attempt = 0
        cached = self.name
        if cached is None:
            return  # nothing to reconnect as
        while self.state == "OFFLINE":
            await asyncio.sleep(_backoff_for_attempt(attempt))
            attempt += 1
            if self.state != "OFFLINE":
                return  # user /quit, or some other path changed state
            result = await self.conn.connect_and_login(cached)
            if result == "ok":
                self.state = "LOGGED_IN"
                out("reconnected")
                await self._flush_outbox()
                return
            if result == "superseded":
                out("disconnected: superseded by another session")
                self.state = "INITIAL"
                self.name = None
                return
            # "unreachable" → try again after the next backoff

    # ── Commands ────────────────────────────────────────────────────

    async def cmd_login(self, args: list[str]) -> None:
        if self.state == "LOGGED_IN" or self.state == "OFFLINE":
            out(f"error: already logged in as {self.name}")
            return
        if len(args) != 1 or not is_valid_name(args[0]):
            out("error: usage: /login <name>")
            return
        name = args[0]
        result = await self.conn.connect_and_login(name)
        if result != "ok":
            # "unreachable" or "superseded" — both surface as the same
            # user-facing error on a user-initiated /login: we never
            # claimed the name.
            out("error: cannot reach server")
            return
        self.name = name
        self.state = "LOGGED_IN"
        out(f"logged in as {name}")
        # Spec: outbox flushes on every LOGGED_IN entry. Covers the
        # killed-and-relaunched case where pending rows are on disk.
        await self._flush_outbox()

    async def cmd_send(self, line_after_verb: str) -> None:
        if self.state == "INITIAL":
            out("error: not logged in; use /login <name> first")
            return
        # Parse: first whitespace separates recipient from text.
        if not line_after_verb.startswith(" "):
            out("error: usage: /send <recipient> <text>")
            return
        rest = line_after_verb[1:]
        m = re.search(r"\s", rest)
        if m is None:
            out("error: usage: /send <recipient> <text>")
            return
        recipient = rest[: m.start()]
        text = rest[m.end():]
        if not recipient or not text or not is_valid_name(recipient):
            out("error: usage: /send <recipient> <text>")
            return

        msg_id = str(uuid.uuid4())

        if self.state == "OFFLINE":
            # Queue to disk; don't transmit. The reconnect loop will
            # flush this row once login_ok arrives.
            assert self.name is not None
            append_pending(self.name, msg_id, recipient, text)
            out("queued")
            return

        # LOGGED_IN — transmit immediately, do NOT write to outbox.
        await self.conn.send({
            "type": "send", "id": msg_id, "to": recipient, "text": text,
        })

    async def _flush_outbox(self) -> None:
        """Read outbox, replay pending rows in file order, mark sent.

        For each pending row: send, wait for send_ok, update status,
        atomic rewrite. If the connection dies mid-flush, the unsent
        rows stay pending and will be retried on the next reconnect.
        """
        assert self.name is not None
        rows = read_all(self.name)
        if not rows:
            return
        for i, row in enumerate(rows):
            if row.get("status") != "pending":
                continue
            ok = await self.conn.send_and_wait_ack(
                row["id"], row["to"], row["text"],
            )
            if not ok:
                # Connection dropped or ack timed out; row stays pending.
                return
            rows[i] = {**row, "status": "sent"}
            rewrite_all(self.name, rows)

    def cmd_help(self) -> None:
        sys.stdout.write(HELP_TEXT + "\n")
        sys.stdout.flush()

    # ── Top-level loop ──────────────────────────────────────────────

    async def handle_line(self, line: str) -> bool:
        line = line.rstrip("\r\n")
        if not line:
            return True
        if not line.startswith("/"):
            out("error: unknown command — try /help")
            return True
        space_idx = line.find(" ")
        if space_idx == -1:
            verb, rest = line, ""
        else:
            verb, rest = line[:space_idx], line[space_idx:]

        if verb == "/login":
            await self.cmd_login(rest.strip().split() if rest.strip() else [])
        elif verb == "/send":
            await self.cmd_send(rest)
        elif verb == "/help":
            if rest.strip(): out("error: usage: /help")
            else: self.cmd_help()
        elif verb == "/quit":
            if self._reconnect_task is not None and not self._reconnect_task.done():
                self._reconnect_task.cancel()
            await self.conn.close()
            return False
        else:
            out("error: unknown command — try /help")
        return True

    async def run(self) -> None:
        while True:
            try:
                line = await asyncio.to_thread(sys.stdin.readline)
            except (KeyboardInterrupt, EOFError):
                break
            if not line:
                break
            if not await self.handle_line(line):
                break
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        await self.conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Messaging CLI client (Python)")
    parser.add_argument("--version", action="version", version="messaging-client 1.0.0")
    parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)
    try:
        asyncio.run(Client().run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
