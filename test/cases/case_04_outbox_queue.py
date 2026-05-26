"""Iteration 4 — outbox queue (offline /send) conformance cases.

Three cases mapping to spec Step 4.a–4.c:
  4.a — outbox row created on first OFFLINE /send
  4.b — multiple OFFLINE sends append in order
  4.c — online /send does NOT write to outbox

OFFLINE is now reached by stopping the server (no more `/offline`
command). The harness spawns each client in its own temp cwd; the
outbox file lives at <cwd>/outbox-<name>.jsonl. We read it directly
from disk to assert on-disk state.
"""
from __future__ import annotations

import json
from pathlib import Path

from framework import server_running, start_client, step, ok


def _read_outbox(cwd: Path, name: str) -> list[dict]:
    """Read outbox-<name>.jsonl from a client's cwd. Empty list if absent."""
    p = cwd / f"outbox-{name}.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


async def case_4a_outbox_created(client_cmd: str) -> None:
    step("Step 4.a — outbox row created on first OFFLINE /send")
    async with server_running() as server:
        alice = await start_client("alice", client_cmd)
        try:
            await alice.send("/login alice")
            await alice.wait_for("logged in as alice")
            await server.stop()
            await alice.wait_for("disconnected")
            await alice.send("/send bob hi bob")
            await alice.wait_for("queued")

            rows = _read_outbox(alice.cwd, "alice")
            assert len(rows) == 1, f"expected 1 row, got {len(rows)}: {rows}"
            r = rows[0]
            assert set(r.keys()) == {"id", "to", "text", "status"}, f"bad keys: {r.keys()}"
            assert r["to"] == "bob" and r["text"] == "hi bob" and r["status"] == "pending", r
            ok(f"outbox has 1 pending row to=bob text='hi bob'")
        finally:
            await alice.close()


async def case_4b_outbox_append_order(client_cmd: str) -> None:
    step("Step 4.b — multiple OFFLINE sends append in order")
    async with server_running() as server:
        alice = await start_client("alice", client_cmd)
        try:
            await alice.send("/login alice")
            await alice.wait_for("logged in as alice")
            await server.stop()
            await alice.wait_for("disconnected")
            await alice.send("/send bob first")
            await alice.wait_for("queued")
            await alice.send("/send carol second")
            await alice.wait_for("queued")
            await alice.send("/send bob third")
            await alice.wait_for("queued")

            rows = _read_outbox(alice.cwd, "alice")
            assert len(rows) == 3, f"expected 3 rows, got {len(rows)}: {rows}"
            assert (rows[0]["to"], rows[0]["text"]) == ("bob", "first")
            assert (rows[1]["to"], rows[1]["text"]) == ("carol", "second")
            assert (rows[2]["to"], rows[2]["text"]) == ("bob", "third")
            assert all(r["status"] == "pending" for r in rows)
            ok("outbox has 3 rows in send order, all pending")
        finally:
            await alice.close()


async def case_4c_online_send_no_outbox(client_cmd: str) -> None:
    step("Step 4.c — online /send does NOT write to outbox")
    async with server_running():
        alice = await start_client("alice", client_cmd)
        bob = await start_client("bob", client_cmd)
        try:
            await alice.send("/login alice")
            await alice.wait_for("logged in as alice")
            await bob.send("/login bob")
            await bob.wait_for("logged in as bob")
            await alice.send("/send bob live message")
            await bob.wait_for("[alice → bob]  live message")

            rows = _read_outbox(alice.cwd, "alice")
            assert rows == [], f"outbox should be empty for live sends, got {rows}"
            ok("alice's outbox is empty (live send not persisted)")
        finally:
            await alice.close()
            await bob.close()


def cases():
    return [
        case_4a_outbox_created,
        case_4b_outbox_append_order,
        case_4c_online_send_no_outbox,
    ]
