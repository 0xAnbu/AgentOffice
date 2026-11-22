"""ASGI application: REST API, MCP endpoint, monitoring UI and security headers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .api import STATIC, app_error, internal_error, routes
from .errors import AppError
from .mcp import mcp_endpoint
from .ws import ws_events

log = logging.getLogger("agentlink.app")

logging.basicConfig(
    level=os.environ.get("AGENTLINK_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

SECURITY_HEADERS = [
    (
        b"content-security-policy",
        b"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data: blob:; "
        b"font-src 'self'; "
        b"base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    ),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
]


class SecurityHeaders:
    """Pure ASGI middleware (safe with streaming responses)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # websockets carry no response headers to decorate
            await self.app(scope, receive, send)
            return
        is_api = scope["path"].startswith(("/api", "/mcp"))

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(SECURITY_HEADERS)
                if is_api:
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, wrapped)


async def index(_: Request) -> Response:
    """The 3D building (visualization plane). Data comes only from the API; it never simulates activity."""
    return FileResponse(STATIC / "office" / "index.html")


async def chat(_: Request) -> Response:
    """Full chat / project status view, without the 3D building."""
    return FileResponse(STATIC / "chat" / "index.html")


async def tareas(_: Request) -> Response:
    """Task manager for project administrators (writes go through the regular task/message/coordinator API)."""
    return FileResponse(STATIC / "tareas" / "index.html")


async def _reconcile_loop() -> None:
    """Recovery supervisor: runs inside the single backend, on the same DB and transaction model."""
    from starlette.concurrency import run_in_threadpool

    from .hub import get_hub
    from .recovery import reconcile

    hub = None
    interval = 5.0
    while True:
        try:
            if hub is None:  # inside the try: a failed first start (e.g. database locked) is retried, not fatal
                hub = await run_in_threadpool(get_hub)
                interval = hub.settings.reconcile_interval
                if interval <= 0:
                    return
                log.info("recovery supervisor running every %.1fs", interval)
            stats = await run_in_threadpool(reconcile, hub.db, hub.settings)
            if any(stats.values()):
                hub.notifier.bump()
        except Exception:  # keep supervising; the error is logged and the next pass retries
            log.exception("reconcile pass failed")
        await asyncio.sleep(interval if hub is not None else 1.0)  # retry a failed start quickly


@contextlib.asynccontextmanager
async def lifespan(_: Starlette) -> AsyncIterator[None]:
    task = asyncio.create_task(_reconcile_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def create_app() -> Starlette:
    app = Starlette(
        lifespan=lifespan,
        routes=[
            WebSocketRoute("/ws/events", ws_events),
            *routes,
            Route("/mcp", mcp_endpoint, methods=["GET", "POST", "DELETE"]),
            Route("/", index),
            Route("/chat", chat),
            Route("/tareas", tareas),
            Mount("/office", app=StaticFiles(directory=STATIC / "office"), name="office"),
            Mount("/static", app=StaticFiles(directory=STATIC), name="static"),
        ],
        exception_handlers={AppError: app_error, Exception: internal_error},
    )
    app.add_middleware(SecurityHeaders)
    return app


app = create_app()
