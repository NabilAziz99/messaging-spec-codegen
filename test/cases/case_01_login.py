"""Iteration 1 — login conformance cases.

Five cases mapping to the spec's 'Conformance scenario' section:
  1.a — happy path login
  1.b — invalid name rejected
  1.c — server unreachable
  1.d — duplicate /login while LOGGED_IN
  1.e — supersede kicks the first session

Each case is an async function taking `client_cmd` (the shell command
that launches a client). The `cases()` function at the bottom returns
the ordered list; the runner discovers it and runs them sequentially.
"""
from __future__ import annotations

from framework import server_running, start_client, step, ok


async def case_1a_happy_login(client_cmd: str) -> None:
    step("Step 1.a — happy path login")
    async with server_running():
        alice = await start_client("alice", client_cmd)
        try:
            await alice.send("/login alice")
            await alice.wait_for("logged in as alice")
            ok("alice logged in")
        finally:
            await alice.close()


async def case_1b_invalid_name(client_cmd: str) -> None:
    step("Step 1.b — invalid name rejected")
    async with server_running():
        alice = await start_client("alice", client_cmd)
        try:
            await alice.send("/login bad@name")
            await alice.wait_for("error: usage: /login <name>")
            ok("invalid name rejected")
        finally:
            await alice.close()


async def case_1c_server_unreachable(client_cmd: str) -> None:
    step("Step 1.c — server unreachable")
    # Deliberately no server.
    alice = await start_client("alice", client_cmd)
    try:
        await alice.send("/login alice")
        await alice.wait_for("error: cannot reach server")
        ok("server-down detected")
    finally:
        await alice.close()


async def case_1d_duplicate_login(client_cmd: str) -> None:
    step("Step 1.d — duplicate /login while LOGGED_IN")
    async with server_running():
        alice = await start_client("alice", client_cmd)
        try:
            await alice.send("/login alice")
            await alice.wait_for("logged in as alice")
            await alice.send("/login bob")
            await alice.wait_for("error: already logged in as alice")
            ok("duplicate /login rejected")
        finally:
            await alice.close()


async def case_1e_supersede(client_cmd: str) -> None:
    step("Step 1.e — supersede kicks the first session")
    async with server_running():
        a = await start_client("a", client_cmd)
        b = await start_client("b", client_cmd)
        try:
            await a.send("/login alice")
            await a.wait_for("logged in as alice")
            await b.send("/login alice")
            await b.wait_for("logged in as alice")
            await a.wait_for("disconnected: superseded by another session")
            ok("first session got the supersede notice")
        finally:
            await a.close()
            await b.close()


def cases():
    """Return the ordered list of cases this module contributes."""
    return [
        case_1a_happy_login,
        case_1b_invalid_name,
        case_1c_server_unreachable,
        case_1d_duplicate_login,
        case_1e_supersede,
    ]
