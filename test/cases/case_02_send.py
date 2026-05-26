"""Iteration 2 — online send conformance cases.

Five cases mapping to the spec's Step 2.a–2.e:
  2.a — happy path online send
  2.b — /send with no args
  2.c — /send with no text
  2.d — /send to self
  2.e — /send while INITIAL
"""
from __future__ import annotations

from framework import server_running, start_client, step, ok


async def case_2a_happy_send(client_cmd: str) -> None:
    step("Step 2.a — happy path online send")
    async with server_running():
        alice = await start_client("alice", client_cmd)
        bob = await start_client("bob", client_cmd)
        try:
            await alice.send("/login alice")
            await alice.wait_for("logged in as alice")
            await bob.send("/login bob")
            await bob.wait_for("logged in as bob")
            await alice.send("/send bob hi bob, what's up?")
            await bob.wait_for("[alice → bob]  hi bob, what's up?")
            ok("bob received alice's message")
        finally:
            await alice.close()
            await bob.close()


async def case_2b_send_no_args(client_cmd: str) -> None:
    step("Step 2.b — /send with no args")
    async with server_running():
        alice = await start_client("alice", client_cmd)
        try:
            await alice.send("/login alice")
            await alice.wait_for("logged in as alice")
            await alice.send("/send")
            await alice.wait_for("error: usage: /send <recipient> <text>")
            ok("/send with no args rejected")
        finally:
            await alice.close()


async def case_2c_send_no_text(client_cmd: str) -> None:
    step("Step 2.c — /send with no text")
    async with server_running():
        alice = await start_client("alice", client_cmd)
        try:
            await alice.send("/login alice")
            await alice.wait_for("logged in as alice")
            await alice.send("/send bob")
            await alice.wait_for("error: usage: /send <recipient> <text>")
            ok("/send with no text rejected")
        finally:
            await alice.close()


async def case_2d_send_to_self(client_cmd: str) -> None:
    step("Step 2.d — /send to self")
    async with server_running():
        alice = await start_client("alice", client_cmd)
        try:
            await alice.send("/login alice")
            await alice.wait_for("logged in as alice")
            await alice.send("/send alice hello self")
            await alice.wait_for("[alice → alice]  hello self")
            ok("alice received her own message")
        finally:
            await alice.close()


async def case_2e_send_while_initial(client_cmd: str) -> None:
    step("Step 2.e — /send while INITIAL")
    async with server_running():
        alice = await start_client("alice", client_cmd)
        try:
            await alice.send("/send bob hi")
            await alice.wait_for("error: not logged in; use /login <name> first")
            ok("/send while INITIAL rejected")
        finally:
            await alice.close()


def cases():
    return [
        case_2a_happy_send,
        case_2b_send_no_args,
        case_2c_send_no_text,
        case_2d_send_to_self,
        case_2e_send_while_initial,
    ]
