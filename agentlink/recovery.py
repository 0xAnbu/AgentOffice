"""Recovery supervisor: a deterministic, idempotent reconciler that runs inside the single backend.

It never needs an LLM. Each pass judges persisted state against the server clock and repairs it in
short transactions, emitting the durable event for every transition it makes:

1. sessions without heartbeat  -> terminated (``session.expired``), their task leases orphaned
2. executing without progress  -> ``session.stalled`` (once); the task lease is then not renewed
3. task lease expired          -> ``orphaned`` (claimable at once) -> ``open`` after ``orphan_hold``
4. dependency failed/cancelled -> dependent ``blocked``; cleared again when the dependency recovers
5. long waits / blocks         -> ``task.wait_timeout`` alert (human intervention), never an endless wait
6. path leases past TTL        -> deleted (``lease.expired``)
7. coordinator lease expired   -> ``coordinator.lost`` + election; unanswered offers expire
8. urgent mention unanswered   -> ``delivery.overdue`` alert

Running it twice, concurrently, or right after a restart is safe: every repair is a conditional
UPDATE on the current state, so a second pass finds nothing to do.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .config import Settings
from .db import Database, Executor, Row
from .events import emit
from .security import now_ms
from .sessions import SESSION_SELECT, derived_status

log = logging.getLogger("agentlink.recovery")


def orphan_task(tx: Executor, t: Row, reason: str, now: int) -> bool:
    """Invalidate the holder's authority and make the task reclaimable. Keeps the epoch (next claim bumps it)."""
    n = tx.execute(
        "UPDATE tasks SET status = 'orphaned', assignee_id = NULL, assignee_session = NULL, lease_expires_at = NULL, "
        "orphaned_at = ?, version = version + 1, updated_at = ? "
        "WHERE id = ? AND lease_epoch = ? AND status IN ('claimed', 'in_progress', 'blocked', 'review')",
        (now, now, t["id"], t["lease_epoch"]),
    )
    if not n:
        return False
    cp = tx.one("SELECT created_at, data FROM checkpoints WHERE task_id = ? ORDER BY id DESC LIMIT 1", (t["id"],))
    holder = tx.one("SELECT name FROM agents WHERE id = ?", (t["assignee_id"],)) if t["assignee_id"] else None
    data = {
        "reason": reason,
        "previous_holder": holder["name"] if holder else None,
        "previous_session": t["assignee_session"],
        "epoch": t["lease_epoch"],
        "from": t["status"],
        "checkpoint_age_s": (now - int(cp["created_at"])) // 1000 if cp else None,
        "checkpoint": json.loads(cp["data"]) if cp else None,
    }
    emit(tx, "task.orphaned", project_id=t["project_id"], channel_id=int(t["channel_id"]), task_id=int(t["id"]),
         session_id=t["assignee_session"], data=data)  # fmt: skip
    for lease in tx.query("SELECT id, resource FROM leases WHERE task_id = ?", (t["id"],)):
        tx.execute("DELETE FROM leases WHERE id = ?", (lease["id"],))
        emit(tx, "lease.released", project_id=t["project_id"], channel_id=int(t["channel_id"]), task_id=int(t["id"]),
             data={"resource": lease["resource"], "reason": "task orphaned"})  # fmt: skip
    ch = tx.one("SELECT * FROM channels WHERE id = ?", (t["channel_id"],))
    creator = tx.one("SELECT name FROM agents WHERE id = ?", (t["created_by"],))
    if ch:
        from .coordination import sys_message

        age = f"last checkpoint {data['checkpoint_age_s']}s old" if cp else "no checkpoint"
        sys_message(
            tx, None, ch,
            f"[recovery] task #{t['id']} orphaned ({reason}); previous holder @{data['previous_holder']} lost authority "
            f"(epoch {t['lease_epoch']}). {age}. It can be claimed again; verify the real repository state first.",
            {"task_id": int(t["id"]), "event": "orphaned", "reason": reason, "epoch": t["lease_epoch"]},
            [str(creator["name"])] if creator else None,
        )  # fmt: skip
    return True


def terminate_session(
    tx: Executor, s: Row, reason: str, event_type: str, actor_id: int | None, stats: dict[str, int] | None = None
) -> int:
    """End a session and its sub-agent sessions; orphan the tasks and drop the leases they held."""
    now = now_ms()
    ended = 0
    stack = [s["id"]]
    while stack:
        sid = stack.pop()
        n = tx.execute(
            "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ? AND ended_at IS NULL", (now, reason, sid)
        )
        if not n:
            continue
        ended += 1
        stack.extend(
            r["id"] for r in tx.query("SELECT id FROM sessions WHERE parent_id = ? AND ended_at IS NULL", (sid,))
        )
        emit(
            tx, event_type, project_id=int(s["project_id"]), actor_id=actor_id, session_id=sid, data={"reason": reason}
        )
        for t in tx.query("SELECT * FROM tasks WHERE assignee_session = ?", (sid,)):
            if orphan_task(tx, t, f"session {reason}", now) and stats is not None:
                stats["tasks_orphaned"] += 1
        for lease in tx.query("SELECT id, channel_id, resource, task_id FROM leases WHERE session_id = ?", (sid,)):
            tx.execute("DELETE FROM leases WHERE id = ?", (lease["id"],))
            emit(tx, "lease.released", project_id=int(s["project_id"]), channel_id=int(lease["channel_id"]),
                 session_id=sid, data={"resource": lease["resource"], "reason": f"session {reason}"})  # fmt: skip
        c = tx.one("SELECT * FROM coordinators WHERE project_id = ? AND status = 'active' AND session_id = ?",
                   (s["project_id"], sid))  # fmt: skip
        if c:
            _lose_coordinator(tx, c, f"session {reason}", now)
            if stats is not None:
                stats["coordinators_lost"] += 1
    return ended


def _lose_coordinator(tx: Executor, c: Row, reason: str, now: int) -> None:
    n = tx.execute(
        "UPDATE coordinators SET status = 'none', session_id = NULL, lease_expires_at = NULL, declined = NULL, "
        "proposed_session = NULL, vacancy_notified = 0, updated_at = ? WHERE project_id = ? AND epoch = ? AND status = 'active'",
        (now, c["project_id"], c["epoch"]),
    )
    if n:
        emit(tx, "coordinator.lost", project_id=int(c["project_id"]), session_id=c["session_id"],
             data={"epoch": c["epoch"], "reason": reason})  # fmt: skip


def reconcile(db: Database, settings: Settings) -> dict[str, int]:
    """One reconciliation pass. Returns counters (useful for tests, logs and health checks)."""
    now = now_ms()
    stats = {k: 0 for k in ("sessions_expired", "sessions_stalled", "tasks_orphaned", "tasks_recovered", "tasks_blocked",
                            "tasks_unblocked", "wait_timeouts", "leases_expired", "coordinators_lost", "offers_expired",
                            "offers_made", "deliveries_overdue")}  # fmt: skip

    # 1-2. sessions
    for s in db.query(f"{SESSION_SELECT} WHERE s.ended_at IS NULL"):
        st = derived_status(s, now, settings)
        if st == "offline":
            with db.transaction() as tx:
                cur = tx.one(f"{SESSION_SELECT} WHERE s.id = ?", (s["id"],))
                if cur and cur["ended_at"] is None and derived_status(cur, now, settings) == "offline":
                    stats["sessions_expired"] += terminate_session(tx, cur, "expired", "session.expired", None, stats)
        elif st == "stalled" and s["stalled_since"] is None:
            with db.transaction() as tx:
                if tx.execute(
                    "UPDATE sessions SET stalled_since = ? WHERE id = ? AND stalled_since IS NULL", (now, s["id"])
                ):
                    last = int(s["last_progress_at"] or s["created_at"])
                    emit(tx, "session.stalled", project_id=int(s["project_id"]), session_id=s["id"],
                         data={"agent": s["agent"], "no_progress_s": (now - last) // 1000})  # fmt: skip
                    stats["sessions_stalled"] += 1

    # 3. task leases
    with db.transaction() as tx:
        # Legacy rows (claimed before leases existed) get a finite lease instead of an infinite one.
        tx.execute(
            "UPDATE tasks SET lease_expires_at = ? WHERE status IN ('claimed', 'in_progress', 'blocked', 'review') "
            "AND assignee_id IS NOT NULL AND lease_expires_at IS NULL",
            (now + settings.task_lease_default * 1000,),
        )
        for t in tx.query(
            "SELECT * FROM tasks WHERE status IN ('claimed', 'in_progress', 'blocked', 'review') AND lease_expires_at <= ?",
            (now,),
        ):
            stats["tasks_orphaned"] += orphan_task(tx, t, "lease expired", now)
        for t in tx.query(
            "SELECT * FROM tasks WHERE status = 'orphaned' AND orphaned_at <= ?", (now - settings.orphan_hold * 1000,)
        ):
            if tx.execute("UPDATE tasks SET status = 'open', version = version + 1, updated_at = ? WHERE id = ? AND status = 'orphaned'",
                          (now, t["id"])):  # fmt: skip
                emit(tx, "task.recovered", project_id=t["project_id"], channel_id=int(t["channel_id"]), task_id=int(t["id"]),
                     data={"epoch": t["lease_epoch"], "state": "open"})  # fmt: skip
                stats["tasks_recovered"] += 1

    # 4-5. dependencies and long waits
    with db.transaction() as tx:
        for r in tx.query(
            "SELECT t.id, t.project_id, t.channel_id, MIN(d.depends_on) AS dep FROM tasks t JOIN task_deps d ON d.task_id = t.id "
            "JOIN tasks dt ON dt.id = d.depends_on WHERE t.status = 'open' AND dt.status IN ('failed', 'cancelled') "
            "GROUP BY t.id, t.project_id, t.channel_id"
        ):
            reason = f"dependency_failed:#{r['dep']}"
            if tx.execute("UPDATE tasks SET status = 'blocked', block_reason = ?, version = version + 1, updated_at = ? "
                          "WHERE id = ? AND status = 'open'", (reason, now, r["id"])):  # fmt: skip
                emit(tx, "task.blocked", project_id=r["project_id"], channel_id=int(r["channel_id"]), task_id=int(r["id"]),
                     data={"reason": reason, "dependency": r["dep"]})  # fmt: skip
                stats["tasks_blocked"] += 1
        for t in tx.query(
            "SELECT id, project_id, channel_id FROM tasks WHERE status = 'blocked' AND block_reason LIKE 'dependency_failed:%'"
        ):
            bad = tx.one("SELECT COUNT(*) AS n FROM task_deps d JOIN tasks dt ON dt.id = d.depends_on WHERE d.task_id = ? "
                         "AND dt.status IN ('failed', 'cancelled')", (t["id"],))  # fmt: skip
            if bad and not int(bad["n"]) and tx.execute(
                "UPDATE tasks SET status = 'open', block_reason = NULL, version = version + 1, updated_at = ? WHERE id = ? "
                "AND status = 'blocked'", (now, t["id"])):  # fmt: skip
                emit(
                    tx,
                    "task.unblocked",
                    project_id=t["project_id"],
                    channel_id=int(t["channel_id"]),
                    task_id=int(t["id"]),
                )
                stats["tasks_unblocked"] += 1
        cutoff = now - settings.wait_timeout * 1000
        for t in tx.query(
            "SELECT t.id, t.project_id, t.channel_id, t.status, t.updated_at FROM tasks t "
            "WHERE t.status = 'blocked' AND t.updated_at <= ? AND NOT EXISTS (SELECT 1 FROM events e "
            "WHERE e.task_id = t.id AND e.type = 'task.wait_timeout' AND e.created_at >= t.updated_at)",
            (cutoff,),
        ):
            emit(tx, "task.wait_timeout", project_id=t["project_id"], channel_id=int(t["channel_id"]), task_id=int(t["id"]),
                 data={"status": t["status"], "waiting_s": (now - int(t["updated_at"])) // 1000})  # fmt: skip
            stats["wait_timeouts"] += 1

    # 6. path leases
    with db.transaction() as tx:
        for lease in tx.query(
            "SELECT l.id, l.channel_id, l.resource, l.task_id, l.session_id, a.name AS holder, c.project_id FROM leases l "
            "JOIN agents a ON a.id = l.holder_id JOIN channels c ON c.id = l.channel_id WHERE l.expires_at <= ?",
            (now,),
        ):
            tx.execute("DELETE FROM leases WHERE id = ?", (lease["id"],))
            emit(tx, "lease.expired", project_id=lease["project_id"], channel_id=int(lease["channel_id"]),
                 task_id=lease["task_id"], session_id=lease["session_id"],
                 data={"resource": lease["resource"], "holder": lease["holder"]})  # fmt: skip
            stats["leases_expired"] += 1

    # 7. coordinators
    from .coordinator import _row as coord_row
    from .coordinator import elect

    projects = db.query(
        "SELECT DISTINCT pr.* FROM projects pr WHERE EXISTS (SELECT 1 FROM sessions s WHERE s.project_id = pr.id) "
        "OR EXISTS (SELECT 1 FROM coordinators c WHERE c.project_id = pr.id AND c.epoch > 0)"
    )
    for pr in projects:
        with db.transaction() as tx:
            c = coord_row(tx, int(pr["id"]))
            lost = None
            if c["status"] == "active":
                sess = tx.one("SELECT ended_at FROM sessions WHERE id = ?", (c["session_id"],))
                expired = not c["lease_expires_at"] or int(c["lease_expires_at"]) <= now
                if expired or sess is None or sess["ended_at"] is not None:
                    _lose_coordinator(tx, c, "lease expired" if expired else "session ended", now)
                    stats["coordinators_lost"] += 1
                    lost = "The previous coordinator is no longer available."
            elif c["status"] == "offered" and c["offer_expires_at"] and int(c["offer_expires_at"]) <= now:
                declined = set(json.loads(c["declined"])) if c["declined"] else set()
                declined.add(str(c["offered_session"]))
                if tx.execute(
                    "UPDATE coordinators SET status = 'none', offered_session = NULL, offer_expires_at = NULL, declined = ?, "
                    "updated_at = ? WHERE project_id = ? AND epoch = ? AND status = 'offered'",
                    (json.dumps(sorted(declined)), now, pr["id"], c["epoch"]),
                ):
                    emit(tx, "coordinator.offer_expired", project_id=int(pr["id"]), session_id=c["offered_session"],
                         data={"epoch": c["epoch"]})  # fmt: skip
                    stats["offers_expired"] += 1
                    lost = "The previous candidate did not answer in time."
            c = coord_row(tx, int(pr["id"]))
            if c["status"] == "none" and elect(tx, pr, now, settings, lost).get("offered"):
                stats["offers_made"] += 1

    # 8. urgent mentions nobody acknowledged
    with db.transaction() as tx:
        for r in tx.query(
            "SELECT mn.message_id, mn.agent_id, m.channel_id, c.project_id, a.name AS recipient FROM mentions mn "
            "JOIN messages m ON m.id = mn.message_id JOIN channels c ON c.id = m.channel_id JOIN agents a ON a.id = mn.agent_id "
            "LEFT JOIN deliveries d ON d.message_id = mn.message_id AND d.agent_id = mn.agent_id "
            "WHERE m.priority = 1 AND m.created_at <= ? AND (d.state IS NULL OR d.state NOT IN ('acknowledged', 'processed')) "
            "AND (d.alerted IS NULL OR d.alerted = 0)",
            (now - settings.delivery_overdue * 1000,),
        ):
            tx.execute(
                "INSERT INTO deliveries(message_id, agent_id, state, updated_at, alerted) VALUES (?, ?, 'queued', ?, 1) "
                "ON CONFLICT (message_id, agent_id) DO UPDATE SET alerted = 1",
                (r["message_id"], r["agent_id"], now),
            )
            emit(tx, "delivery.overdue", project_id=r["project_id"], channel_id=int(r["channel_id"]), message_id=int(r["message_id"]),
                 data={"recipient": r["recipient"], "overdue_s": settings.delivery_overdue})  # fmt: skip
            stats["deliveries_overdue"] += 1

    with db.transaction() as tx:
        tx.execute(
            "INSERT INTO meta(key, value) VALUES ('reconciler_at', ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (str(now),),
        )
    if any(stats.values()):
        log.info("reconcile: %s", {k: n for k, n in stats.items() if n})
    return stats


def last_run(db: Database) -> int | None:
    row = db.one("SELECT value FROM meta WHERE key = 'reconciler_at'")
    return int(row["value"]) if row else None


def health(db: Database, settings: Settings) -> dict[str, Any]:
    """Deep health: database read/write and a recent reconciler pass. Used by deployment/healthcheck.sh."""
    out: dict[str, Any] = {"db": False, "reconciler": False}
    try:
        with db.transaction() as tx:
            tx.execute(
                "INSERT INTO meta(key, value) VALUES ('health_probe', ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (str(now_ms()),),
            )
        out["db"] = True
        last = last_run(db)
        limit = max(30.0, settings.reconcile_interval * 6) * 1000
        out["reconciler_age_s"] = None if last is None else (now_ms() - last) / 1000
        out["reconciler"] = settings.reconcile_interval > 0 and last is not None and now_ms() - last < limit
    except Exception as exc:  # pragma: no cover - reported, not raised
        out["error"] = type(exc).__name__
    out["ok"] = bool(out["db"] and out["reconciler"])
    return out
