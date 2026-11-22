"""Read model for /tareas: the current work of one project, for its human administrators.

One response holds what the page needs: open tasks, the open coordination items that are messages (handoffs,
alerts, questions, reviews and urgent system notices), the project's channels, agents, live sessions and its
coordinator. Every change is made through the existing endpoints (tasks, messages, coordinator), so the same
rules, events and notifications apply whoever makes it.

A message item stays "current" until it is resolved: a question by an `answer` reply, any item by a reply whose
data carries ``{"resolves": <id>}``, or when every recipient has processed it. Older than ITEM_WINDOW_MS, it is
no longer shown (the chat keeps the full history).
"""

from __future__ import annotations

import json
from typing import Any

from . import validation as v
from .config import Settings
from .coordination import _TASK_SELECT, TERMINAL, TRANSITIONS, _deps, _task_out
from .coordinator import _row as coord_row
from .coordinator import state_out
from .db import Database
from .directory import project_admin
from .errors import Forbidden
from .messages import iso
from .projects import project_for
from .security import Principal, now_ms
from .sessions import SESSION_SELECT, session_out

ITEM_KINDS = ("handoff", "alert", "question", "review")
ITEM_WINDOW_MS = 7 * 86_400_000
MAX_ITEMS = 300


def require_board_admin(db: Database, p: Principal, project: Any) -> dict[str, Any]:
    """The board is for administrators: a global admin or an admin of this project. Monitors only read the chat."""
    if p.role == "monitor":
        raise Forbidden("the task manager is for project administrators")
    pr = project_for(db, p, v.name(project, "project"))
    if not project_admin(db, p, pr["id"]):
        raise Forbidden(f"project admin role required in '{pr['name']}'")
    return pr


def _items(tx: Any, project_id: int, now: int) -> list[dict[str, Any]]:
    rows = tx.query(
        "SELECT m.id, m.kind, m.body, m.data, m.priority, m.created_at, m.thread_id, c.name AS channel, a.name AS author "
        "FROM messages m JOIN channels c ON c.id = m.channel_id JOIN agents a ON a.id = m.author_id "
        f"WHERE c.project_id = ? AND m.created_at >= ? AND (m.kind IN ({','.join('?' * len(ITEM_KINDS))}) "
        "OR (m.kind = 'system' AND m.priority = 1)) ORDER BY m.id DESC LIMIT ?",
        (project_id, now - ITEM_WINDOW_MS, *ITEM_KINDS, MAX_ITEMS),
    )
    if not rows:
        return []
    ids = [int(r["id"]) for r in rows]
    ph = ",".join("?" * len(ids))
    resolved: set[int] = set()
    for r in tx.query(
        f"SELECT reply_to, thread_id, kind, data FROM messages WHERE reply_to IN ({ph}) OR thread_id IN ({ph})",
        (*ids, *ids),
    ):
        data = json.loads(r["data"]) if r["data"] else {}
        target = data.get("resolves") if isinstance(data, dict) else None
        if isinstance(target, int):
            resolved.add(target)
        elif r["kind"] == "answer" and r["reply_to"]:
            resolved.add(int(r["reply_to"]))
    to: dict[int, list[dict[str, Any]]] = {}
    for r in tx.query(
        "SELECT mn.message_id, a.name, COALESCE(d.state, 'queued') AS state FROM mentions mn "
        "JOIN agents a ON a.id = mn.agent_id LEFT JOIN deliveries d ON d.message_id = mn.message_id "
        f"AND d.agent_id = mn.agent_id WHERE mn.message_id IN ({ph}) ORDER BY a.name",
        tuple(ids),
    ):
        to.setdefault(int(r["message_id"]), []).append({"agent": r["name"], "state": r["state"]})
    out = []
    for r in rows:
        mid = int(r["id"])
        rec = to.get(mid, [])
        if mid in resolved or (rec and all(x["state"] == "processed" for x in rec)):
            continue
        data = json.loads(r["data"]) if r["data"] else None
        if r["kind"] == "system" and isinstance(data, dict) and data.get("event") == "coordinator.offered":
            continue  # a succession offer is shown as the coordinator's state, not as a separate item
        seen = any(x["state"] in ("acknowledged", "processed") for x in rec)
        out.append({
            "id": mid,
            "type": "alert" if r["kind"] == "system" else r["kind"],
            "body": r["body"],
            "channel": r["channel"],
            "priority": r["priority"],
            "author": r["author"],
            "to": rec,
            "status": "acknowledged" if seen else "pending",
            "thread_id": r["thread_id"] or mid,
            "created": iso(int(r["created_at"])),
        })  # fmt: skip
    return out


def board(db: Database, p: Principal, q: dict[str, Any], settings: Settings) -> dict[str, Any]:
    pr = require_board_admin(db, p, q.get("project"))
    pid = int(pr["id"])
    now = now_ms()
    with db.transaction() as tx:
        channels = tx.query("SELECT name, description FROM channels WHERE project_id = ? ORDER BY name", (pid,))
        agents = tx.query(
            "SELECT a.name, a.owner, a.specialization, pm.role AS project_role FROM project_members pm "
            "JOIN agents a ON a.id = pm.agent_id WHERE pm.project_id = ? AND a.disabled = 0 ORDER BY a.name",
            (pid,),
        )
        sessions = [
            session_out(r, now, settings)
            for r in tx.query(
                f"{SESSION_SELECT} WHERE s.project_id = ? AND s.ended_at IS NULL AND s.parent_id IS NULL "
                "ORDER BY a.name, s.created_at",
                (pid,),
            )
        ]
        c = coord_row(tx, pid)
        coord = state_out(c, now)
        sid = coord["session_id"] or coord["offered_session"]
        who = (
            tx.one("SELECT a.name FROM sessions s JOIN agents a ON a.id = s.agent_id WHERE s.id = ?", (sid,))
            if sid
            else None
        )
        coord["agent"] = who["name"] if who else None
        rows = tx.query(
            f"{_TASK_SELECT} WHERE t.project_id = ? AND t.status NOT IN ({','.join('?' * len(TERMINAL))}) "
            "ORDER BY t.priority, t.id LIMIT 500",
            (pid, *TERMINAL),
        )
        deps = _deps(tx, [int(r["id"]) for r in rows])
        tasks = [_task_out(r, deps.get(int(r["id"]))) for r in rows]
        items = _items(tx, pid, now)
    return {
        "project": {"name": pr["name"], "repo": pr["repo"], "description": pr["description"]},
        "server_time": now,
        "me": {"agent": p.name, "role": p.role},
        "channels": channels,
        "agents": agents,
        "sessions": [s for s in sessions if s["status"] not in ("offline", "terminated")],
        "coordinator": coord,
        "tasks": tasks,
        "items": items,
        "transitions": TRANSITIONS,
        "kinds": list(v.POSTABLE_KINDS),
    }


def admin_projects(db: Database, p: Principal) -> list[dict[str, Any]]:
    """Projects this principal administers (the page's project selector), with their number of open tasks."""
    if p.role == "monitor":
        return []
    ph = ",".join("?" * len(TERMINAL))
    count = f"(SELECT COUNT(*) FROM tasks t WHERE t.project_id = pr.id AND t.status NOT IN ({ph})) AS open_tasks"
    if p.is_admin:
        return db.query(f"SELECT pr.name, pr.repo, {count} FROM projects pr ORDER BY pr.name", TERMINAL)
    return db.query(
        f"SELECT pr.name, pr.repo, {count} FROM projects pr JOIN project_members pm ON pm.project_id = pr.id "
        "WHERE pm.agent_id = ? AND pm.role = 'admin' ORDER BY pr.name",
        (*TERMINAL, p.agent_id),
    )
