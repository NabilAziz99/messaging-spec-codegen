"""WebSocket lifecycle.

The `Connection` class wraps a single WebSocket and exposes a small
interface to the client:

  - connect_and_login(name) → str    : "ok" | "unreachable" | "superseded"
  - send(frame) → bool                : send a frame; False if not connected
  - close()                           : close cleanly
  - on_deliver, on_disconnect         : callbacks set by the client

This module is reusable across the initial `/login`, the auto-reconnect
loop, and any other path that needs a WS. It does NOT print user-facing
errors — that's the client layer's job (e.g., `error: cannot reach
server` is only shown on user-initiated `/login`, not on background
reconnect retries).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

import websockets
from websockets.exceptions import ConnectionClosed, InvalidURI, InvalidHandshake

from protocol import CLOSE_SUPERSEDED, encode_frame


class Connection:
    def __init__(
        self,
        url: str,
        on_deliver: Callable[[dict[str, Any]], Awaitable[None]],
        on_disconnect: Callable[[bool], None],
    ) -> None:
        self.url = url
        self.ws: Any = None
        self._reader_task: asyncio.Task[None] | None = None
        self._login_ok_future: asyncio.Future[None] | None = None
        # Pending send_ok waiters keyed by message id. Resolved by the
        # reader when send_ok arrives. Used by send_and_wait_ack() during
        # outbox flush.
        self._send_ok_waiters: dict[str, asyncio.Future[None]] = {}
        self._on_deliver = on_deliver
        self._on_disconnect = on_disconnect
        # Set when a login attempt's WS closes with code 4000 before
        # login_ok arrived. connect_and_login uses this to disambiguate
        # the failure cause.
        self._login_superseded: bool = False

    @property
    def is_open(self) -> bool:
        return self.ws is not None

    async def connect_and_login(self, name: str) -> str:
        """Open WS, send login, await login_ok.

        Returns:
            "ok"          — login_ok received.
            "unreachable" — couldn't connect, or the WS dropped before
                            login_ok (treated as a transient failure).
            "superseded"  — the server closed mid-login with code 4000.

        Never prints to stdout. The caller decides what to surface.
        """
        # Clean up any residue from a prior attempt (the reconnect loop
        # calls this in a tight loop).
        await self._cleanup_prior_attempt()

        loop = asyncio.get_running_loop()
        self._login_ok_future = loop.create_future()
        self._login_superseded = False

        try:
            self.ws = await websockets.connect(self.url)
        except (OSError, InvalidURI, InvalidHandshake, asyncio.TimeoutError):
            self.ws = None
            self._login_ok_future = None
            return "unreachable"

        try:
            await self.ws.send(encode_frame({"type": "login", "name": name}))
        except (ConnectionClosed, OSError):
            try: await self.ws.close()
            except Exception: pass
            self.ws = None
            self._login_ok_future = None
            return "unreachable"

        self._reader_task = asyncio.create_task(self._reader_loop())

        try:
            await self._login_ok_future
        except Exception:
            superseded = self._login_superseded
            await self.close()
            return "superseded" if superseded else "unreachable"
        return "ok"

    async def _cleanup_prior_attempt(self) -> None:
        """Tear down any leftover state from a previous connect attempt.

        The reconnect loop reuses one Connection across many attempts —
        this guards against the WS / reader task from the prior attempt
        still being half-alive when the next attempt starts.
        """
        if self.ws is not None:
            try: await self.ws.close()
            except Exception: pass
            self.ws = None
        if self._reader_task is not None and not self._reader_task.done():
            try:
                await asyncio.wait_for(self._reader_task, timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                pass
        self._reader_task = None
        for fut in self._send_ok_waiters.values():
            if not fut.done():
                fut.cancel()
        self._send_ok_waiters.clear()
        self._login_ok_future = None

    async def send(self, frame: dict[str, Any]) -> bool:
        if self.ws is None:
            return False
        try:
            await self.ws.send(encode_frame(frame))
            return True
        except (ConnectionClosed, OSError):
            return False

    async def send_and_wait_ack(
        self, msg_id: str, to: str, text: str, timeout: float = 5.0
    ) -> bool:
        """Send one `send` frame and wait for its `send_ok`.

        Returns True on success, False if the connection drops or the
        ack times out. Used by the outbox flush in client.py.
        """
        if self.ws is None:
            return False
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._send_ok_waiters[msg_id] = future
        try:
            sent = await self.send({"type": "send", "id": msg_id, "to": to, "text": text})
            if not sent:
                return False
            await asyncio.wait_for(future, timeout=timeout)
            return True
        except (asyncio.TimeoutError, ConnectionClosed):
            return False
        finally:
            self._send_ok_waiters.pop(msg_id, None)

    async def close(self) -> None:
        if self.ws is not None:
            try: await self.ws.close()
            except Exception: pass
            self.ws = None
        if self._reader_task is not None:
            try: await self._reader_task
            except Exception: pass
            self._reader_task = None

    # ── Internals ────────────────────────────────────────────────────

    async def _reader_loop(self) -> None:
        # IMPORTANT: depending on the `websockets` version, a server-
        # initiated close may either raise `ConnectionClosed` or just
        # let the `async for` exit silently. We must call _handle_close
        # in every termination path or the client won't notice the
        # disconnect (state stays LOGGED_IN forever).
        ws = self.ws
        superseded = False
        try:
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(frame, dict):
                    continue
                await self._dispatch_frame(frame)
            # Graceful exit — inspect close_code on the (now-closed) ws.
            code = getattr(ws, "close_code", None)
            if code is not None and code == CLOSE_SUPERSEDED:
                superseded = True
        except ConnectionClosed as e:
            superseded = (getattr(e, "code", None) == CLOSE_SUPERSEDED) or (
                getattr(getattr(e, "rcvd", None), "code", None) == CLOSE_SUPERSEDED
            )
        except Exception:
            pass
        self._handle_close(superseded=superseded)

    def _handle_close(self, *, superseded: bool) -> None:
        # If we were mid-login, record the cause for connect_and_login()
        # to disambiguate "unreachable" vs "superseded" return values.
        if self._login_ok_future is not None and not self._login_ok_future.done():
            self._login_superseded = superseded
            self._login_ok_future.set_exception(ConnectionClosed(None, None))
        # Fail any pending send_ok waiters so flush can unwind cleanly.
        for fut in self._send_ok_waiters.values():
            if not fut.done():
                fut.set_exception(ConnectionClosed(None, None))
        self._send_ok_waiters.clear()
        self.ws = None
        self._on_disconnect(superseded)

    async def _dispatch_frame(self, frame: dict[str, Any]) -> None:
        t = frame.get("type")
        if t == "login_ok":
            if self._login_ok_future is not None and not self._login_ok_future.done():
                self._login_ok_future.set_result(None)
            return
        if t == "send_ok":
            mid = frame.get("id")
            if isinstance(mid, str):
                waiter = self._send_ok_waiters.pop(mid, None)
                if waiter is not None and not waiter.done():
                    waiter.set_result(None)
            return
        if t == "deliver":
            await self._on_deliver(frame)
