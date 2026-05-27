"""Iteration 9 — protocol error handling.

Four cases mapping to spec Step 9.a–9.d. Each opens a raw WebSocket
to the server, sends a malformed frame, and verifies the server
closes with close code 4001 and the spec-pinned reason string.

These cases bypass the CLI client because we need to send raw bytes
that a well-behaved client would never produce.
"""
from __future__ import annotations

import json

import websockets as ws_lib

from framework import server_running, step, ok


SERVER_URL = "ws://127.0.0.1:8765/ws"


async def _expect_close(coro_send, expected_reason: str) -> None:
    """Open a WS, run the send coroutine, expect ConnectionClosed with
    code 4001 and the given reason. Raises AssertionError otherwise."""
    async with ws_lib.connect(SERVER_URL) as ws:
        try:
            await coro_send(ws)
            # If the server closes the WS, the next recv raises.
            await ws.recv()
            raise AssertionError("server did not close; expected 4001")
        except ws_lib.ConnectionClosedError as e:
            assert e.code == 4001, f"expected close code 4001, got {e.code}"
            # The reason can be in .reason or in the rcvd close frame.
            reason = e.reason or (getattr(e, "rcvd", None) and e.rcvd.reason) or ""
            assert reason == expected_reason, (
                f"expected reason {expected_reason!r}, got {reason!r}"
            )


async def case_9a_invalid_json(client_cmd: str) -> None:
    step("Step 9.a — malformed JSON → close 4001 'invalid json'")
    async with server_running():
        async def send_bad_json(ws):
            await ws.send("{not json")
        await _expect_close(send_bad_json, "invalid json")
        ok("server closed with 4001 'invalid json'")


async def case_9b_unknown_type(client_cmd: str) -> None:
    step("Step 9.b — unknown frame type → close 4001 'invalid frame'")
    async with server_running():
        async def send_bad_type(ws):
            await ws.send(json.dumps({"type": "nope", "name": "alice"}))
        await _expect_close(send_bad_type, "invalid frame")
        ok("server closed with 4001 'invalid frame'")


async def case_9c_extra_fields(client_cmd: str) -> None:
    step("Step 9.c — extra fields → close 4001 'invalid frame'")
    async with server_running():
        async def send_extras(ws):
            await ws.send(json.dumps({
                "type": "login", "name": "alice", "extra": "field",
            }))
        await _expect_close(send_extras, "invalid frame")
        ok("server closed with 4001 'invalid frame'")


async def case_9d_oversize(client_cmd: str) -> None:
    step("Step 9.d — oversize frame (>16 KiB) → close 4001 'oversize frame'")
    async with server_running():
        # Build a frame > 16384 bytes by padding the text field.
        big_text = "x" * 17000
        async def send_oversize(ws):
            await ws.send(json.dumps({
                "type": "login", "name": "alice",
                # 'name' regex would normally reject this, but the size
                # check runs BEFORE shape validation per spec.
            }) + " " * 17000)  # padded to >16KiB
            # Actually simpler: just send a large payload directly
        # The above is messy — easier to just send a big string we control:
        async def send_oversize2(ws):
            payload = '{"type":"login","name":"alice","pad":"' + ("x" * 17000) + '"}'
            assert len(payload.encode("utf-8")) > 16384
            await ws.send(payload)
        await _expect_close(send_oversize2, "oversize frame")
        ok("server closed with 4001 'oversize frame'")


def cases():
    return [
        case_9a_invalid_json,
        case_9b_unknown_type,
        case_9c_extra_fields,
        case_9d_oversize,
    ]
