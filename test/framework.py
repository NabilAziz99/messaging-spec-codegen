"""Test framework — reusable across all iterations.

This file holds the pieces that DON'T change per-iteration:
  - ClientHandle  : wraps a client subprocess (stdin write, stdout watch)
  - server_running: async context manager that boots+kills the server
  - start_client  : spawns a client in its own temp cwd
  - tiny ANSI helpers for readable PASS/FAIL output

Each iteration adds its own case module under test/cases/. The runner
imports them in order; the framework here stays stable.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import signal
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator


# ─── Constants ─────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_CMD = [sys.executable, str(REPO_ROOT / "server" / "server.py")]
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8765
STEP_TIMEOUT = 8.0
SERVER_BOOT_TIMEOUT = 5.0


# ─── ANSI helpers ──────────────────────────────────────────────────────

class A:
    BOLD = "\033[1m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def ok(msg: str) -> None:   print(f"  {A.GREEN}✓{A.RESET} {msg}")
def fail(msg: str) -> None: print(f"  {A.RED}✗{A.RESET} {msg}")
def step(msg: str) -> None: print(f"{A.BOLD}{msg}{A.RESET}")
def note(msg: str) -> None: print(f"  {A.DIM}{msg}{A.RESET}")


# ─── ClientHandle ──────────────────────────────────────────────────────

class ClientHandle:
    """One client subprocess + its stdin/stdout pipes."""

    def __init__(self, name: str, proc: asyncio.subprocess.Process, cwd: Path):
        self.name = name
        self.proc = proc
        self.cwd = cwd
        self.lines: list[str] = []
        self._reader: asyncio.Task | None = None
        self._wait_cursor: int = 0

    async def start_reader(self) -> None:
        async def _read():
            assert self.proc.stdout is not None
            while True:
                raw = await self.proc.stdout.readline()
                if not raw:
                    return
                self.lines.append(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
        self._reader = asyncio.create_task(_read())

    async def send(self, command: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((command + "\n").encode("utf-8"))
        await self.proc.stdin.drain()
        note(f"{self.name} ← {command}")

    async def wait_for(self, expected: str, timeout: float = STEP_TIMEOUT) -> str:
        """Block until a line containing `expected` arrives."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            for i in range(self._wait_cursor, len(self.lines)):
                if expected in self.lines[i]:
                    self._wait_cursor = i + 1
                    return self.lines[i]
            if asyncio.get_event_loop().time() > deadline:
                tail = "\n".join(f"      {l}" for l in self.lines[-10:])
                raise TimeoutError(
                    f"{self.name}: did not see {expected!r} in {timeout}s\n"
                    f"    last lines:\n{tail or '      (none)'}"
                )
            await asyncio.sleep(0.05)

    async def assert_not_seen(self, forbidden: str, window: float = 0.4) -> None:
        """Assert that `forbidden` does NOT appear in any line received
        during the next `window` seconds. Already-received lines (before
        this call) are ignored — this is a forward-looking assertion."""
        baseline = len(self.lines)
        await asyncio.sleep(window)
        for line in self.lines[baseline:]:
            if forbidden in line:
                raise AssertionError(f"{self.name}: saw forbidden {forbidden!r} in {line!r}")

    async def close(self) -> None:
        if self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass


# ─── Server lifecycle ──────────────────────────────────────────────────

async def _wait_for_server(host: str, port: int, timeout: float) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return
        except (ConnectionRefusedError, OSError):
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError(f"server did not start within {timeout}s")
            await asyncio.sleep(0.1)


class ServerHandle:
    """Wraps the server subprocess; supports stop/start mid-test.

    Iteration 3 introduces this so cases can simulate a network outage
    by stopping the server and then bring it back. Tests that just
    need "server up for the body" can use the `server_running` context
    manager and ignore the handle.
    """

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        if self.running:
            return
        self._proc = await asyncio.create_subprocess_exec(
            *SERVER_CMD, "--host", SERVER_HOST, "--port", str(SERVER_PORT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        await _wait_for_server(SERVER_HOST, SERVER_PORT, SERVER_BOOT_TIMEOUT)

    async def stop(self) -> None:
        if self._proc is None or self._proc.returncode is not None:
            self._proc = None
            return
        self._proc.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            self._proc.kill()
            await self._proc.wait()
        self._proc = None
        # Give the OS a moment to release the listening socket so a
        # subsequent .start() doesn't trip "address in use".
        await asyncio.sleep(0.15)


@asynccontextmanager
async def server_running() -> AsyncIterator[ServerHandle]:
    """Context manager: server is up for the body, cleanly stopped on exit.

    Yields a `ServerHandle`. Most cases ignore it (`async with
    server_running():`); cases that need to cycle the server mid-test
    bind it (`async with server_running() as server:`) and call
    `server.stop()` / `server.start()`.
    """
    server = ServerHandle()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def _resolve_paths(argv: list[str]) -> list[str]:
    """Resolve relative paths in argv to absolute paths from REPO_ROOT."""
    out = []
    for arg in argv:
        if not arg.startswith("-") and not Path(arg).is_absolute():
            candidate = REPO_ROOT / arg
            if candidate.exists():
                out.append(str(candidate))
                continue
        out.append(arg)
    return out


async def start_client(name: str, cmd: str) -> ClientHandle:
    """Spawn a client subprocess in its own temp cwd."""
    argv = _resolve_paths(shlex.split(cmd))
    cwd = Path(tempfile.mkdtemp(prefix=f"client-{name}-"))
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(cwd),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    h = ClientHandle(name=name, proc=proc, cwd=cwd)
    await h.start_reader()
    return h
