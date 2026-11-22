"""Per-project coordinator with leases, a monotonic epoch and server-side election.

State machine (``coordinators.status``)::

    none ──designate / election──▶ offered ──accept(epoch)──▶ active
      ▲                              │  │                        │
      │        decline / timeout ────┘  └─▶ next candidate        │ lease expired / session gone / resign
      └──────────────────────────────────────────────────────────┘  (coordinator.lost; epoch is never reused)

* Every offer gets a new epoch (``epoch + 1``); an acceptance must quote it, so two candidates can
  never both accept (single-row conditional UPDATE inside one transaction).
* Exclusive coordinator operations must quote the active epoch; an old coordinator that reappears
  fails that check (``stale_epoch``) and cannot regain authority on its own.
* The election is deterministic code: ranking = configured priority, coordination specialization,
  lowest active-task load, oldest session; ties broken by session id. No LLM is involved and the
  server never invents an acceptance: an unanswered offer simply expires.
"""

from __future__ import annotations

import json
from typing import Any

from . import validation as v
from .config import Settings
from .db import Database, Executor, Row
from .errors import Conflict, Forbidden
from .events import emit
from .projects import project_for
from .security import Principal, now_ms
from .sessions import SESSION_SELECT, derived_status, own_session

COORD_SPECS = ("coordination", "coordinator", "lead", "pm")

SUCCESSION_PROMPT = """[AGENT OFFICE — COORDINATOR SUCCESSION]

{lost_line}
You have been selected as provisional coordinator of project {project}.
Coordination epoch: {epoch}. Offer expires: {expires}.

Before accepting:
1. Check the current project state (GET /api/office/snapshot?project={project}).
2. Review active tasks and their holders.
3. Check blocked tasks and dependencies.
4. Decide whether you have the capacity to coordinate.
5. Check whether a better-prepared candidate exists.

If you are suitable: accept (POST /api/projects/{project}/coordinator/accept with session_id and epoch {epoch}),
publish your coordination plan and keep supervising.
If a better candidate exists: decline with a reason and propose them (field "propose").
If you lack context or capacity: decline.
Do not act as coordinator until the server confirms your acceptance."""


def _row(tx: Executor, project_id: int) -> Row:
    row = tx.one("SELECT * FROM coordinators WHERE project_id = ?", (project_id,))
    if row is None:
        tx.execute(
            "INSERT INTO coordinators(project_id, status, epoch, updated_at) VALUES (?, 'none', 0, ?) ON CONFLICT DO NOTHING",
            (project_id, now_ms()),
        )
        row = tx.one("SELECT * FROM coordinators WHERE project_id = ?", (project_id,))
    assert row is not None
    return row


def state_out(r: Row, now: int) -> dict[str, Any]:
    return {
        "status": r["status"],
        "epoch": r["epoch"],
        "session_id": r["session_id"] if r["status"] == "active" else None,
        "lease_expires_at": r["lease_expires_at"] if r["status"] == "active" else None,
        "offered_session": r["offered_session"] if r["status"] == "offered" else None,
        "offer_expires_at": r["offer_expires_at"] if r["status"] == "offered" else None,
        "valid": r["status"] == "active" and bool(r["lease_expires_at"]) and int(r["lease_expires_at"]) > now,
    }


def get_state(db: Database, p: Principal, project: str) -> dict[str, Any]:
    pr = project_for(db, p, project)
    with db.transaction() as tx:
        r = _row(tx, int(pr["id"]))
        out = state_out(r, now_ms())
        if r["session_id"] and r["status"] == "active":
            who = tx.one(
                "SELECT a.name FROM sessions s JOIN agents a ON a.id = s.agent_id WHERE s.id = ?", (r["session_id"],)
            )
            out["agent"] = who["name"] if who else None
    return {"project": pr["name"], **out}


def _candidates(tx: Executor, project_id: int, exclude: set[str], now: int, settings: Settings) -> list[Row]:
    rows = tx.query(
        f"{SESSION_SELECT} WHERE s.project_id = ? AND s.ended_at IS NULL AND a.disabled = 0 AND s.parent_id IS NULL",
        (project_id,),
    )
    live = [
        r
        for r in rows
        if r["id"] not in exclude and derived_status(r, now, settings) in ("registered", "executing", "idle", "waiting")
    ]
    loads = {
        r["assignee_session"]: int(r["n"])
        for r in tx.query(
            "SELECT assignee_session, COUNT(*) AS n FROM tasks WHERE project_id = ? AND assignee_session IS NOT NULL "
            "GROUP BY assignee_session",
            (project_id,),
        )
    }
    prio = {
        int(r["id"]): int(r["coord_priority"] or 0)
        for r in tx.query(
            "SELECT id, coord_priority FROM agents WHERE id IN (SELECT agent_id FROM sessions WHERE project_id = ?)",
            (project_id,),
        )
    }

    def rank(r: Row) -> tuple[Any, ...]:
        spec = (r["specialization"] or r["agent_specialization"] or "").lower()
        caps = json.loads(r["capabilities"]) if r["capabilities"] else []
        can = spec in COORD_SPECS or "coordination" in caps
        return (-prio.get(int(r["agent_id"]), 0), not can, loads.get(r["id"], 0), int(r["created_at"]), r["id"])

    return sorted(live, key=rank)


def _offer(tx: Executor, project: Row, c: Row, target: Row, now: int, settings: Settings, lost: str | None) -> None:
    epoch = int(c["epoch"]) + 1
    expires = now + settings.offer_timeout * 1000
    tx.execute(
        "UPDATE coordinators SET status = 'offered', epoch = ?, offered_session = ?, offer_expires_at = ?, "
        "session_id = NULL, lease_expires_at = NULL, updated_at = ? WHERE project_id = ?",
        (epoch, target["id"], expires, now, project["id"]),
    )
    prompt = SUCCESSION_PROMPT.format(
        lost_line=lost or "The project has no active coordinator.",
        project=project["name"],
        epoch=epoch,
        expires=expires,
    )
    emit(
        tx,
        "coordinator.offered",
        project_id=int(project["id"]),
        session_id=str(target["id"]),
        data={"epoch": epoch, "agent": target["agent"], "offer_expires_at": expires, "prompt": prompt},
    )
    _notify(tx, project, target, prompt, epoch)


def _notify(tx: Executor, project: Row, target: Row, prompt: str, epoch: int) -> None:
    """Deliver the succession prompt as a high-priority mention in the project's first channel (inbox path)."""
    ch = tx.one("SELECT * FROM channels WHERE project_id = ? ORDER BY id LIMIT 1", (project["id"],))
    if ch is None:
        return
    from .coordination import sys_message

    sys_message(
        tx, None, ch, f"@{target['agent']} {prompt}",
        {"event": "coordinator.offered", "epoch": epoch, "session_id": target["id"], "priority": 1},
        [str(target["agent"])],
    )  # fmt: skip


def elect(tx: Executor, project: Row, now: int, settings: Settings, lost: str | None = None) -> dict[str, Any]:
    """Pick the next candidate and make an offer, or record that there is none. Idempotent per state."""
    c = _row(tx, int(project["id"]))
    declined = set(json.loads(c["declined"])) if c["declined"] else set()
    cands = _candidates(tx, int(project["id"]), declined, now, settings)
    proposed = c["proposed_session"]
    if proposed:
        for r in cands:
            if r["id"] == proposed:
                cands.remove(r)
                cands.insert(0, r)
                break
    if not cands:
        if not c["vacancy_notified"]:
            tx.execute("UPDATE coordinators SET vacancy_notified = 1, status = 'none', updated_at = ? WHERE project_id = ?",
                       (now, project["id"]))  # fmt: skip
            emit(tx, "coordinator.unavailable", project_id=int(project["id"]),
                 data={"reason": "no eligible live session", "excluded": sorted(declined)})  # fmt: skip
            _alert_humans(tx, project)
        return {"offered": None}
    _offer(tx, project, c, cands[0], now, settings, lost)
    return {"offered": cands[0]["id"]}


def _alert_humans(tx: Executor, project: Row) -> None:
    ch = tx.one("SELECT * FROM channels WHERE project_id = ? ORDER BY id LIMIT 1", (project["id"],))
    if ch is None:
        return
    admins = [
        str(r["name"])
        for r in tx.query(
            "SELECT a.name FROM project_members pm JOIN agents a ON a.id = pm.agent_id WHERE pm.project_id = ? AND pm.role = 'admin'",
            (project["id"],),
        )
    ]
    from .coordination import sys_message

    sys_message(
        tx, None, ch,
        f"[Agent Office] Project {project['name']} has no coordinator and no eligible candidate. "
        "Work that does not need a coordinator continues; tasks stay recoverable. Human attention needed.",
        {"event": "coordinator.unavailable", "priority": 1}, admins or None,
    )  # fmt: skip


def designate(db: Database, p: Principal, project: str, payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """A project admin (or global admin) designates the initial coordinator: an offer the session must accept."""
    payload = v.obj(payload)
    pr = project_for(db, p, project, write=True, admin=True)
    sid = v.text(payload.get("session_id"), "session_id", 64, required=True)
    now = now_ms()
    with db.transaction() as tx:
        target = tx.one(f"{SESSION_SELECT} WHERE s.id = ? AND s.project_id = ?", (sid, pr["id"]))
        if target is None or target["ended_at"] is not None:
            raise Conflict("session is not live in this project", reason="invalid_candidate")
        c = _row(tx, int(pr["id"]))
        if (
            c["status"] == "active"
            and c["lease_expires_at"]
            and int(c["lease_expires_at"]) > now
            and not payload.get("replace")
        ):
            raise Conflict("project already has an active coordinator (pass replace=true)", reason="coordinator_active")
        if c["status"] == "active":
            emit(tx, "coordinator.released", project_id=int(pr["id"]), session_id=c["session_id"],
                 data={"epoch": c["epoch"], "reason": "replaced by designation"})  # fmt: skip
        tx.execute("UPDATE coordinators SET declined = NULL, proposed_session = NULL, vacancy_notified = 0 WHERE project_id = ?",
                   (pr["id"],))  # fmt: skip
        _offer(tx, pr, _row(tx, int(pr["id"])), target, now, settings, "A participant designated you.")
        out = state_out(_row(tx, int(pr["id"])), now)
    return {"project": pr["name"], **out}


def accept(db: Database, p: Principal, project: str, payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    payload = v.obj(payload)
    pr = project_for(db, p, project, write=True)
    epoch = v.integer(payload.get("epoch"), "epoch", lo=1)
    now = now_ms()
    with db.transaction() as tx:
        s = own_session(tx, p, payload.get("session_id"))
        n = tx.execute(
            "UPDATE coordinators SET status = 'active', session_id = ?, lease_expires_at = ?, offered_session = NULL, "
            "offer_expires_at = NULL, declined = NULL, proposed_session = NULL, vacancy_notified = 0, updated_at = ? "
            "WHERE project_id = ? AND status = 'offered' AND offered_session = ? AND epoch = ? AND offer_expires_at > ?",
            (s["id"], now + settings.coordinator_lease * 1000, now, pr["id"], s["id"], epoch, now),
        )
        if not n:
            c = _row(tx, int(pr["id"]))
            raise Conflict("no valid pending offer for this session and epoch", reason="stale_epoch",
                           current=state_out(c, now))  # fmt: skip
        emit(tx, "coordinator.accepted", project_id=int(pr["id"]), actor_id=p.agent_id, session_id=str(s["id"]),
             data={"epoch": epoch, "agent": p.name})  # fmt: skip
        out = state_out(_row(tx, int(pr["id"])), now)
    return {"project": pr["name"], **out}


def decline(db: Database, p: Principal, project: str, payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    payload = v.obj(payload)
    pr = project_for(db, p, project, write=True)
    epoch = v.integer(payload.get("epoch"), "epoch", lo=1)
    reason = v.text(payload.get("reason"), "reason", 500)
    propose = payload.get("propose")
    now = now_ms()
    with db.transaction() as tx:
        s = own_session(tx, p, payload.get("session_id"))
        c = _row(tx, int(pr["id"]))
        if not (c["status"] == "offered" and c["offered_session"] == s["id"] and int(c["epoch"]) == epoch):
            raise Conflict(
                "no pending offer for this session and epoch", reason="stale_epoch", current=state_out(c, now)
            )
        declined = set(json.loads(c["declined"])) if c["declined"] else set()
        declined.add(str(s["id"]))
        prop = None
        if propose:
            cand = tx.one(
                "SELECT id FROM sessions WHERE id = ? AND project_id = ? AND ended_at IS NULL", (str(propose), pr["id"])
            )
            prop = cand["id"] if cand else None
        tx.execute(
            "UPDATE coordinators SET status = 'none', offered_session = NULL, offer_expires_at = NULL, declined = ?, "
            "proposed_session = ?, updated_at = ? WHERE project_id = ?",
            (json.dumps(sorted(declined)), prop, now, pr["id"]),
        )
        emit(tx, "coordinator.declined", project_id=int(pr["id"]), actor_id=p.agent_id, session_id=str(s["id"]),
             data={"epoch": epoch, "reason": reason, "proposed": prop})  # fmt: skip
        result = elect(tx, pr, now, settings)
        out = state_out(_row(tx, int(pr["id"])), now)
    return {"project": pr["name"], **out, **result}


def resign(db: Database, p: Principal, project: str, payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    payload = v.obj(payload)
    pr = project_for(db, p, project, write=True)
    now = now_ms()
    with db.transaction() as tx:
        s = own_session(tx, p, payload.get("session_id"))
        require_epoch(tx, int(pr["id"]), str(s["id"]), payload.get("epoch"), now)
        c = _row(tx, int(pr["id"]))
        tx.execute("UPDATE coordinators SET status = 'none', session_id = NULL, lease_expires_at = NULL, updated_at = ?, "
                   "declined = ? WHERE project_id = ?", (now, json.dumps([s["id"]]), pr["id"]))  # fmt: skip
        emit(tx, "coordinator.released", project_id=int(pr["id"]), session_id=str(s["id"]),
             data={"epoch": c["epoch"], "reason": "resigned"})  # fmt: skip
        elect(tx, pr, now, settings, "The previous coordinator resigned.")
        out = state_out(_row(tx, int(pr["id"])), now)
    return {"project": pr["name"], **out}


def require_epoch(tx: Executor, project_id: int, session_id: str, epoch: Any, now: int) -> Row:
    """Guard for exclusive coordinator operations: the caller must be the *current, valid* coordinator."""
    c = _row(tx, project_id)
    e = v.integer(epoch, "epoch", lo=1)
    valid = (
        c["status"] == "active"
        and c["session_id"] == session_id
        and int(c["epoch"]) == e
        and c["lease_expires_at"] is not None
        and int(c["lease_expires_at"]) > now
    )
    if not valid:
        raise Conflict(
            f"coordinator epoch {e} is not the current authority (current epoch {c['epoch']}, status {c['status']})",
            reason="stale_epoch",
            current=state_out(c, now),
        )
    return c


def assign(db: Database, p: Principal, project: str, payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Exclusive operation: the coordinator reserves an open task for an agent (epoch-guarded)."""
    payload = v.obj(payload)
    pr = project_for(db, p, project, write=True)
    task_id = v.integer(payload.get("task_id"), "task_id", lo=1)
    now = now_ms()
    from .coordination import get_task, sys_message
    from .directory import agent_by_name

    with db.transaction() as tx:
        s = own_session(tx, p, payload.get("session_id"))
        c = require_epoch(tx, int(pr["id"]), str(s["id"]), payload.get("epoch"), now)
        t = tx.one(
            "SELECT t.*, c.project_id AS cp FROM tasks t JOIN channels c ON c.id = t.channel_id WHERE t.id = ?",
            (task_id,),
        )
        if t is None or int(t["cp"] or 0) != int(pr["id"]):
            raise Forbidden("task is not in this project")
        if t["status"] not in ("open", "orphaned"):
            raise Conflict(f"task #{task_id} is {t['status']}", reason="not_open")
        a = agent_by_name(tx, v.name(payload.get("assignee"), "assignee"))
        tx.execute("UPDATE tasks SET assignee_id = ?, status = 'open', version = version + 1, updated_at = ? WHERE id = ?",
                   (a["id"], now, task_id))  # fmt: skip
        ch = tx.one("SELECT * FROM channels WHERE id = ?", (t["channel_id"],))
        assert ch is not None
        emit(tx, "task.updated", project_id=int(pr["id"]), channel_id=int(ch["id"]), actor_id=p.agent_id,
             session_id=str(s["id"]), task_id=task_id,
             data={"assignee": a["name"], "by_coordinator_epoch": c["epoch"], "from": t["status"]})  # fmt: skip
        sys_message(tx, p, ch, f"@{p.name} (coordinator, epoch {c['epoch']}) assigned task #{task_id} to @{a['name']}",
                    {"task_id": task_id, "event": "assigned", "assignee": a["name"]}, [str(a["name"])])  # fmt: skip
        out = get_task(tx, p, task_id)
    return out


def renew_on_heartbeat(tx: Executor, s: Row, now: int, settings: Settings) -> dict[str, Any] | None:
    """The active coordinator's lease is renewed by its own (non-stalled) session heartbeats."""
    c = tx.one("SELECT * FROM coordinators WHERE project_id = ?", (s["project_id"],))
    if c is None:
        return None
    if c["status"] == "active" and c["session_id"] == s["id"]:
        if derived_status(s, now, settings) == "stalled":
            return state_out(c, now)  # a stalled coordinator is not renewed; it will lose the lease
        if c["lease_expires_at"] and int(c["lease_expires_at"]) > now:
            tx.execute("UPDATE coordinators SET lease_expires_at = ? WHERE project_id = ? AND epoch = ?",
                       (now + settings.coordinator_lease * 1000, s["project_id"], c["epoch"]))  # fmt: skip
        c = tx.one("SELECT * FROM coordinators WHERE project_id = ?", (s["project_id"],))
        assert c is not None
        return {**state_out(c, now), "you": True}
    if c["status"] == "offered" and c["offered_session"] == s["id"]:
        return {**state_out(c, now), "offer_for_you": True}
    return None
