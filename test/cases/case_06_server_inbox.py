"""Iteration 6 — server inbox (offline recipient).

Three cases mapping to spec Step 6.a–6.c:
  6.a — server holds a message for an offline recipient; flushed on login
  6.b — multiple queued messages flushed in FIFO order
  6.c — deliver_ok clears the inbox (no re-delivery on re-login)
"""
from __future__ import annotations

from framework import server_running, start_client, step, ok


async def case_6a_offline_recipient(client_cmd: str) -> None:
    step("Step 6.a — server holds for offline recipient, flushes on login")
    async with server_running():
        alice = await start_client("alice", client_cmd)
        try:
            await alice.send("/login alice")
            await alice.wait_for("logged in as alice")
            # Send to bob who is not online
            await alice.send("/send bob hi bob")
            # Wait briefly so the server has time to register the send
            # (no client-side confirmation since live sends print nothing)

            bob = await start_client("bob", client_cmd)
            try:
                await bob.send("/login bob")
                await bob.wait_for("logged in as bob")
                await bob.wait_for("[alice → bob]  hi bob")
                ok("bob received the queued message on first login")
            finally:
                await bob.close()
        finally:
            await alice.close()


async def case_6b_inbox_fifo(client_cmd: str) -> None:
    step("Step 6.b — multiple queued messages flushed in FIFO order")
    async with server_running():
        alice = await start_client("alice", client_cmd)
        try:
            await alice.send("/login alice")
            await alice.wait_for("logged in as alice")
            await alice.send("/send bob first")
            await alice.send("/send bob second")
            await alice.send("/send bob third")

            bob = await start_client("bob", client_cmd)
            try:
                await bob.send("/login bob")
                await bob.wait_for("logged in as bob")
                await bob.wait_for("[alice → bob]  first")
                await bob.wait_for("[alice → bob]  second")
                await bob.wait_for("[alice → bob]  third")
                ok("bob received 3 messages in FIFO order")
            finally:
                await bob.close()
        finally:
            await alice.close()


async def case_6c_deliver_ok_clears(client_cmd: str) -> None:
    step("Step 6.c — deliver_ok clears the inbox (no re-delivery on reconnect)")
    async with server_running() as server:
        alice = await start_client("alice", client_cmd)
        try:
            await alice.send("/login alice")
            await alice.wait_for("logged in as alice")
            await alice.send("/send bob once")

            bob = await start_client("bob", client_cmd)
            try:
                await bob.send("/login bob")
                await bob.wait_for("logged in as bob")
                await bob.wait_for("[alice → bob]  once")
                # Cycle the server: bob's WS drops, bob auto-reconnects.
                # The server must NOT re-deliver "once" because bob
                # already acked it (deliver_ok cleared the inbox).
                await server.stop()
                await bob.wait_for("disconnected")
                await alice.wait_for("disconnected")
                await server.start()
                await bob.wait_for("reconnected", timeout=15.0)
                await alice.wait_for("reconnected", timeout=15.0)
                await bob.assert_not_seen("[alice → bob]  once", window=0.4)
                ok("server cycle did not re-deliver (deliver_ok cleared inbox)")
            finally:
                await bob.close()
        finally:
            await alice.close()


def cases():
    return [
        case_6a_offline_recipient,
        case_6b_inbox_fifo,
        case_6c_deliver_ok_clears,
    ]
