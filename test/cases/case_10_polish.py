"""Iteration 10 — polish.

Three cases covering the small surface that didn't get a test earlier:
  10.a — /help prints the command list
  10.b — --version flag prints version and exits 0
  10.c — unknown command prints the help-pointing error
"""
from __future__ import annotations

import asyncio
import shlex
import subprocess
import sys

from framework import server_running, start_client, step, ok


async def case_10a_help_command(client_cmd: str) -> None:
    step("Step 10.a — /help prints the command list")
    async with server_running():
        alice = await start_client("alice", client_cmd)
        try:
            await alice.send("/help")
            # /help should list the 4 user-facing commands. /offline and
            # /online are NOT commands in the implicit-network model.
            await alice.wait_for("/login")
            await alice.wait_for("/send")
            await alice.wait_for("/help")
            await alice.wait_for("/quit")
            # Drain any trailing help lines before scanning the buffer.
            await asyncio.sleep(0.2)
            assert not any("/offline" in line for line in alice.lines), (
                f"/help should not list /offline: {alice.lines!r}"
            )
            assert not any("/online" in line for line in alice.lines), (
                f"/help should not list /online: {alice.lines!r}"
            )
            ok("/help printed the 4 commands; no /offline or /online")
        finally:
            await alice.close()


async def case_10b_version_flag(client_cmd: str) -> None:
    step("Step 10.b — --version prints 'messaging-client 1.0.0' and exits 0")
    # Reuse framework's path-resolution by running the command directly.
    # We invoke synchronously since this is a one-shot, not interactive.
    from framework import _resolve_paths
    argv = _resolve_paths(shlex.split(client_cmd) + ["--version"])
    result = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, f"non-zero exit {result.returncode}: {result.stderr}"
    assert "messaging-client 1.0.0" in result.stdout, (
        f"expected 'messaging-client 1.0.0' in stdout, got: {result.stdout!r}"
    )
    ok(f"--version → '{result.stdout.strip()}', exit 0")


async def case_10c_unknown_command(client_cmd: str) -> None:
    step("Step 10.c — unknown command prints 'error: unknown command — try /help'")
    async with server_running():
        alice = await start_client("alice", client_cmd)
        try:
            await alice.send("/bogus")
            await alice.wait_for("error: unknown command — try /help")
            ok("unknown command rejected with help hint")
        finally:
            await alice.close()


def cases():
    return [
        case_10a_help_command,
        case_10b_version_flag,
        case_10c_unknown_command,
    ]
