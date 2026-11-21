"""Coordination primitives that stop agents stepping on each other:
tasks with atomic claims, renewable task leases with fencing (lease_epoch), dependencies,
checkpoints, path leases with TTL bound to session/task, shared versioned key-value context,
and presence heartbeats.

Every state change commits together with its durable event and its ``system`` message
(one transaction), so an agent following the message stream and a dashboard following the event
log both see exactly the transitions that happened.

Task state machine (persisted ``status``)::

    open ──claim──▶ claimed ──▶ in_progress ──▶ review ──▶ done
      ▲               │  │          │   │                    │
      │               │  └──────────┴───┴──▶ blocked ◀───────┤ (reopen by creator/admin)
      │               └──▶ failed / cancelled                 │
      └── recovered ◀── orphaned ◀── lease expired / session gone

Fencing: a claim increments ``lease_epoch``. Every holder write must quote the current epoch
and session and arrive before ``lease_expires_at`` (server clock); otherwise 409 ``stale_lease``
and nothing changes.
"""

from __future__ import annotations

import json
from typing import Any

from . import validation as v
from .config import Settings
from .db import Database, Executor, Row
from .directory import agent_by_name, channel_for
from .errors import BadRequest, Conflict, Forbidden, NotFound
from .events import emit
from .messages import Notifier, insert_message, iso
from .security import Principal, now_ms

ONLINE_WINDOW_MS = 120_000
ACTIVE = ("claimed", "in_progress", "blocked", "review")  # statuses that may hold a lease
TERMINAL = ("done", "cancelled", "failed")
CHECKPOINT_KEYS = {
    "summary", "done", "pending", "branch", "commit", "base_commit", "worktree", "files", "depends",
    "tests", "risks", "notes",
}  # fmt: skip
# Allowed status transitions via PATCH. Claims go through claim_task; orphaning through the reconciler.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "open": ("blocked", "cancelled"),
    "claimed": ("in_progress", "blocked", "review", "done", "failed", "cancelled", "open"),
    "in_progress": ("blocked", "review", "done", "failed", "cancelled", "open"),
    "blocked": ("in_progress", "review", "done", "failed", "cancelled", "open"),
    "review": ("in_progress", "done", "failed", "cancelled", "open", "blocked"),
    "orphaned": ("open", "cancelled"),
    "done": ("open",),
    "failed": ("open", "cancelled"),
    "cancelled": ("open",),
}
HOLDER_ONLY = ("in_progress", "review", "done", "failed")  # need the lease when the task is leased


def sys_message(
    tx: Executor, p: Principal | None, ch: Row, body: str, data: dict[str, Any], mentions: list[str] | None = None
) -> None:
    """System message inside the caller's transaction. `p=None` => authored by the server (admin identity)."""
    author = p or _server_principal(tx)
    insert_message(tx, author, ch, {"kind": "system", "body": body, "data": data, "mentions": mentions}, system=True)


def _server_principal(tx: Executor) -> Principal:
    from .security import ADMIN_AGENT, ensure_admin_agent

    row = tx.one("SELECT id FROM agents WHERE name = ?", (ADMIN_AGENT,))
    aid = int(row["id"]) if row else ensure_admin_agent(tx)  # type: ignore[arg-type]
    return Principal(aid, ADMIN_AGENT, "admin", None)


# -- tasks -----------------------------------------------------------------------


def _deps(db: Executor, task_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not task_ids:
        return {}
    ph = ",".join("?" * len(task_ids))
    out: dict[int, list[dict[str, Any]]] = {}
    for r in db.query(
        f"SELECT d.task_id, d.depends_on, t.status FROM task_deps d JOIN tasks t ON t.id = d.depends_on "
        f"WHERE d.task_id IN ({ph}) ORDER BY d.depends_on",
        tuple(task_ids),
    ):
        out.setdefault(int(r["task_id"]), []).append({"id": r["depends_on"], "status": r["status"]})
    return out


def _task_out(r: Row, deps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    deps = deps or []
    now = now_ms()
    leased = r["assignee_session"] is not None or (r["lease_expires_at"] is not None and r["status"] in ACTIVE)
    out = {
        "id": r["id"],
        "channel": r["channel"],
        "project": r.get("project"),
        "title": r["title"],
        "description": r["description"],
        "status": r["status"],
        "priority": r["priority"],
        "assignee": r["assignee"],
        "assignee_session": r["assignee_session"],
        "created_by": r["creator"],
        "refs": json.loads(r["refs"]) if r["refs"] else [],
        "version": r["version"],
        "lease_epoch": r["lease_epoch"],
        "lease_expires": iso(int(r["lease_expires_at"])) if r["lease_expires_at"] else None,
        "lease_valid": bool(leased and r["lease_expires_at"] and int(r["lease_expires_at"]) > now),
        "last_progress": iso(int(r["last_progress_at"])) if r["last_progress_at"] else None,
        "block_reason": r["block_reason"],
        "depends_on": [d["id"] for d in deps],
        "ready": all(d["status"] == "done" for d in deps),
        "updated": iso(int(r["updated_at"])),
    }
    return out


_TASK_SELECT = (
    "SELECT t.*, c.name AS channel, pr.name AS project, a.name AS assignee, cr.name AS creator FROM tasks t "
    "JOIN channels c ON c.id = t.channel_id LEFT JOIN projects pr ON pr.id = t.project_id "
    "LEFT JOIN agents a ON a.id = t.assignee_id JOIN agents cr ON cr.id = t.created_by "
)


def _task_row(db: Executor, task_id: int) -> Row:
    row = db.one(f"{_TASK_SELECT} WHERE t.id = ?", (task_id,))
    if row is None:
        raise NotFound(f"task #{task_id} not found")
    return row


def _shape(db: Executor, row: Row) -> dict[str, Any]:
    return _task_out(row, _deps(db, [int(row["id"])]).get(int(row["id"])))


def get_task(db: Executor, p: Principal, task_id: int) -> dict[str, Any]:
    row = _task_row(db, task_id)
    channel_for(db, p, int(row["channel_id"]))
    out = _shape(db, row)
    cp = db.one(
        "SELECT created_at, data, lease_epoch FROM checkpoints WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
    )
    if cp:
        out["checkpoint"] = {"at": iso(int(cp["created_at"])), "epoch": cp["lease_epoch"], **json.loads(cp["data"])}
    return out


def _validate_deps(tx: Executor, task_id: int | None, project_id: int, deps: list[int]) -> None:
    """Dependencies must exist in the same project and must not close a cycle (DFS over the stored graph)."""
    for d in deps:
        row = tx.one("SELECT project_id FROM tasks WHERE id = ?", (d,))
        if row is None:
            raise BadRequest(f"dependency task #{d} not found")
        if int(row["project_id"] or 0) != project_id:
            raise BadRequest(f"dependency task #{d} belongs to another project")
        if task_id is not None and d == task_id:
            raise Conflict("a task cannot depend on itself", reason="dependency_cycle")
    if task_id is None:
        return  # a brand-new task has no dependents, so it cannot close a cycle
    stack, seen = list(deps), set()
    while stack:
        cur = stack.pop()
        if cur == task_id:
            raise Conflict(f"dependency cycle through task #{task_id}", reason="dependency_cycle")
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(
            int(r["depends_on"]) for r in tx.query("SELECT depends_on FROM task_deps WHERE task_id = ?", (cur,))
        )


def _dep_list(payload: dict[str, Any]) -> list[int] | None:
    raw = payload.get("depends_on")
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) > v.MAX_LIST:
        raise BadRequest(f"depends_on must be a list of at most {v.MAX_LIST} task ids")
    return sorted({v.integer(x, "depends_on", lo=1) for x in raw})


def create_task(
    db: Database, p: Principal, channel: str | int, payload: dict[str, Any], notifier: Notifier | None = None
) -> dict[str, Any]:
    payload = v.obj(payload)
    ch = channel_for(db, p, channel, write=True)
    title = v.text(payload.get("title"), "title", 200, required=True)
    desc = v.text(payload.get("description"), "description", v.MAX_BODY)
    prio = v.integer(payload.get("priority"), "priority", default=2, lo=1, hi=3)
    refs = v.refs(payload.get("refs"))
    deps = _dep_list(payload) or []
    assignee = payload.get("assignee")
    now = now_ms()
    with db.transaction() as tx:
        assignee_id = int(agent_by_name(tx, v.name(assignee, "assignee"))["id"]) if assignee else None
        _validate_deps(tx, None, int(ch["project_id"]), deps)
        row = tx.one(
            "INSERT INTO tasks(channel_id, project_id, title, description, status, priority, assignee_id, created_by, "
            "refs, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (
                ch["id"],
                ch["project_id"],
                title,
                desc,
                "open",
                prio,
                assignee_id,
                p.agent_id,
                json.dumps(refs) if refs else None,
                now,
                now,
            ),
        )
        assert row is not None
        tid = int(row["id"])
        for d in deps:
            tx.execute("INSERT INTO task_deps(task_id, depends_on) VALUES (?, ?)", (tid, d))
        emit(
            tx,
            "task.created",
            project_id=ch["project_id"],
            channel_id=int(ch["id"]),
            actor_id=p.agent_id,
            task_id=tid,
            data={"title": title, "priority": prio, "assignee": assignee, "depends_on": deps},
        )
        insert_message(
            tx,
            p,
            ch,
            {
                "kind": "task",
                "body": f"task #{tid} (p{prio}): {title}",
                "data": {"task_id": tid, "event": "created", "status": "open", "depends_on": deps},
                "refs": refs or None,
                "mentions": [assignee] if assignee else None,
                "priority": prio,
            },
        )
        task = get_task(tx, p, tid)
    if notifier:
        notifier.bump()
    return task


def list_tasks(db: Database, p: Principal, channel: str | int, q: dict[str, Any]) -> list[dict[str, Any]]:
    ch = channel_for(db, p, channel)
    where, params = "t.channel_id = ?", [ch["id"]]
    status = q.get("status")
    if status:
        statuses = [v.choice(s, "status", v.TASK_STATUSES) for s in str(status).split(",")]
        where += f" AND t.status IN ({','.join('?' * len(statuses))})"
        params += statuses
    elif not q.get("all"):
        where += " AND t.status NOT IN ('done', 'cancelled', 'failed')"
    if q.get("assignee"):
        where += " AND a.name = ?"
        params.append(v.name(q["assignee"], "assignee"))
    rows = db.query(f"{_TASK_SELECT} WHERE {where} ORDER BY t.priority, t.id LIMIT 200", tuple(params))
    deps = _deps(db, [int(r["id"]) for r in rows])
    return [_task_out(r, deps.get(int(r["id"]))) for r in rows]


def _ttl(payload: dict[str, Any], settings: Settings) -> int:
    return v.integer(
        payload.get("ttl_seconds"),
        "ttl_seconds",
        default=settings.task_lease_default,
        lo=10,
        hi=settings.task_lease_max,
    )


def claim_task(
    db: Database,
    p: Principal,
    task_id: int,
    notifier: Notifier | None = None,
    payload: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Atomic: exactly one concurrent claimer wins; the others get 409 with the winner.

    With ``session_id`` the claim is bound to that session and returns a fresh ``lease_epoch``
    (fencing token). Without it (legacy AgentLink contract) the lease is bound to the agent only;
    it still expires, so a vanished agent can never hold a task forever.
    """
    payload = v.obj(payload or {})
    settings = settings or Settings()
    p.require_writer()
    row = _task_row(db, task_id)
    ch = channel_for(db, p, int(row["channel_id"]), write=True)
    ttl = _ttl(payload, settings)
    now = now_ms()
    with db.transaction() as tx:
        sid = None
        if payload.get("session_id"):
            from .sessions import own_session

            s = own_session(tx, p, payload["session_id"])
            if int(s["project_id"]) != int(row["project_id"] or 0):
                raise Forbidden("the session belongs to another project")
            sid = str(s["id"])
        cur = _task_row(tx, task_id)
        # Idempotent re-claim by the current holder (same session, or same agent for legacy claims).
        same_holder = cur["assignee_id"] == p.agent_id and (sid is None or cur["assignee_session"] == sid)
        if cur["status"] in ACTIVE and same_holder and cur["lease_expires_at"] and int(cur["lease_expires_at"]) > now:
            return get_task(tx, p, task_id)
        unmet = tx.one(
            "SELECT COUNT(*) AS n FROM task_deps d JOIN tasks t ON t.id = d.depends_on "
            "WHERE d.task_id = ? AND t.status <> 'done'",
            (task_id,),
        )
        n = tx.execute(
            "UPDATE tasks SET status = 'claimed', assignee_id = ?, assignee_session = ?, lease_epoch = lease_epoch + 1, "
            "lease_expires_at = ?, last_progress_at = ?, block_reason = NULL, orphaned_at = NULL, "
            "version = version + 1, updated_at = ? "
            "WHERE id = ? AND status IN ('open', 'orphaned') AND (assignee_id IS NULL OR assignee_id = ? OR status = 'orphaned') "
            "AND NOT EXISTS (SELECT 1 FROM task_deps d JOIN tasks t2 ON t2.id = d.depends_on "
            "WHERE d.task_id = tasks.id AND t2.status <> 'done')",
            (p.agent_id, sid, now + ttl * 1000, now, now, task_id, p.agent_id),
        )
        if not n:
            current = _shape(tx, _task_row(tx, task_id))
            if unmet and int(unmet["n"]) and current["status"] in ("open", "orphaned"):
                raise Conflict(
                    f"task #{task_id} has unfinished dependencies", reason="dependencies_unmet",
                    depends_on=current["depends_on"],
                )  # fmt: skip
            raise Conflict(
                f"task #{task_id} is {current['status']}", assignee=current["assignee"], status=current["status"],
                reason="already_claimed",
            )  # fmt: skip
        new = _task_row(tx, task_id)
        recovered = cur["status"] == "orphaned"
        data = {"epoch": new["lease_epoch"], "ttl_seconds": ttl, "recovered": recovered, "title": new["title"]}
        emit(
            tx, "task.claimed", project_id=new["project_id"], channel_id=int(ch["id"]), actor_id=p.agent_id,
            session_id=sid, task_id=task_id, data=data,
        )  # fmt: skip
        sys_message(
            tx,
            p,
            ch,
            f"@{p.name} claimed task #{task_id}: {new['title']}"
            + (" (recovered after orphaning)" if recovered else ""),
            {"task_id": task_id, "event": "claimed", "epoch": new["lease_epoch"], "session_id": sid},
        )
        out = get_task(tx, p, task_id)
    if notifier:
        notifier.bump()
    return out


def _check_holder(tx: Executor, p: Principal, row: Row, payload: dict[str, Any], now: int) -> None:
    """Fencing check for a holder write. Raises 409 stale_lease without touching state."""
    epoch = payload.get("lease_epoch")
    sid = payload.get("session_id")
    if row["assignee_session"] is not None and epoch is None:
        raise Conflict(
            f"task #{row['id']} is leased to a session: lease_epoch and session_id are required",
            reason="lease_required",
        )
    stale = (
        row["assignee_id"] != p.agent_id
        or (epoch is not None and v.integer(epoch, "lease_epoch") != int(row["lease_epoch"]))
        or (row["assignee_session"] is not None and sid != row["assignee_session"])
        or (row["lease_expires_at"] is not None and int(row["lease_expires_at"]) <= now)
    )
    if stale:
        raise Conflict(
            f"stale lease for task #{row['id']}: current epoch is {row['lease_epoch']}",
            reason="stale_lease",
            current_epoch=row["lease_epoch"],
            your_epoch=epoch,
        )


def _record_stale(db: Database, p: Principal, row: Row, payload: dict[str, Any]) -> None:
    """Rejected stale writes are made observable (separate tx: the rejected operation changed nothing)."""
    with db.transaction() as tx:
        emit(
            tx, "task.stale_write", project_id=row["project_id"], channel_id=int(row["channel_id"]), actor_id=p.agent_id,
            session_id=payload.get("session_id") if isinstance(payload.get("session_id"), str) else None,
            task_id=int(row["id"]), data={"your_epoch": payload.get("lease_epoch"), "current_epoch": row["lease_epoch"]},
        )  # fmt: skip


def _clear_lease_sql() -> str:
    return "assignee_session = NULL, lease_expires_at = NULL"


def update_task(
    db: Database,
    p: Principal,
    task_id: int,
    payload: dict[str, Any],
    notifier: Notifier | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    payload = v.obj(payload)
    row = _task_row(db, task_id)
    ch = channel_for(db, p, int(row["channel_id"]), write=True)
    try:
        with db.transaction() as tx:
            out = _update_task_tx(tx, p, ch, task_id, payload)
    except Conflict as exc:
        if exc.extra.get("reason") == "stale_lease":
            _record_stale(db, p, row, payload)
        raise
    if notifier:
        notifier.bump()
    return out


def _update_task_tx(tx: Executor, p: Principal, ch: Row, task_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    row = _task_row(tx, task_id)
    now = now_ms()
    is_holder = row["assignee_id"] == p.agent_id and row["status"] in ACTIVE
    is_creator = row["created_by"] == p.agent_id
    if not (p.is_admin or is_holder or is_creator):
        if row["lease_epoch"] and payload.get("lease_epoch") is not None:
            raise Conflict(
                f"stale lease for task #{task_id}: you no longer hold it", reason="stale_lease",
                current_epoch=row["lease_epoch"], your_epoch=payload.get("lease_epoch"),
            )  # fmt: skip
        raise Forbidden("only the assignee, the creator or an admin can update this task")
    if payload.get("if_version") is not None and v.integer(payload["if_version"], "if_version", lo=1) != row["version"]:
        raise Conflict("task changed since you read it", current=_shape(tx, row))
    holder_write = payload.get("lease_epoch") is not None or (is_holder and not p.is_admin and not is_creator)
    if holder_write or (is_holder and payload.get("status") in HOLDER_ONLY and not p.is_admin):
        _check_holder(tx, p, row, payload, now)
    sets: list[str] = ["version = version + 1", "updated_at = ?"]
    params: list[Any] = [now]
    changes: dict[str, Any] = {}
    new_status = None
    if "status" in payload:
        new_status = v.choice(payload["status"], "status", v.TASK_STATUSES)
        if new_status != row["status"]:
            if new_status not in TRANSITIONS.get(str(row["status"]), ()):
                raise Conflict(f"invalid transition {row['status']} -> {new_status}", reason="invalid_transition")
            leased_by_other = row["status"] in ACTIVE and row["assignee_id"] not in (None, p.agent_id)
            if new_status in HOLDER_ONLY and leased_by_other and not p.is_admin:
                raise Forbidden(f"only the lease holder can move task #{task_id} to {new_status}")
            changes["status"] = new_status
    if "assignee" in payload:
        if payload["assignee"] in (None, ""):
            sets.append("assignee_id = NULL")
            sets.append(_clear_lease_sql())
            changes["assignee"] = None
            changes.setdefault("status", "open")
        else:
            if not (p.is_admin or is_creator):
                raise Forbidden("only the creator or an admin can reassign a task")
            a = agent_by_name(tx, v.name(payload["assignee"], "assignee"))
            sets.append("assignee_id = ?")
            params.append(a["id"])
            sets.append(_clear_lease_sql())
            sets.append("lease_epoch = lease_epoch + 1")  # old holder's epoch is now stale
            changes["assignee"] = a["name"]
    for f, limit in (("title", 200), ("description", v.MAX_BODY)):
        if f in payload:
            sets.append(f"{f} = ?")
            params.append(v.text(payload[f], f, limit, required=(f == "title")))
            changes[f] = "updated"
    if "priority" in payload:
        sets.append("priority = ?")
        params.append(v.integer(payload["priority"], "priority", lo=1, hi=3))
        changes["priority"] = params[-1]
    deps = _dep_list(payload)
    if deps is not None:
        if not (p.is_admin or is_creator):
            raise Forbidden("only the creator or an admin can change dependencies")
        _validate_deps(tx, task_id, int(row["project_id"] or 0), deps)
        tx.execute("DELETE FROM task_deps WHERE task_id = ?", (task_id,))
        for d in deps:
            tx.execute("INSERT INTO task_deps(task_id, depends_on) VALUES (?, ?)", (task_id, d))
        changes["depends_on"] = deps
    if "status" in changes:
        sets.append("status = ?")
        params.append(changes["status"])
        if changes["status"] in TERMINAL or changes["status"] == "open":
            sets.append(_clear_lease_sql())
            if changes["status"] == "open" and "assignee" not in changes:
                sets.append("assignee_id = NULL")
        if changes["status"] == "blocked":
            sets.append("block_reason = ?")
            params.append(v.text(payload.get("note"), "note", 2000) or "blocked by holder")
        elif row["block_reason"]:
            sets.append("block_reason = NULL")
    if payload.get("lease_epoch") is not None and row["assignee_session"]:
        sets.append("last_progress_at = ?")  # a valid holder write is evidence of progress
        params.append(now)
    if not changes:
        return get_task(tx, p, task_id)
    tx.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", (*params, task_id))
    if changes.get("status") in (*TERMINAL, "open"):
        _drop_task_leases(tx, task_id, row["project_id"], "task " + str(changes["status"]))
    note = v.text(payload.get("note"), "note", 2000)
    summary = ", ".join(f"{k}={val}" for k, val in changes.items())
    body = f"@{p.name} updated task #{task_id} ({summary})" + (f": {note}" if note else "")
    mentions = [x for x in (changes.get("assignee"), row["creator"]) if x and x != p.name]
    etype = "task.blocked" if changes.get("status") == "blocked" else "task.updated"
    emit(
        tx, etype, project_id=row["project_id"], channel_id=int(ch["id"]), actor_id=p.agent_id,
        session_id=payload.get("session_id") if isinstance(payload.get("session_id"), str) else None,
        task_id=task_id, data={**changes, "from": row["status"], "note": note[:300] if note else None},
    )  # fmt: skip
    sys_message(tx, p, ch, body, {"task_id": task_id, "event": "updated", **changes}, mentions)
    return get_task(tx, p, task_id)


def renew_task(db: Database, p: Principal, task_id: int, payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Extend the lease of a task the caller holds. Requires recent *progress*, not mere liveness (I10).

    ``progress: true`` / ``progress_note`` in the request counts as fresh progress evidence. Without it, the
    renewal is only granted if progress was recorded within ``progress_window``.
    """
    payload = v.obj(payload)
    ttl = _ttl(payload, settings)
    note = v.text(payload.get("progress_note"), "progress_note", 300)
    progress = bool(payload.get("progress")) or bool(note)
    now = now_ms()
    row = _task_row(db, task_id)
    channel_for(db, p, int(row["channel_id"]), write=True)
    try:
        with db.transaction() as tx:
            row = _task_row(tx, task_id)
            if row["status"] not in ACTIVE:
                raise Conflict(
                    f"task #{task_id} is {row['status']}", reason="stale_lease", current_epoch=row["lease_epoch"]
                )
            _check_holder(
                tx,
                p,
                row,
                {
                    **payload,
                    "lease_epoch": payload.get(
                        "lease_epoch", row["lease_epoch"] if row["assignee_session"] is None else None
                    ),
                },
                now,
            )
            last = now if progress else int(row["last_progress_at"] or 0)
            if now - last > settings.progress_window * 1000:
                raise Conflict(
                    f"no progress reported for task #{task_id} in {settings.progress_window}s: lease not renewed",
                    reason="no_progress",
                    lease_expires=iso(int(row["lease_expires_at"])) if row["lease_expires_at"] else None,
                )
            tx.execute(
                "UPDATE tasks SET lease_expires_at = ?, last_progress_at = ? WHERE id = ? AND lease_epoch = ?",
                (now + ttl * 1000, last, task_id, row["lease_epoch"]),
            )
            if progress and row["assignee_session"]:
                tx.execute(
                    "UPDATE sessions SET last_progress_at = ?, last_heartbeat_at = ?, stalled_since = NULL"
                    + (", progress_note = ?" if note else "")
                    + " WHERE id = ?",
                    (now, now, *([note] if note else []), row["assignee_session"]),
                )
            out = get_task(tx, p, task_id)
    except Conflict as exc:
        if exc.extra.get("reason") == "stale_lease":
            _record_stale(db, p, row, payload)
        raise
    return out


def release_task(
    db: Database, p: Principal, task_id: int, payload: dict[str, Any], notifier: Notifier | None = None
) -> dict[str, Any]:
    """Holder gives the task back (status -> open). Same fencing as any holder write."""
    payload = v.obj(payload)
    return update_task(db, p, task_id, {**payload, "status": "open"}, notifier)


def add_checkpoint(
    db: Database, p: Principal, task_id: int, payload: dict[str, Any], notifier: Notifier | None = None
) -> dict[str, Any]:
    """Record what the holder knows (branch, commit, done/pending, tests, risks). Counts as progress."""
    payload = v.obj(payload)
    data = v.obj(
        payload.get("checkpoint")
        if "checkpoint" in payload
        else {k: payload[k] for k in payload if k in CHECKPOINT_KEYS}
    )
    unknown = set(data) - CHECKPOINT_KEYS
    if unknown:
        raise BadRequest(f"unknown checkpoint keys: {sorted(unknown)}; allowed: {sorted(CHECKPOINT_KEYS)}")
    blob = v.json_blob(data, "checkpoint", 16_000)
    now = now_ms()
    row = _task_row(db, task_id)
    ch = channel_for(db, p, int(row["channel_id"]), write=True)
    try:
        with db.transaction() as tx:
            row = _task_row(tx, task_id)
            if row["status"] not in ACTIVE:
                raise Conflict(f"task #{task_id} is {row['status']}: no active lease", reason="stale_lease",
                               current_epoch=row["lease_epoch"])  # fmt: skip
            _check_holder(tx, p, row, payload, now)
            tx.execute(
                "INSERT INTO checkpoints(task_id, session_id, lease_epoch, author_id, data, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, row["assignee_session"], row["lease_epoch"], p.agent_id, blob, now),
            )
            tx.execute("UPDATE tasks SET last_progress_at = ? WHERE id = ?", (now, task_id))
            if row["assignee_session"]:
                tx.execute(
                    "UPDATE sessions SET last_progress_at = ?, stalled_since = NULL WHERE id = ?",
                    (now, row["assignee_session"]),
                )
            summary = {k: data[k] for k in ("summary", "branch", "commit") if k in data}
            emit(
                tx, "task.checkpoint", project_id=row["project_id"], channel_id=int(ch["id"]), actor_id=p.agent_id,
                session_id=row["assignee_session"], task_id=task_id, data={"epoch": row["lease_epoch"], **summary},
            )  # fmt: skip
            out = get_task(tx, p, task_id)
    except Conflict as exc:
        if exc.extra.get("reason") == "stale_lease":
            _record_stale(db, p, row, payload)
        raise
    if notifier:
        notifier.bump()
    return out


def list_checkpoints(db: Database, p: Principal, task_id: int) -> list[dict[str, Any]]:
    get_task(db, p, task_id)  # access check
    rows = db.query(
        "SELECT c.id, c.session_id, c.lease_epoch, c.data, c.created_at, a.name AS author FROM checkpoints c "
        "JOIN agents a ON a.id = c.author_id WHERE c.task_id = ? ORDER BY c.id DESC LIMIT 50",
        (task_id,),
    )
    return [
        {"id": r["id"], "session_id": r["session_id"], "epoch": r["lease_epoch"], "author": r["author"],
         "at": iso(int(r["created_at"])), **json.loads(r["data"])}
        for r in rows
    ]  # fmt: skip


def lease_status(db: Database, p: Principal, task_id: int, q: dict[str, Any]) -> dict[str, Any]:
    """Pre-publish check for local integrations (e.g. before `git push`): do I still hold this task?"""
    t = get_task(db, p, task_id)
    epoch = q.get("lease_epoch")
    sid = q.get("session_id")
    holds = (
        t["assignee"] == p.name
        and t["lease_valid"]
        and (epoch is None or v.integer(epoch, "lease_epoch") == t["lease_epoch"])
        and (t["assignee_session"] is None or sid == t["assignee_session"])
    )
    return {"task_id": task_id, "holds_lease": bool(holds), "current_epoch": t["lease_epoch"], "status": t["status"],
            "assignee": t["assignee"], "lease_expires": t["lease_expires"]}  # fmt: skip


def _drop_task_leases(tx: Executor, task_id: int, project_id: int | None, why: str) -> None:
    for lease in tx.query("SELECT id, channel_id, resource FROM leases WHERE task_id = ?", (task_id,)):
        tx.execute("DELETE FROM leases WHERE id = ?", (lease["id"],))
        emit(
            tx, "lease.released", project_id=project_id, channel_id=int(lease["channel_id"]), task_id=task_id,
            data={"resource": lease["resource"], "reason": why},
        )  # fmt: skip


# -- leases ----------------------------------------------------------------------------


def _norm(res: str) -> str:
    for suffix in ("/**", "/*"):
        if res.endswith(suffix):
            res = res[: -len(suffix)]
    return res.rstrip("/")


def overlaps(a: str, b: str) -> bool:
    """Path-aware overlap: 'src/auth' conflicts with 'src/auth/x.py' but not 'src/authz'."""
    a, b = _norm(a), _norm(b)
    return a == b or b.startswith(a + "/") or a.startswith(b + "/") or a == "" or b == ""


def _lease_out(r: Row) -> dict[str, Any]:
    return {
        "resource": r["resource"],
        "holder": r["holder"],
        "note": r["note"],
        "expires": iso(int(r["expires_at"])),
        "channel": r["channel"],
        "session_id": r.get("session_id"),
        "task_id": r.get("task_id"),
        "lease_epoch": r.get("lease_epoch"),
    }


_LEASE_SELECT = (
    "SELECT l.*, a.name AS holder, c.name AS channel FROM leases l "
    "JOIN agents a ON a.id = l.holder_id JOIN channels c ON c.id = l.channel_id "
)


def acquire_lease(
    db: Database, p: Principal, channel: str | int, payload: dict[str, Any], notifier: Notifier | None = None
) -> dict[str, Any]:
    payload = v.obj(payload)
    ch = channel_for(db, p, channel, write=True)
    res = v.resource(payload.get("resource"))
    ttl = v.integer(payload.get("ttl_seconds"), "ttl_seconds", default=900, lo=10, hi=86_400)
    note = v.text(payload.get("note"), "note", 300)
    task_id = v.opt_int(payload.get("task_id"), "task_id")
    now = now_ms()
    renewed = False
    with db.transaction() as tx:
        sid = None
        if payload.get("session_id"):
            from .sessions import own_session

            sid = str(own_session(tx, p, payload["session_id"])["id"])
        epoch = None
        if task_id is not None:
            trow = _task_row(tx, task_id)
            if int(trow["channel_id"]) != int(ch["id"]):
                raise BadRequest("task_id must belong to the same channel")
            _check_holder(tx, p, trow, payload, now)
            epoch = int(trow["lease_epoch"])
        for gone in tx.query(f"{_LEASE_SELECT} WHERE l.channel_id = ? AND l.expires_at <= ?", (ch["id"], now)):
            tx.execute("DELETE FROM leases WHERE id = ?", (gone["id"],))
            emit(tx, "lease.expired", project_id=ch["project_id"], channel_id=int(ch["id"]), task_id=gone["task_id"],
                 data={"resource": gone["resource"], "holder": gone["holder"]})  # fmt: skip
        for other in tx.query(f"{_LEASE_SELECT} WHERE l.channel_id = ?", (ch["id"],)):
            if not overlaps(other["resource"], res):
                continue
            mine = int(other["holder_id"]) == p.agent_id and (other["session_id"] in (None, sid) or sid is None)
            if mine and other["resource"] == res:
                tx.execute(
                    "UPDATE leases SET expires_at = ?, note = ?, session_id = COALESCE(?, session_id), "
                    "task_id = COALESCE(?, task_id), lease_epoch = COALESCE(?, lease_epoch) WHERE id = ?",
                    (now + ttl * 1000, note or other["note"], sid, task_id, epoch, other["id"]),
                )
                renewed = True
                break
            if not mine:
                raise Conflict(
                    f"'{res}' overlaps lease '{other['resource']}' held by @{other['holder']}", lease=_lease_out(other),
                    reason="lease_conflict",
                )  # fmt: skip
        if not renewed:
            tx.execute(
                "INSERT INTO leases(channel_id, resource, holder_id, note, expires_at, created_at, session_id, task_id, "
                "lease_epoch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ch["id"], res, p.agent_id, note, now + ttl * 1000, now, sid, task_id, epoch),
            )
            emit(tx, "lease.acquired", project_id=ch["project_id"], channel_id=int(ch["id"]), actor_id=p.agent_id,
                 session_id=sid, task_id=task_id, data={"resource": res, "ttl_seconds": ttl})  # fmt: skip
            sys_message(
                tx, p, ch, f"@{p.name} leased {res} for {ttl}s" + (f": {note}" if note else ""),
                {"event": "lease.acquired", "resource": res, "ttl_seconds": ttl, "task_id": task_id},
            )  # fmt: skip
        row = tx.one(f"{_LEASE_SELECT} WHERE l.channel_id = ? AND l.resource = ?", (ch["id"], res))
    assert row is not None
    if notifier and not renewed:
        notifier.bump()
    return {**_lease_out(row), "renewed": renewed}


def release_lease(
    db: Database, p: Principal, channel: str | int, resource: Any, notifier: Notifier | None = None
) -> dict[str, Any]:
    ch = channel_for(db, p, channel, write=True)
    res = v.resource(resource)
    with db.transaction() as tx:
        row = tx.one("SELECT id, holder_id, task_id FROM leases WHERE channel_id = ? AND resource = ?", (ch["id"], res))
        if row is None:
            raise NotFound(f"no lease on '{res}'")
        if int(row["holder_id"]) != p.agent_id and not p.is_admin:
            raise Forbidden("only the holder or an admin can release this lease")
        tx.execute("DELETE FROM leases WHERE id = ?", (row["id"],))
        emit(tx, "lease.released", project_id=ch["project_id"], channel_id=int(ch["id"]), actor_id=p.agent_id,
             task_id=row["task_id"], data={"resource": res})  # fmt: skip
        sys_message(tx, p, ch, f"@{p.name} released {res}", {"event": "lease.released", "resource": res})
    if notifier:
        notifier.bump()
    return {"released": res}


def list_leases(db: Database, p: Principal, channel: str | int) -> list[dict[str, Any]]:
    ch = channel_for(db, p, channel)
    rows = db.query(
        f"{_LEASE_SELECT} WHERE l.channel_id = ? AND l.expires_at > ? ORDER BY l.resource", (ch["id"], now_ms())
    )
    return [_lease_out(r) for r in rows]


# -- shared key-value context ---------------------------------------------------------------


def kv_get(db: Database, p: Principal, channel: str | int, key: Any) -> dict[str, Any]:
    ch = channel_for(db, p, channel)
    k = v.key(key)
    row = db.one(
        "SELECT kv.*, a.name AS updated_by_name FROM kv JOIN agents a ON a.id = kv.updated_by "
        "WHERE channel_id = ? AND key = ?",
        (ch["id"], k),
    )
    if row is None:
        raise NotFound(f"key '{k}' not set")
    return {
        "key": k,
        "value": json.loads(row["value"]),
        "version": row["version"],
        "updated_by": row["updated_by_name"],
        "updated": iso(int(row["updated_at"])),
    }


def kv_list(db: Database, p: Principal, channel: str | int, prefix: str = "") -> list[dict[str, Any]]:
    ch = channel_for(db, p, channel)
    rows = db.query(
        "SELECT kv.key, kv.value, kv.version, kv.updated_at, a.name AS updated_by FROM kv "
        "JOIN agents a ON a.id = kv.updated_by WHERE channel_id = ? ORDER BY key",
        (ch["id"],),
    )
    return [
        {
            "key": r["key"],
            "value": json.loads(r["value"]),
            "version": r["version"],
            "updated_by": r["updated_by"],
            "updated": iso(int(r["updated_at"])),
        }
        for r in rows
        if str(r["key"]).startswith(prefix)
    ]


def kv_set(
    db: Database, p: Principal, channel: str | int, key: Any, payload: dict[str, Any], notifier: Notifier | None = None
) -> dict[str, Any]:
    payload = v.obj(payload)
    ch = channel_for(db, p, channel, write=True)
    k = v.key(key)
    if "value" not in payload:
        raise BadRequest("value is required")
    value = v.json_blob(payload["value"], "value")
    expected = payload.get("if_version")
    now = now_ms()
    with db.transaction() as tx:
        row = tx.one("SELECT version FROM kv WHERE channel_id = ? AND key = ?", (ch["id"], k))
        current = int(row["version"]) if row else 0
        if expected is not None and v.integer(expected, "if_version") != current:
            raise Conflict(f"key '{k}' is at version {current}", version=current)
        if row:
            tx.execute(
                "UPDATE kv SET value = ?, version = ?, updated_by = ?, updated_at = ? WHERE channel_id = ? AND key = ?",
                (value, current + 1, p.agent_id, now, ch["id"], k),
            )
        else:
            tx.execute(
                "INSERT INTO kv(channel_id, key, value, version, updated_by, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
                (ch["id"], k, value, p.agent_id, now),
            )
        emit(tx, "kv.set", project_id=ch["project_id"], channel_id=int(ch["id"]), actor_id=p.agent_id,
             data={"key": k, "version": current + 1})  # fmt: skip
        sys_message(
            tx,
            p,
            ch,
            f"@{p.name} set context '{k}' (v{current + 1})",
            {"event": "kv.set", "key": k, "version": current + 1},
        )
    if notifier:
        notifier.bump()
    return kv_get(db, p, ch["name"], k)


# -- presence ----------------------------------------------------------------------------

PRESENCE_STATUSES = ("online", "busy", "idle", "offline")


def heartbeat(db: Database, p: Principal, payload: dict[str, Any]) -> dict[str, Any]:
    p.require_writer()
    payload = v.obj(payload)
    status = v.choice(payload.get("status"), "status", PRESENCE_STATUSES, default="online")
    activity = v.text(payload.get("activity"), "activity", 300)
    channel_id = None
    if payload.get("channel"):
        channel_id = int(channel_for(db, p, payload["channel"])["id"])
    db.execute(
        "INSERT INTO presence(agent_id, status, activity, channel_id, last_seen) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (agent_id) DO UPDATE SET status = excluded.status, activity = excluded.activity, "
        "channel_id = excluded.channel_id, last_seen = excluded.last_seen",
        (p.agent_id, status, activity, channel_id, now_ms()),
    )
    return {"agent": p.name, "status": status, "activity": activity}


def presence(db: Database, p: Principal) -> list[dict[str, Any]]:
    now = now_ms()
    rows = db.query(
        "SELECT a.name, a.role, pr.status, pr.activity, pr.last_seen, c.name AS channel FROM presence pr "
        "JOIN agents a ON a.id = pr.agent_id LEFT JOIN channels c ON c.id = pr.channel_id "
        "ORDER BY pr.last_seen DESC"
    )
    return [
        {
            "agent": r["name"],
            "role": r["role"],
            "channel": r["channel"],
            "activity": r["activity"],
            "status": r["status"] if now - int(r["last_seen"]) < ONLINE_WINDOW_MS else "offline",
            "last_seen": iso(int(r["last_seen"])),
        }
        for r in rows
    ]
