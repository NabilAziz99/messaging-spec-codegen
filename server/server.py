"""Reference server entry point + connection lifecycle.

The protocol-level parsing lives in protocol.py; state + per-frame
business logic in handlers.py. This file is just the WebSocket server
setup and the dispatch loop.

Run:
    python server/server.py [--host 127.0.0.1] [--port 8765]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

# Make sibling imports work when running as `python server/server.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import websockets
from websockets.exceptions import ConnectionClosed

from protocol import (  # noqa: E402
    CLOSE_INVALID_FRAME,
    DEFAULT_HOST,
    DEFAULT_PORT,
    MAX_FRAME_SIZE_BYTES,
    WS_PATH,
    parse_frame,
)
from handlers import (  # noqa: E402
    handle_deliver_ok,
    handle_login,
    handle_send,
    on_disconnect,
)

logger = logging.getLogger("server")


async def connection_handler(ws) -> None:
    """One connection's full lifecycle: enforce path, read frames in a
    loop, dispatch by type, clean up on close."""
    # Accept only WS_PATH. Probe both newer (ws.request.path) and legacy
    # (ws.path) websockets APIs.
    path = (
        getattr(getattr(ws, "request", None), "path", None)
        or getattr(ws, "path", "/")
    )
    if path != WS_PATH:
        await ws.close(code=CLOSE_INVALID_FRAME, reason=f"unknown path {path}")
        return

    user: Optional[str] = None
    try:
        async for raw in ws:
            try:
                # Iteration 9: frame size cap (16 KiB). Reject before parsing.
                raw_bytes = raw if isinstance(raw, bytes) else raw.encode("utf-8")
                if len(raw_bytes) > MAX_FRAME_SIZE_BYTES:
                    raise ValueError("oversize frame")
                frame = parse_frame(raw)
                frame_type = frame["type"]
                if frame_type == "login":
                    if user is not None:
                        raise ValueError("already logged in on this connection")
                    user = await handle_login(ws, frame)
                elif frame_type == "send":
                    if user is None:
                        raise ValueError("send: not logged in")
                    await handle_send(ws, frame, user)
                elif frame_type == "deliver_ok":
                    if user is None:
                        raise ValueError("deliver_ok: not logged in")
                    await handle_deliver_ok(frame, user)
                else:
                    raise ValueError(f"unsupported frame: {frame_type!r}")
            except ValueError as e:
                logger.warning("invalid frame from %r: %s", user or "?", e)
                await ws.close(code=CLOSE_INVALID_FRAME, reason=str(e)[:120])
                return
    except ConnectionClosed:
        pass
    finally:
        on_disconnect(user, ws)


async def main_async(host: str, port: int) -> None:
    async with websockets.serve(connection_handler, host, port):
        logger.info("listening on ws://%s:%d%s", host, port, WS_PATH)
        await asyncio.Future()  # run forever


def main() -> None:
    parser = argparse.ArgumentParser(description="Messaging spec reference server")
    parser.add_argument("--host",      default=DEFAULT_HOST)
    parser.add_argument("--port",      default=DEFAULT_PORT, type=int)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
    )
    try:
        asyncio.run(main_async(args.host, args.port))
    except KeyboardInterrupt:
        logger.info("shutting down")


if __name__ == "__main__":
    main()
