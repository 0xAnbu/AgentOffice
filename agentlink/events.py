"""Durable event log: the single ordered stream the dashboards (and any client) can replay by cursor.

Rule: a state change and its event are written in the SAME transaction (`emit(tx, ...)`), so a crash
between the two can never leave a committed change without its event (see tests/test_consistency.py).
WebSockets only *notify* that new rows exist; they are never the source of truth.
"""

from __future__ import annotations

import json
from typing import Any

from . import validation as v
from .db import Database, Executor, Row
from .security import Principal, now_ms

# Persisted event types. The dashboard maps them to visual categories; nothing parses free text.
TYPES = (
    "message.created",
    "task.created",
    "task.claimed",
    "task.updated",
    "task.renewed",
    "task.released",
    "task.orphaned",
    "task.recovered",
    "task.blocked",
    "task.unblocked",
    "task.checkpoint",
    "task.stale_write",
    "task.wait_timeout",
    "lease.acquired",
    "lease.released",
    "lease.expired",
    "session.registered",
    "session.ended",
    "session.expired",
    "session.stalled",
    "session.resumed",
    "agent.online",
    "agent.offline",
    "coordinator.offered",
    "coordinator.accepted",
    "coordinator.declined",
    "coordinator.offer_expired",
    "coordinator.lost",
    "coordinator.unavailable",
    "coordinator.released",
    "delivery.overdue",
    "project.created",
    "kv.set",
)


def emit(
    tx: Executor,
    type_: str,
    *,
    project_id: int | None,
    channel_id: int | None = None,
    actor_id: int | None = None,
    session_id: str | None = None,
    task_id: int | None = None,
    message_id: int | None = None,
    data: dict[str, Any] | None = None,
) -> int:
    """Append one event inside the caller's transaction and return its id."""
    assert type_ in TYPES, type_
    row = tx.one(
        "INSERT INTO events(project_id, channel_id, type, actor_id, session_id, task_id, message_id, data, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (
            project_id,
            channel_id,
            type_,
            actor_id,
            session_id,
            task_id,
            message_id,
            json.dumps(data, separators=(",", ":"), ensure_ascii=False) if data else None,
            now_ms(),
        ),
    )
    assert row is not None
    return int(row["id"])


def scoped_monitor(db: Executor, p: Principal) -> bool:
    """A monitor with explicit project memberships only sees those projects; without any it sees all (legacy)."""
    if p.role != "monitor":
        return False
    return db.one("SELECT 1 AS x FROM project_members WHERE agent_id = ?", (p.agent_id,)) is not None


def visibility_sql(db: Executor, p: Principal, alias: str = "e") -> tuple[str, tuple[Any, ...]]:
    """Events a principal may read: channel events of its channels, project events of its projects."""
    if p.is_admin or (p.role == "monitor" and not scoped_monitor(db, p)):
        return "1 = 1", ()
    return (
        f"(({alias}.channel_id IS NOT NULL AND {alias}.channel_id IN "
        f"(SELECT mb.channel_id FROM members mb WHERE mb.agent_id = ?)) OR "
        f"({alias}.channel_id IS NULL AND {alias}.project_id IN "
        f"(SELECT pm.project_id FROM project_members pm WHERE pm.agent_id = ?)))",
        (p.agent_id, p.agent_id),
    )


def _out(r: Row) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": r["id"],
        "type": r["type"],
        "ts": r["created_at"],
        "project": r["project"],
        "channel": r["channel"],
        "actor": r["actor"],
    }
    for k in ("session_id", "task_id", "message_id"):
        if r[k] is not None:
            out[k] = r[k]
    if r["data"]:
        out["data"] = json.loads(r["data"])
    return out


_SELECT = (
    "SELECT e.*, p.name AS project, c.name AS channel, a.name AS actor FROM events e "
    "LEFT JOIN projects p ON p.id = e.project_id LEFT JOIN channels c ON c.id = e.channel_id "
    "LEFT JOIN agents a ON a.id = e.actor_id "
)


def list_events(db: Database, p: Principal, q: dict[str, Any]) -> dict[str, Any]:
    """Cursor read. `after` = last id seen. If the cursor predates retained history, `resync` tells the
    client to rebuild from /api/office/snapshot instead of trusting a gap."""
    after = v.integer(q.get("after"), "after", default=0)
    limit = v.integer(q.get("limit"), "limit", default=200, lo=1, hi=1000)
    where, params = visibility_sql(db, p)
    extra = ""
    extra_params: list[Any] = []
    if q.get("project"):
        extra += " AND p.name = ?"
        extra_params.append(v.name(q["project"], "project"))
    if q.get("types"):
        types = [t for t in str(q["types"]).split(",") if t]
        for t in types:
            v.choice(t, "types", TYPES)
        extra += f" AND e.type IN ({','.join('?' * len(types))})"
        extra_params += types
    rows = db.query(
        f"{_SELECT} WHERE {where} AND e.id > ?{extra} ORDER BY e.id LIMIT ?",
        (*params, after, *extra_params, limit),
    )
    bounds = db.one("SELECT MIN(id) AS lo, MAX(id) AS hi FROM events")
    lo = int((bounds or {}).get("lo") or 0)
    hi = int((bounds or {}).get("hi") or 0)
    events = [_out(r) for r in rows]
    return {
        "events": events,
        "next_after": events[-1]["id"] if events else max(after, 0) if after <= hi else hi,
        "has_more": len(events) == limit,
        "resync": bool(after and lo and after < lo - 1),
        "head": hi,
    }


def head(db: Executor) -> int:
    row = db.one("SELECT MAX(id) AS hi FROM events")
    return int((row or {}).get("hi") or 0)
