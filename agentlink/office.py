"""Read model for the dashboards: one consistent snapshot of a project's live state, plus the building
configuration (floors = specializations). The 3D view and /chat both consume exactly this + /api/events.

Floors are assigned here, from the session's declared specialization (not its owner), so the mapping is
testable and identical for every client.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import Settings
from .coordinator import _row as coord_row
from .coordinator import state_out
from .db import Database
from .events import head, scoped_monitor
from .projects import project_for
from .security import Principal, now_ms
from .sessions import SESSION_SELECT, session_out

DEFAULT_BUILDING: dict[str, Any] = {
    "title": "Agent Office",
    # Listed top floor first. `match` = specializations that work on that floor; one floor is the fallback.
    "floors": [
        {
            "key": "coordination",
            "name": "Coordinación",
            "color": "naranja",
            "match": ["coordination", "coordinator", "lead", "pm"],
        },
        {
            "key": "qa",
            "name": "Seguridad / QA",
            "color": "rojo",
            "match": ["security", "qa", "test", "testing", "review", "reviewer"],
        },
        {
            "key": "devops",
            "name": "DevOps / Infraestructura",
            "color": "verde",
            "match": ["devops", "infra", "infrastructure", "ops", "sre"],
        },
        {
            "key": "backend",
            "name": "Backend / APIs",
            "color": "azul",
            "match": ["backend", "api", "apis", "database", "data"],
            "fallback": True,
        },
        {
            "key": "frontend",
            "name": "Frontend / UX",
            "color": "morado",
            "match": ["frontend", "ux", "ui", "design", "web"],
        },
    ],
}


def building(settings: Settings) -> dict[str, Any]:
    if settings.office_config:
        data = json.loads(Path(settings.office_config).read_text("utf-8"))
        if not isinstance(data.get("floors"), list) or not data["floors"]:
            raise ValueError("office config needs a non-empty 'floors' list")
        return {**DEFAULT_BUILDING, **data}
    return DEFAULT_BUILDING


def floor_for(spec: str, floors: list[dict[str, Any]]) -> str:
    s = (spec or "").strip().lower()
    for f in floors:
        if s and (s == f["key"] or s in [m.lower() for m in f.get("match", [])]):
            return str(f["key"])
    fb = next((f for f in floors if f.get("fallback")), floors[-1])
    return str(fb["key"])


def snapshot(db: Database, p: Principal, q: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Everything the dashboards need, read inside one transaction so it is internally consistent.

    `cursor` is the event id the snapshot corresponds to: replay /api/events?after=cursor to stay current.
    """
    b = building(settings)
    now = now_ms()
    with db.transaction() as tx:
        if q.get("project"):
            projects = [project_for(tx, p, q["project"])]
        elif p.is_admin or (p.role == "monitor" and not scoped_monitor(tx, p)):
            projects = tx.query("SELECT * FROM projects ORDER BY name")
        else:
            projects = tx.query(
                "SELECT pr.* FROM projects pr JOIN project_members pm ON pm.project_id = pr.id WHERE pm.agent_id = ? ORDER BY pr.name",
                (p.agent_id,),
            )
        ids = [int(x["id"]) for x in projects]
        cursor = head(tx)
        if not ids:
            return {"server_time": now, "cursor": cursor, "building": b, "projects": [], "sessions": [], "tasks": [],
                    "leases": [], "coordinators": {}}  # fmt: skip
        ph = ",".join("?" * len(ids))
        recent = now - 15 * 60_000
        srows = tx.query(
            f"{SESSION_SELECT} WHERE s.project_id IN ({ph}) AND (s.ended_at IS NULL OR s.ended_at >= ?) ORDER BY s.created_at",
            (*ids, recent),
        )
        sessions = []
        for r in srows:
            out = session_out(r, now, settings)
            out["floor"] = floor_for(out["specialization"], b["floors"])
            sessions.append(out)
        trows = tx.query(
            "SELECT t.id, t.title, t.status, t.priority, t.lease_epoch, t.lease_expires_at, t.assignee_session, t.block_reason, "
            "t.updated_at, c.name AS channel, pr.name AS project, a.name AS assignee FROM tasks t JOIN channels c ON c.id = t.channel_id "
            f"JOIN projects pr ON pr.id = t.project_id LEFT JOIN agents a ON a.id = t.assignee_id WHERE t.project_id IN ({ph}) "
            "AND (t.status NOT IN ('done', 'cancelled', 'failed') OR t.updated_at >= ?) ORDER BY t.priority, t.id LIMIT 500",
            (*ids, now - 3_600_000),
        )
        leases = tx.query(
            "SELECT l.resource, l.expires_at, l.session_id, l.task_id, a.name AS holder, c.name AS channel FROM leases l "
            f"JOIN channels c ON c.id = l.channel_id JOIN agents a ON a.id = l.holder_id WHERE c.project_id IN ({ph}) AND l.expires_at > ?",
            (*ids, now),
        )
        coords = {}
        for pr in projects:
            c = coord_row(tx, int(pr["id"]))
            coords[pr["name"]] = state_out(c, now)
        devices = tx.query(  # only devices of participants in the projects this reader can see
            "SELECT owner, name, last_seen, supervisor_seen FROM devices WHERE owner IN (SELECT a.owner FROM agents a "
            f"JOIN project_members pm ON pm.agent_id = a.id WHERE pm.project_id IN ({ph})) ORDER BY owner, name",
            tuple(ids),
        )
    return {
        "server_time": now,
        "cursor": cursor,
        "mode": settings.mode,
        "building": b,
        "projects": [{"name": x["name"], "repo": x["repo"]} for x in projects],
        "sessions": sessions,
        "tasks": trows,
        "leases": leases,
        "coordinators": coords,
        "devices": devices,
    }
