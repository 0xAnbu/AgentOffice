"""Delivery receipts for messages addressed to an agent (mentions = its inbox).

States, strictly ordered and monotonic (a report can never move a receipt backwards)::

    queued                   implicit: the mention exists, nobody has reported anything yet
    delivered_to_supervisor  the recipient's local supervisor downloaded AND persisted it locally
    injected_to_runtime      the runtime adapter performed its delivery operation (hook output, queue,
                             prompt). Says nothing about whether the model read it
    acknowledged             explicit protocol confirmation from the agent (e.g. `agentlink ack`)
    processed                requires evidence: a reply message authored by the recipient, or a
                             task/checkpoint reference. Never inferred from delivery

Delivery to the supervisor is at-least-once (cursor + local store + idempotent receipts);
processing by an LLM is never exactly-once, so effects must be idempotent or state-checked.
"""

from __future__ import annotations

import json
from typing import Any

from . import validation as v
from .db import Database
from .errors import BadRequest, Forbidden, NotFound
from .security import Principal, now_ms

STATES = ("queued", "delivered_to_supervisor", "injected_to_runtime", "acknowledged", "processed")
RANK = {s: i for i, s in enumerate(STATES)}
EVIDENCE_KEYS = {"reply_message_id", "task_id", "checkpoint", "note", "adapter", "mechanism"}


def report(db: Database, p: Principal, payload: dict[str, Any]) -> dict[str, Any]:
    p.require_writer()
    payload = v.obj(payload)
    state = v.choice(payload.get("state"), "state", STATES[1:])
    ids = payload.get("message_ids")
    if not isinstance(ids, list) or not ids or len(ids) > 200:
        raise BadRequest("message_ids must be a non-empty list (max 200)")
    mids = sorted({v.integer(x, "message_ids", lo=1) for x in ids})
    sid = v.text(payload.get("session_id"), "session_id", 64) or None
    evidence = payload.get("evidence")
    if evidence is not None:
        evidence = v.obj(evidence)
        if set(evidence) - EVIDENCE_KEYS:
            raise BadRequest(f"evidence keys must be from {sorted(EVIDENCE_KEYS)}")
    if state == "processed" and (not evidence or not ({"reply_message_id", "task_id", "checkpoint"} & set(evidence))):
        raise BadRequest("processed requires evidence: reply_message_id, task_id or checkpoint")
    now = now_ms()
    updated, unchanged = [], []
    with db.transaction() as tx:
        if sid:
            s = tx.one("SELECT agent_id FROM sessions WHERE id = ?", (sid,))
            if s is None or int(s["agent_id"]) != p.agent_id:
                raise Forbidden("session does not belong to you")
        if evidence and evidence.get("reply_message_id"):
            r = tx.one(
                "SELECT author_id FROM messages WHERE id = ?",
                (v.integer(evidence["reply_message_id"], "reply_message_id"),),
            )
            if r is None or int(r["author_id"]) != p.agent_id:
                raise BadRequest("reply_message_id must be a message you authored")
        for mid in mids:
            if tx.one("SELECT 1 AS x FROM mentions WHERE message_id = ? AND agent_id = ?", (mid, p.agent_id)) is None:
                raise NotFound(f"message {mid} is not addressed to you")
            cur = tx.one("SELECT state FROM deliveries WHERE message_id = ? AND agent_id = ?", (mid, p.agent_id))
            if cur and RANK[str(cur["state"])] >= RANK[state]:
                unchanged.append(mid)  # idempotent / out-of-order report: never move backwards
                continue
            tx.execute(
                "INSERT INTO deliveries(message_id, agent_id, session_id, state, evidence, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (message_id, agent_id) DO UPDATE SET state = excluded.state, "
                "session_id = COALESCE(excluded.session_id, deliveries.session_id), "
                "evidence = COALESCE(excluded.evidence, deliveries.evidence), updated_at = excluded.updated_at",
                (mid, p.agent_id, sid, state, json.dumps(evidence) if evidence else None, now),
            )
            updated.append(mid)
    return {"state": state, "updated": updated, "unchanged": unchanged}


def status(db: Database, p: Principal, message_id: int) -> dict[str, Any]:
    m = db.one("SELECT author_id, channel_id FROM messages WHERE id = ?", (message_id,))
    if m is None:
        raise NotFound(f"message {message_id} not found")
    recipients = db.query(
        "SELECT a.id, a.name, d.state, d.session_id, d.evidence, d.updated_at FROM mentions mn JOIN agents a ON a.id = mn.agent_id "
        "LEFT JOIN deliveries d ON d.message_id = mn.message_id AND d.agent_id = mn.agent_id WHERE mn.message_id = ? ORDER BY a.name",
        (message_id,),
    )
    allowed = p.is_admin or int(m["author_id"]) == p.agent_id or any(int(r["id"]) == p.agent_id for r in recipients)
    if not allowed:
        from .directory import channel_for

        channel_for(db, p, int(m["channel_id"]))  # channel members and monitors that can see the channel
    return {
        "message_id": message_id,
        "recipients": [
            {"agent": r["name"], "state": r["state"] or "queued", "session_id": r["session_id"],
             "evidence": json.loads(r["evidence"]) if r["evidence"] else None, "updated_at": r["updated_at"]}
            for r in recipients
        ],
    }  # fmt: skip


def pending(db: Database, p: Principal, q: dict[str, Any]) -> dict[str, Any]:
    """Messages addressed to me that are not yet acknowledged/processed (supervisor reconciliation after restart)."""
    limit = v.integer(q.get("limit"), "limit", default=100, lo=1, hi=500)
    rows = db.query(
        "SELECT mn.message_id, COALESCE(d.state, 'queued') AS state, m.priority FROM mentions mn JOIN messages m ON m.id = mn.message_id "
        "LEFT JOIN deliveries d ON d.message_id = mn.message_id AND d.agent_id = mn.agent_id WHERE mn.agent_id = ? "
        "AND (d.state IS NULL OR d.state NOT IN ('acknowledged', 'processed')) ORDER BY m.priority, mn.message_id LIMIT ?",
        (p.agent_id, limit),
    )
    return {"pending": rows}
