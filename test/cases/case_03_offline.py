"""Iteration 3 — implicit offline state conformance cases.

Four cases mapping to the spec's Step 3.a–3.d. In the new model
there is no `/offline` or `/online` command — OFFLINE / LOGGED_IN
transitions are driven by the actual WebSocket. Tests simulate
network drops by stopping/starting the server:

  3.a — server stop triggers OFFLINE automatically
  3.b — /send while OFFLINE queues to disk
  3.c — server restart triggers auto-reconnect
  3.d — supersede after reconnect halts the reconnect loop
"""
from __future__ import annotations

import asyncio

from framework import server_running, start_client, step, ok


async def case_3a_server_stop_offline(client_cmd: str) -> None:
    step("Step 3.a — server failure triggers OFFLINE automatically")
    async with server_running() as server:
        alice = await start_client("alice", client_cmd)
        try:
            await alice.send("/login alice")
            await alice.wait_for("logged in as alice")
            await server.stop()
            await alice.wait_for("disconnected")
            ok("alice transitioned LOGGED_IN → OFFLINE on server stop")
        finally:
            await alice.close()


async def case_3b_send_while_offline(client_cmd: str) -> None:
    step("Step 3.b — /send while OFFLINE queues to outbox")
    async with server_running() as server:
        alice = await start_client("alice", client_cmd)
        try:
            await alice.send("/login alice")
            await alice.wait_for("logged in as alice")
            await server.stop()
            await alice.wait_for("disconnected")
            await alice.send("/send bob hi")
            await alice.wait_for("queued")
            ok("/send while OFFLINE was queued (no error)")
        finally:
            await alice.close()


async def case_3c_server_restart_reconnect(client_cmd: str) -> None:
    step("Step 3.c — server restoration auto-reconnects")
    async with server_running() as server:
        alice = await start_client("alice", client_cmd)
        try:
            await alice.send("/login alice")
            await alice.wait_for("logged in as alice")
            await server.stop()
            await alice.wait_for("disconnected")
            await server.start()
            # Auto-reconnect uses exponential backoff (1s, 2s, 5s, cap
            # 10s) so allow a generous window for the first retry.
            await alice.wait_for("reconnected", timeout=15.0)
            ok("alice auto-reconnected to LOGGED_IN on server restart")
        finally:
            await alice.close()


async def case_3d_supersede_halts_reconnect(client_cmd: str) -> None:
    step("Step 3.d — supersede after reconnect halts the reconnect loop")
    async with server_running() as server:
        alice = await start_client("alice", client_cmd)
        bob = await start_client("bob", client_cmd)  # second alice-client
        try:
            await alice.send("/login alice")
            await alice.wait_for("logged in as alice")

            # Cycle the server so alice goes through the full
            # OFFLINE → reconnect → LOGGED_IN dance once.
            await server.stop()
            await alice.wait_for("disconnected")
            await server.start()
            await alice.wait_for("reconnected", timeout=15.0)

            # Now supersede her from a second client. Server kicks
            # alice with code 4000; she must go to INITIAL and NOT
            # auto-reconnect.
            await bob.send("/login alice")
            await bob.wait_for("logged in as alice")
            await alice.wait_for("disconnected: superseded by another session")

            # The unique invariant: no `reconnected` afterwards, even
            # though the server is still up.
            await alice.assert_not_seen("reconnected", window=1.5)
            ok("supersede stopped the reconnect loop")
        finally:
            await alice.close()
            await bob.close()


def cases():
    return [
        case_3a_server_stop_offline,
        case_3b_send_while_offline,
        case_3c_server_restart_reconnect,
        case_3d_supersede_halts_reconnect,
    ]
