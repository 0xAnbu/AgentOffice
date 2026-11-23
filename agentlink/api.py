"""REST transport. Each handler: authenticate -> rate-limit -> call a service -> JSON.

No business rules live here; they are in directory/messages/coordination/archive.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from . import (
    __version__,
    archive,
    board,
    coordination,
    coordinator,
    delivery,
    directory,
    events,
    github,
    messages,
    office,
    projects,
    recovery,
    sessions,
)
from . import validation as v
from .errors import AppError, BadRequest, TooLarge
from .hub import Hub, get_hub
from .security import Principal

log = logging.getLogger("agentlink.api")
STATIC = Path(__file__).parent / "static"

Handler = Callable[[Hub, Principal, Request, Any], Any]


async def read_body(request: Request, limit: int) -> bytes:
    """Read at most `limit` bytes: reject on Content-Length first, then count while streaming (chunked bodies)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise TooLarge(f"body exceeds {limit} bytes")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise TooLarge(f"body exceeds {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def read_json(request: Request, hub: Hub) -> Any:
    raw = await read_body(request, hub.settings.max_body_bytes)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        raise BadRequest("invalid JSON body") from None


async def authenticate(request: Request, *, write: bool) -> tuple[Hub, Principal]:
    hub = await run_in_threadpool(get_hub)
    p = await run_in_threadpool(hub.auth.authenticate, request.headers.get("authorization"))
    if write:
        await run_in_threadpool(hub.limiter.check_write, p)
    else:
        hub.limiter.check_read(p)
    return hub, p


def endpoint(fn: Handler, *, write: bool = False) -> Callable[[Request], Awaitable[Response]]:
    """Wrap a synchronous service call; the DB work runs in the thread pool."""

    async def handler(request: Request) -> Response:
        hub, p = await authenticate(request, write=write)
        body = await read_json(request, hub) if request.method in ("POST", "PUT", "PATCH") else None
        result = await run_in_threadpool(fn, hub, p, request, body)
        if isinstance(result, Response):
            return result
        return JSONResponse(result)

    return handler


def _wait(request: Request, hub: Hub) -> float:
    return v.wait_seconds(request.query_params.get("wait"), hub.settings.max_wait_seconds)


def polled(fetch: Callable[[Hub, Principal, Request], dict[str, Any]]) -> Callable[[Request], Awaitable[Response]]:
    """Read endpoint with optional long-poll (?wait=N) and compact text (?format=text)."""

    async def handler(request: Request) -> Response:
        hub, p = await authenticate(request, write=False)
        result = await messages.long_poll(
            lambda: fetch(hub, p, request), _wait(request, hub), hub.notifier, hub.settings.poll_interval
        )
        if request.query_params.get("format") == "text":
            return PlainTextResponse(messages.as_text(result))
        return JSONResponse(result)

    return handler


def q(request: Request) -> dict[str, Any]:
    return dict(request.query_params)


def path(request: Request, name: str) -> str:
    return str(request.path_params[name])


def task_id(request: Request) -> int:
    return v.integer(request.path_params["task_id"], "task_id", lo=1)


# -- unauthenticated -------------------------------------------------------------


async def health(request: Request) -> Response:
    """Shallow by default (process up). `?deep=1` also checks SQLite read/write and the reconciler heartbeat;
    deployment/healthcheck.sh uses it and only pings the external monitor when it is ok."""
    base = {"ok": True, "service": "agent-office", "version": __version__}
    if request.query_params.get("deep") not in ("1", "true"):
        return JSONResponse(base)
    hub = await run_in_threadpool(get_hub)
    deep = await run_in_threadpool(recovery.health, hub.db, hub.settings)
    return JSONResponse({**base, **deep, "mode": hub.settings.mode}, status_code=200 if deep["ok"] else 503)


async def agent_guide(_: Request) -> Response:
    return PlainTextResponse((STATIC / "agent-guide.md").read_text("utf-8"), media_type="text/markdown; charset=utf-8")


async def github_webhook(request: Request) -> Response:
    hub = await run_in_threadpool(get_hub)
    signature = request.headers.get("x-hub-signature-256")
    # Reachable from the Internet (webhook Funnel): refuse unsigned requests before buffering up to 5 MiB of body.
    github.verify_signature(hub.settings.github_webhook_secret, b"", signature, header_only=True)
    raw = await read_body(request, 5 * 1024 * 1024)  # GitHub caps payloads at 25 MB; we only need summaries
    github.verify_signature(hub.settings.github_webhook_secret, raw, signature)
    try:
        payload = json.loads(raw)
    except ValueError:
        raise BadRequest("invalid JSON") from None
    event = request.headers.get("x-github-event", "")
    delivery = request.headers.get("x-github-delivery", "")[:64]
    result = await run_in_threadpool(github.ingest, hub.db, event, delivery, v.obj(payload), hub.notifier)
    return JSONResponse(result)


# -- export (streamed) -----------------------------------------------------------


def _export(h: Hub, p: Principal, r: Request, _: Any) -> Response:
    upto = v.integer(r.query_params.get("upto_id"), "upto_id", lo=1)
    p.require_admin()
    return StreamingResponse(archive.export_lines(h.db, p, upto), media_type="application/x-ndjson")


# -- websocket tickets ---------------------------------------------------------------
# Browsers cannot set Authorization on a WebSocket, and permanent tokens must not travel in URLs, so the
# dashboard trades its bearer token for a single-use ticket valid for 30 s (in-process: one backend).

_TICKETS: dict[str, tuple[Principal, float]] = {}
TICKET_TTL = 30.0


def issue_ticket(p: Principal) -> dict[str, Any]:
    import secrets
    import time

    now = time.monotonic()
    for k, (_, exp) in list(_TICKETS.items()):
        if exp < now:
            _TICKETS.pop(k, None)
    t = "wst_" + secrets.token_urlsafe(24)
    _TICKETS[t] = (p, now + TICKET_TTL)
    return {"ticket": t, "expires_in": TICKET_TTL}


def redeem_ticket(ticket: str | None) -> Principal | None:
    import time

    if not ticket:
        return None
    item = _TICKETS.pop(ticket, None)
    if item is None or item[1] < time.monotonic():
        return None
    return item[0]


def admin_reconcile(h: Hub, p: Principal) -> dict[str, Any]:
    p.require_admin()
    stats = recovery.reconcile(h.db, h.settings)
    h.notifier.bump()
    return stats


# -- route table -------------------------------------------------------------------


def W(fn: Handler) -> Callable[[Request], Awaitable[Response]]:
    return endpoint(fn, write=True)


def R(fn: Handler) -> Callable[[Request], Awaitable[Response]]:
    return endpoint(fn, write=False)


def _coord_op(fn: Callable[..., Any]) -> Callable[..., Any]:
    return lambda h, p, r, b: fn(h.db, p, path(r, "project"), b, h.settings)


routes = [
    Route("/api/health", health),
    Route("/api/agent-guide", agent_guide),
    Route("/api/github/webhook", github_webhook, methods=["POST"]),
    Route("/api/me", R(lambda h, p, r, b: directory.whoami(h.db, p))),
    Route("/api/channels", R(lambda h, p, r, b: directory.list_channels(h.db, p))),
    Route("/api/channels", W(lambda h, p, r, b: directory.create_channel(h.db, p, v.obj(b))), methods=["POST"]),
    Route("/api/channels/{ch}", R(lambda h, p, r, b: directory.get_channel(h.db, p, path(r, "ch")))),
    Route(
        "/api/channels/{ch}/members",
        W(lambda h, p, r, b: directory.set_membership(h.db, p, path(r, "ch"), v.obj(b).get("agent"), True)),
        methods=["POST"],
    ),
    Route(
        "/api/channels/{ch}/members/{agent}",
        W(lambda h, p, r, b: directory.set_membership(h.db, p, path(r, "ch"), path(r, "agent"), False)),
        methods=["DELETE"],
    ),
    Route(
        "/api/channels/{ch}/messages", polled(lambda h, p, r: messages.channel_messages(h.db, p, path(r, "ch"), q(r)))
    ),
    Route(
        "/api/channels/{ch}/messages",
        W(lambda h, p, r, b: messages.post_message(h.db, p, path(r, "ch"), b, notifier=h.notifier)),
        methods=["POST"],
    ),
    Route("/api/feed", polled(lambda h, p, r: messages.feed(h.db, p, q(r)))),
    Route("/api/inbox", polled(lambda h, p, r: messages.inbox(h.db, p, q(r)))),
    Route("/api/channels/{ch}/tasks", R(lambda h, p, r, b: coordination.list_tasks(h.db, p, path(r, "ch"), q(r)))),
    Route(
        "/api/channels/{ch}/tasks",
        W(lambda h, p, r, b: coordination.create_task(h.db, p, path(r, "ch"), b, h.notifier)),
        methods=["POST"],
    ),
    Route("/api/tasks/{task_id}", R(lambda h, p, r, b: coordination.get_task(h.db, p, task_id(r)))),
    Route(
        "/api/tasks/{task_id}",
        W(lambda h, p, r, b: coordination.update_task(h.db, p, task_id(r), b, h.notifier, h.settings)),
        methods=["PATCH"],
    ),
    Route(
        "/api/tasks/{task_id}/claim",
        W(lambda h, p, r, b: coordination.claim_task(h.db, p, task_id(r), h.notifier, b, h.settings)),
        methods=["POST"],
    ),
    Route(
        "/api/tasks/{task_id}/renew",
        W(lambda h, p, r, b: coordination.renew_task(h.db, p, task_id(r), b, h.settings)),
        methods=["POST"],
    ),
    Route(
        "/api/tasks/{task_id}/release",
        W(lambda h, p, r, b: coordination.release_task(h.db, p, task_id(r), b, h.notifier)),
        methods=["POST"],
    ),
    Route("/api/tasks/{task_id}/checkpoints", R(lambda h, p, r, b: coordination.list_checkpoints(h.db, p, task_id(r)))),
    Route(
        "/api/tasks/{task_id}/checkpoints",
        W(lambda h, p, r, b: coordination.add_checkpoint(h.db, p, task_id(r), b, h.notifier)),
        methods=["POST"],
    ),
    Route("/api/tasks/{task_id}/lease", R(lambda h, p, r, b: coordination.lease_status(h.db, p, task_id(r), q(r)))),
    # projects & coordinator
    Route("/api/projects", R(lambda h, p, r, b: projects.list_projects(h.db, p))),
    Route("/api/projects", W(lambda h, p, r, b: projects.create_project(h.db, p, b)), methods=["POST"]),
    Route("/api/projects/{project}", R(lambda h, p, r, b: projects.get_project(h.db, p, path(r, "project")))),
    Route(
        "/api/projects/{project}/members",
        W(lambda h, p, r, b: projects.set_project_member(h.db, p, path(r, "project"), b, True)),
        methods=["POST"],
    ),
    Route(
        "/api/projects/{project}/members/{agent}",
        W(
            lambda h, p, r, b: projects.set_project_member(
                h.db, p, path(r, "project"), {"agent": path(r, "agent")}, False
            )
        ),
        methods=["DELETE"],
    ),
    Route(
        "/api/projects/{project}/coordinator", R(lambda h, p, r, b: coordinator.get_state(h.db, p, path(r, "project")))
    ),
    *[
        Route(
            f"/api/projects/{{project}}/coordinator/{op}",
            W(_coord_op(fn)),
            methods=["POST"],
        )
        for op, fn in (
            ("designate", coordinator.designate),
            ("accept", coordinator.accept),
            ("decline", coordinator.decline),
            ("resign", coordinator.resign),
            ("assign", coordinator.assign),
        )
    ],
    # sessions & devices
    Route("/api/sessions", R(lambda h, p, r, b: sessions.list_sessions(h.db, p, q(r), h.settings))),
    Route("/api/sessions", W(lambda h, p, r, b: sessions.register_session(h.db, p, b, h.settings)), methods=["POST"]),
    Route(
        "/api/sessions/{sid}/heartbeat",
        W(lambda h, p, r, b: sessions.heartbeat_session(h.db, p, path(r, "sid"), b, h.settings)),
        methods=["POST"],
    ),
    Route(
        "/api/sessions/{sid}/end",
        W(lambda h, p, r, b: sessions.end_session(h.db, p, path(r, "sid"), b, h.settings)),
        methods=["POST"],
    ),
    Route("/api/devices", R(lambda h, p, r, b: sessions.list_devices(h.db, p))),
    Route("/api/devices/heartbeat", W(lambda h, p, r, b: sessions.device_heartbeat(h.db, p, b)), methods=["POST"]),
    # deliveries
    Route("/api/deliveries", W(lambda h, p, r, b: delivery.report(h.db, p, b)), methods=["POST"]),
    Route("/api/deliveries/pending", R(lambda h, p, r, b: delivery.pending(h.db, p, q(r)))),
    Route(
        "/api/messages/{message_id}/deliveries",
        R(lambda h, p, r, b: delivery.status(h.db, p, v.integer(r.path_params["message_id"], "message_id", lo=1))),
    ),
    # events & read model
    Route("/api/events", polled(lambda h, p, r: events.list_events(h.db, p, q(r)))),
    Route("/api/office/snapshot", R(lambda h, p, r, b: office.snapshot(h.db, p, q(r), h.settings))),
    Route("/api/board/projects", R(lambda h, p, r, b: board.admin_projects(h.db, p))),
    Route("/api/board", R(lambda h, p, r, b: board.board(h.db, p, q(r), h.settings))),
    Route("/api/office/building", R(lambda h, p, r, b: office.building(h.settings))),
    Route("/api/ws-ticket", W(lambda h, p, r, b: issue_ticket(p)), methods=["POST"]),
    Route("/api/channels/{ch}/leases", R(lambda h, p, r, b: coordination.list_leases(h.db, p, path(r, "ch")))),
    Route(
        "/api/channels/{ch}/leases",
        W(lambda h, p, r, b: coordination.acquire_lease(h.db, p, path(r, "ch"), b, h.notifier)),
        methods=["POST"],
    ),
    Route(
        "/api/channels/{ch}/leases",
        W(
            lambda h, p, r, b: coordination.release_lease(
                h.db, p, path(r, "ch"), r.query_params.get("resource"), h.notifier
            )
        ),
        methods=["DELETE"],
    ),
    Route(
        "/api/channels/{ch}/kv",
        R(lambda h, p, r, b: coordination.kv_list(h.db, p, path(r, "ch"), r.query_params.get("prefix", ""))),
    ),
    Route(
        "/api/channels/{ch}/kv/{key:path}",
        R(lambda h, p, r, b: coordination.kv_get(h.db, p, path(r, "ch"), path(r, "key"))),
    ),
    Route(
        "/api/channels/{ch}/kv/{key:path}",
        W(lambda h, p, r, b: coordination.kv_set(h.db, p, path(r, "ch"), path(r, "key"), b, h.notifier)),
        methods=["PUT"],
    ),
    Route("/api/presence", R(lambda h, p, r, b: coordination.presence(h.db, p))),
    Route("/api/presence", W(lambda h, p, r, b: coordination.heartbeat(h.db, p, b)), methods=["POST"]),
    Route("/api/stats", R(lambda h, p, r, b: archive.stats(h.db, p))),
    # admin
    Route("/api/admin/agents", R(lambda h, p, r, b: directory.list_agents(h.db, p))),
    Route("/api/admin/agents", W(lambda h, p, r, b: directory.create_agent(h.db, p, v.obj(b))), methods=["POST"]),
    Route(
        "/api/admin/agents/{name}/tokens",
        W(lambda h, p, r, b: directory.issue_token(h.db, p, path(r, "name"), v.obj(b))),
        methods=["POST"],
    ),
    Route(
        "/api/admin/agents/{name}/disable",
        W(lambda h, p, r, b: directory.set_agent_disabled(h.db, p, path(r, "name"), True)),
        methods=["POST"],
    ),
    Route(
        "/api/admin/agents/{name}/enable",
        W(lambda h, p, r, b: directory.set_agent_disabled(h.db, p, path(r, "name"), False)),
        methods=["POST"],
    ),
    Route(
        "/api/admin/agents/{name}",
        W(
            lambda h, p, r, b: directory.delete_agent(
                h.db, p, path(r, "name"), r.query_params.get("purge_messages") in ("1", "true")
            )
        ),
        methods=["DELETE"],
    ),
    Route(
        "/api/admin/tokens/{token_id}",
        W(lambda h, p, r, b: directory.revoke_token(h.db, p, v.integer(r.path_params["token_id"], "token_id", lo=1))),
        methods=["DELETE"],
    ),
    Route(
        "/api/admin/messages/{message_id}",
        W(
            lambda h, p, r, b: messages.delete_message(
                h.db, p, v.integer(r.path_params["message_id"], "message_id", lo=1)
            )
        ),
        methods=["DELETE"],
    ),
    Route(
        "/api/admin/channels/{ch}",
        W(lambda h, p, r, b: directory.delete_channel(h.db, p, path(r, "ch"))),
        methods=["DELETE"],
    ),
    Route("/api/admin/audit", R(lambda h, p, r, b: directory.audit_log(h.db, p))),
    Route(
        "/api/admin/agents/{name}",
        W(lambda h, p, r, b: directory.update_agent(h.db, p, path(r, "name"), v.obj(b))),
        methods=["PATCH"],
    ),
    Route("/api/admin/reconcile", W(lambda h, p, r, b: admin_reconcile(h, p)), methods=["POST"]),
    Route(
        "/api/admin/archive/plan", R(lambda h, p, r, b: archive.plan(h.db, p, r.query_params.get("older_than_days")))
    ),
    Route("/api/admin/archive/export", R(_export)),
    Route("/api/admin/archive/prune", W(lambda h, p, r, b: archive.prune(h.db, p, b)), methods=["POST"]),
]


async def app_error(_: Request, exc: Exception) -> Response:
    assert isinstance(exc, AppError)
    headers = {}
    if exc.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if "retry_after" in exc.extra:
        headers["Retry-After"] = str(exc.extra["retry_after"])
    return JSONResponse(exc.to_dict(), status_code=exc.status, headers=headers)


async def internal_error(_: Request, exc: Exception) -> Response:
    log.exception("unhandled error", exc_info=exc)
    return JSONResponse({"error": "internal", "message": "internal server error"}, status_code=500)
