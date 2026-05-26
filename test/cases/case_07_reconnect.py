"""Iteration 7 — the brief's full 7-step scenario.

One integration case that walks the entire brief verbatim and asserts
the expected stdout + outbox state at each meaningful checkpoint.

The implicit-network model has no `/offline` / `/online` commands; we
combine two mechanisms to drive the scenario:

  - **server stop / start** — globally drops every connected client to
    OFFLINE; used to enter the queueing phase in steps 3-5.
  - **SIGKILL + restart_client** — drops one client independently of
    the other; used in step 6 (alice goes offline alone) and step 7
    (bob comes back alone). The killed process's outbox file persists
    on disk; the relaunched process picks it up via the spec's
    "flush on every LOGGED_IN entry" rule.

Steps (matching spec Step 7):
  1. Alice → Bob (live)
  2. Bob → Alice (live)
  3. Server outage. Alice queues `/send bob "the something"` locally.
  4. Bob queues `/send alice "anything"` locally. Then we kill bob's
     process so he won't auto-reconnect when the server returns.
  5. Server restored. Alice auto-reconnects, flushes "the something" to
     the server; bob is dead, so the server stores it in inbox[bob].
  6. Kill alice's process (her network drops again).
  7. Relaunch bob; he /logins, flushes inbox[bob] to him (he sees "the
     something"), and his outbox flush sends "anything" to the server,
     which stores it in inbox[alice].
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from framework import server_running, start_client, restart_client, step, ok


def _read_outbox(cwd: Path, name: str) -> list[dict]:
    p = cwd / f"outbox-{name}.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


async def case_7_brief_full_scenario(client_cmd: str) -> None:
    step("Step 7 — the brief's full 7-step scenario (concurrent reconnect)")
    msg_hi      = "Hi Bob, I have something important to tell you"
    msg_what    = "What is it?"
    msg_late_a  = "the something"
    msg_late_b  = "anything"

    async with server_running() as server:
        alice = await start_client("alice", client_cmd)
        bob = await start_client("bob", client_cmd)
        try:
            # Setup — both login
            await alice.send("/login alice")
            await alice.wait_for("logged in as alice")
            await bob.send("/login bob")
            await bob.wait_for("logged in as bob")

            # Step 1 — Alice → Bob (live)
            await alice.send(f"/send bob {msg_hi}")
            await bob.wait_for(f"[alice → bob]  {msg_hi}")
            ok("Step 1 — bob received alice's long hello")

            # Step 2 — Bob → Alice (live)
            await bob.send(f"/send alice {msg_what}")
            await alice.wait_for(f"[bob → alice]  {msg_what}")
            ok("Step 2 — alice received bob's reply")

            # Step 3 — server outage; alice queues
            await server.stop()
            await alice.wait_for("disconnected")
            await bob.wait_for("disconnected")
            await alice.send(f"/send bob {msg_late_a}")
            await alice.wait_for("queued")
            await bob.assert_not_seen(msg_late_a, window=0.2)
            ok("Step 3 — server down; alice queued 'the something' (bob unaware)")

            # Step 4 — bob queues; then bob's process dies so he won't
            # auto-reconnect when the server returns.
            await bob.send(f"/send alice {msg_late_b}")
            await bob.wait_for("queued")
            await bob.kill()
            ok("Step 4 — bob queued 'anything', then his process died")

            # Step 5 — server returns. Alice auto-reconnects; her outbox
            # flushes; "the something" lands in inbox[bob] (bob is dead).
            await server.start()
            await alice.wait_for("reconnected", timeout=15.0)
            # Poll alice's outbox row until status flips to "sent".
            for _ in range(40):  # 40 × 50ms = 2s budget
                rows = _read_outbox(alice.cwd, "alice")
                if rows and rows[0].get("status") == "sent":
                    break
                await asyncio.sleep(0.05)
            else:
                rows = _read_outbox(alice.cwd, "alice")
                raise AssertionError(f"alice's outbox row never flipped to sent: {rows}")
            ok("Step 5 — alice reconnected, flushed; row status=sent")

            # Step 6 — kill alice's process. Her network drops again.
            await alice.kill()
            ok("Step 6 — alice's process died (offline again)")

            # Step 7 — relaunch bob with the same cwd so his on-disk
            # outbox is picked up. He /logins, server flushes inbox[bob]
            # to him, and his outbox flush sends "anything" to the server.
            bob = await restart_client(bob, client_cmd)
            await bob.send("/login bob")
            await bob.wait_for("logged in as bob")
            # Server flushes inbox[bob] — bob sees alice's queued msg.
            await bob.wait_for(f"[alice → bob]  {msg_late_a}")
            # Bob's outbox row flips to sent (server stored in inbox[alice]).
            for _ in range(40):
                rows = _read_outbox(bob.cwd, "bob")
                if rows and rows[0].get("status") == "sent":
                    break
                await asyncio.sleep(0.05)
            else:
                rows = _read_outbox(bob.cwd, "bob")
                raise AssertionError(f"bob's outbox row never flipped to sent: {rows}")
            ok("Step 7 — bob received alice's queued msg AND flushed his own queue")

            # Final state checks.
            alice_rows = _read_outbox(alice.cwd, "alice")
            bob_rows = _read_outbox(bob.cwd, "bob")
            assert len(alice_rows) == 1 and alice_rows[0]["status"] == "sent"
            assert alice_rows[0]["text"] == msg_late_a
            assert len(bob_rows) == 1 and bob_rows[0]["status"] == "sent"
            assert bob_rows[0]["text"] == msg_late_b
            # Alice's terminal never saw "anything" (she was dead by then).
            assert all(msg_late_b not in line for line in alice.lines), (
                f"alice should NOT have seen {msg_late_b!r}: {alice.lines!r}"
            )
            ok("final state — both outboxes 1 row each, all sent; alice never saw bob's late msg")
        finally:
            await alice.close()
            await bob.close()


def cases():
    return [case_7_brief_full_scenario]
