"""Iteration 5 — outbox flush on auto-reconnect.

Two cases mapping to spec Step 5.a–5.b:
  5.a — flush delivers queued messages after server cycle,
        rows flip pending → sent on alice's outbox
  5.b — already-`sent` rows are skipped (no re-transmission)

Both cases drive the offline/online transition via server stop/restart
(no `/offline` `/online` commands in the new model).
"""
from __future__ import annotations

import json
from pathlib import Path

from framework import server_running, start_client, step, ok


def _read_outbox(cwd: Path, name: str) -> list[dict]:
    p = cwd / f"outbox-{name}.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


async def case_5a_flush_to_online(client_cmd: str) -> None:
    step("Step 5.a — flush delivers queued messages on auto-reconnect")
    async with server_running() as server:
        alice = await start_client("alice", client_cmd)
        bob = await start_client("bob", client_cmd)
        try:
            await alice.send("/login alice")
            await alice.wait_for("logged in as alice")
            await bob.send("/login bob")
            await bob.wait_for("logged in as bob")

            # Server outage: both clients should transition OFFLINE.
            await server.stop()
            await alice.wait_for("disconnected")
            await bob.wait_for("disconnected")

            # Alice queues two messages while offline.
            await alice.send("/send bob first")
            await alice.wait_for("queued")
            await alice.send("/send bob second")
            await alice.wait_for("queued")

            # Server comes back. Both clients auto-reconnect; alice's
            # outbox flush delivers both messages to bob.
            await server.start()
            await alice.wait_for("reconnected", timeout=15.0)
            await bob.wait_for("reconnected", timeout=15.0)

            # Bob receives both in send order.
            await bob.wait_for("[alice → bob]  first")
            await bob.wait_for("[alice → bob]  second")

            # Alice's outbox rows flipped to sent.
            rows = _read_outbox(alice.cwd, "alice")
            assert len(rows) == 2, f"expected 2 rows, got {rows}"
            assert all(r["status"] == "sent" for r in rows), f"rows not all sent: {rows}"
            assert rows[0]["text"] == "first" and rows[1]["text"] == "second", rows
            ok("bob got 2 messages in order; outbox rows all status=sent")
        finally:
            await alice.close()
            await bob.close()


async def case_5b_skips_already_sent(client_cmd: str) -> None:
    step("Step 5.b — flush skips rows already marked sent")
    async with server_running() as server:
        alice = await start_client("alice", client_cmd)
        bob = await start_client("bob", client_cmd)
        try:
            await alice.send("/login alice")
            await alice.wait_for("logged in as alice")
            await bob.send("/login bob")
            await bob.wait_for("logged in as bob")

            # First cycle: queue one, flush via server restart, verify sent.
            await server.stop()
            await alice.wait_for("disconnected")
            await bob.wait_for("disconnected")
            await alice.send("/send bob hello")
            await alice.wait_for("queued")
            await server.start()
            await alice.wait_for("reconnected", timeout=15.0)
            await bob.wait_for("reconnected", timeout=15.0)
            await bob.wait_for("[alice → bob]  hello")

            rows_after_first = _read_outbox(alice.cwd, "alice")
            assert len(rows_after_first) == 1 and rows_after_first[0]["status"] == "sent"

            # Second cycle: no new queued messages. The existing "sent"
            # row must NOT trigger a re-send to bob.
            await server.stop()
            await alice.wait_for("disconnected")
            await bob.wait_for("disconnected")
            await server.start()
            await alice.wait_for("reconnected", timeout=15.0)
            await bob.wait_for("reconnected", timeout=15.0)

            # Bob should NOT see a second "hello".
            await bob.assert_not_seen("[alice → bob]  hello", window=0.4)

            # Outbox unchanged.
            rows_after_second = _read_outbox(alice.cwd, "alice")
            assert rows_after_second == rows_after_first, (
                f"outbox changed unexpectedly: {rows_after_first} → {rows_after_second}"
            )
            ok("already-sent row was skipped; no double delivery; file unchanged")
        finally:
            await alice.close()
            await bob.close()


def cases():
    return [
        case_5a_flush_to_online,
        case_5b_skips_already_sent,
    ]
