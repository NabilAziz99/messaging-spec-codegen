"""Iteration 8 — UUID dedup + retry safety.

Two cases mapping to spec Step 8.a–8.b:
  8.a — retry with the same id deduplicates (only one delivery)
  8.b — same id with different to/text still deduplicates

These cases bypass the CLI client and send raw frames over a direct
WebSocket so we can produce the same UUID twice deterministically.
The real clients in clients/ would never do this — a generated UUID
collision is astronomically unlikely — but a retry-after-disconnect
WOULD produce this scenario in production. We test the server's dedup
directly.
"""
from __future__ import annotations

import asyncio
import json

import websockets as ws_lib

from framework import server_running, start_client, step, ok


SERVER_URL = "ws://127.0.0.1:8765/ws"


async def case_8a_retry_same_id(client_cmd: str) -> None:
    step("Step 8.a — retry with same id is deduplicated")
    async with server_running():
        # Bob via the real client (so we can assert what he sees on stdout).
        bob = await start_client("bob", client_cmd)
        try:
            await bob.send("/login bob")
            await bob.wait_for("logged in as bob")

            # Alice via raw WebSocket so we can replay the same frame twice.
            async with ws_lib.connect(SERVER_URL) as alice_ws:
                await alice_ws.send(json.dumps({"type": "login", "name": "alice"}))
                login_ack = json.loads(await alice_ws.recv())
                assert login_ack == {"type": "login_ok", "name": "alice"}

                msg_id = "11111111-1111-4111-8111-111111111111"
                frame = {"type": "send", "id": msg_id, "to": "bob", "text": "duplicate me"}

                await alice_ws.send(json.dumps(frame))
                ack1 = json.loads(await alice_ws.recv())
                assert ack1 == {"type": "send_ok", "id": msg_id}, ack1

                # Replay the EXACT same frame
                await alice_ws.send(json.dumps(frame))
                ack2 = json.loads(await alice_ws.recv())
                assert ack2 == {"type": "send_ok", "id": msg_id}, ack2

            # Bob should have received exactly ONE deliver.
            await bob.wait_for("[alice → bob]  duplicate me")
            await bob.assert_not_seen("[alice → bob]  duplicate me", window=0.4)
            ok("two send_oks returned; bob saw exactly one deliver")
        finally:
            await bob.close()


async def case_8b_dedup_different_content(client_cmd: str) -> None:
    step("Step 8.b — same id, different to/text still dedupes")
    async with server_running():
        bob = await start_client("bob", client_cmd)
        carol = await start_client("carol", client_cmd)
        try:
            await bob.send("/login bob")
            await bob.wait_for("logged in as bob")
            await carol.send("/login carol")
            await carol.wait_for("logged in as carol")

            async with ws_lib.connect(SERVER_URL) as alice_ws:
                await alice_ws.send(json.dumps({"type": "login", "name": "alice"}))
                assert json.loads(await alice_ws.recv())["type"] == "login_ok"

                msg_id = "22222222-2222-4222-8222-222222222222"
                # First: to bob with text "first"
                await alice_ws.send(json.dumps({
                    "type": "send", "id": msg_id, "to": "bob", "text": "first",
                }))
                assert json.loads(await alice_ws.recv())["type"] == "send_ok"
                # Second: SAME id, different to + text. Server should dedup.
                await alice_ws.send(json.dumps({
                    "type": "send", "id": msg_id, "to": "carol", "text": "different",
                }))
                ack2 = json.loads(await alice_ws.recv())
                assert ack2 == {"type": "send_ok", "id": msg_id}, ack2

            # Bob should see the first message
            await bob.wait_for("[alice → bob]  first")
            # Carol should see nothing
            await carol.assert_not_seen("[alice → carol]  different", window=0.4)
            await carol.assert_not_seen("[alice → carol]", window=0.0)  # nothing at all
            ok("bob saw 'first'; carol saw nothing; dedup applied")
        finally:
            await bob.close()
            await carol.close()


def cases():
    return [
        case_8a_retry_same_id,
        case_8b_dedup_different_content,
    ]
