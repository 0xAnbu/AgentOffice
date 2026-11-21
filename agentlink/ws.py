"""WS /ws/events — real-time push of the durable event log to dashboards.

Protocol (JSON text frames)::

    client connects   /ws/events?ticket=<single-use ticket from POST /api/ws-ticket>&after=<last event id>
    server -> client  {"type": "hello", "head": N}
                      {"type": "events", "events": [...], "next_after": N}   (replay, then live, in id order)
                      {"type": "resync"}   cursor older than retained history: GET /api/office/snapshot
                      {"type": "ping"}     keep-alive every 20 s
    client -> server  {"type": "ack", "after": N}   optional, lets the server track the client cursor

The socket is only a notification channel; the event table is the source of truth. A client that
drops simply reconnects with its last cursor and receives what it missed, de-duplicating by id.
Events are filtered by the same visibility rules as GET /api/events.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import events
from .api import redeem_ticket
from .hub import get_hub


async def ws_events(ws: WebSocket) -> None:
    p = redeem_ticket(ws.query_params.get("ticket"))
    if p is None:
        await ws.close(code=4401, reason="invalid or expired ticket")
        return
    hub = await run_in_threadpool(get_hub)
    try:
        after = max(0, int(ws.query_params.get("after") or 0))
    except ValueError:
        after = 0
    await ws.accept()
    await ws.send_json({"type": "hello", "head": await run_in_threadpool(events.head, hub.db), "agent": p.name})
    last_ping = time.monotonic()

    async def reader() -> None:
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            while True:
                await ws.receive_text()  # acks are optional; reading keeps the close handshake working

    reading = asyncio.create_task(reader())
    try:
        while not reading.done():
            seen = hub.notifier.version
            res = await run_in_threadpool(events.list_events, hub.db, p, {"after": after, "limit": 500})
            if res["resync"]:
                await ws.send_json({"type": "resync", "head": res["head"]})
                after = res["head"]
                continue
            if res["events"]:
                after = res["next_after"]
                await ws.send_json({"type": "events", "events": res["events"], "next_after": after})
                if res["has_more"]:
                    continue
            if time.monotonic() - last_ping > 20:
                await ws.send_json({"type": "ping"})
                last_ping = time.monotonic()
            await hub.notifier.wait_change(seen, 1.0)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        reading.cancel()
        with contextlib.suppress(Exception):
            await ws.close()
