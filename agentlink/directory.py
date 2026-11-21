"""Identities, credentials, channels, membership and access control."""

from __future__ import annotations

from typing import Any

from . import validation as v
from .db import Database, Executor, Row
from .errors import BadRequest, Conflict, Forbidden, NotFound
from .security import ADMIN_AGENT, Principal, hash_token, new_token, now_ms

DAY_MS = 86_400_000


def audit(db: Executor, actor: Principal | None, action: str, target: str = "", detail: str = "") -> None:
    db.execute(
        "INSERT INTO audit(actor_id, action, target, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (actor.agent_id if actor else None, action, target, detail[:500], now_ms()),
    )


def audit_log(db: Database, p: Principal, limit: int = 200) -> list[Row]:
    p.require_admin()
    return db.query(
        "SELECT a.id, g.name AS actor, a.action, a.target, a.detail, a.created_at FROM audit a "
        "LEFT JOIN agents g ON g.id = a.actor_id ORDER BY a.id DESC LIMIT ?",
        (limit,),
    )


# -- agents & tokens ---------------------------------------------------------


def agent_by_name(db: Executor, name: str) -> Row:
    row = db.one("SELECT id, name, role, description, disabled, created_at FROM agents WHERE name = ?", (name,))
    if row is None:
        raise NotFound(f"agent '{name}' not found")
    return row


def create_agent(db: Database, p: Principal, payload: dict[str, Any]) -> dict[str, Any]:
    p.require_admin()
    name = v.name(payload.get("name"))
    role = v.choice(payload.get("role"), "role", v.ROLES, default="member")
    desc = v.text(payload.get("description"), "description", 500)
    ttl_days = v.integer(payload.get("ttl_days"), "ttl_days", default=0, hi=3650)
    owner = v.text(payload.get("owner"), "owner", 64).strip()
    spec = v.text(payload.get("specialization"), "specialization", 64).strip().lower()
    prio = v.integer(payload.get("coord_priority"), "coord_priority", default=0, lo=0, hi=100)
    projects = v.str_list(payload.get("projects"), "projects", max_items=50, max_len=64)
    with db.transaction() as tx:
        if tx.one("SELECT 1 AS x FROM agents WHERE name = ?", (name,)):
            raise Conflict(f"agent '{name}' already exists")
        row = tx.one(
            "INSERT INTO agents(name, role, description, owner, specialization, coord_priority, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (name, role, desc, owner, spec, prio, now_ms()),
        )
        assert row is not None
        token, token_id = _issue(tx, int(row["id"]), "initial", ttl_days)
        for pname in projects:
            pr = tx.one("SELECT id FROM projects WHERE name = ?", (v.name(pname, "project"),))
            if pr is None:
                raise NotFound(f"project '{pname}' not found")
            tx.execute("INSERT INTO project_members(project_id, agent_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                       (pr["id"], row["id"]))  # fmt: skip
        audit(tx, p, "agent.create", name, f"role={role} owner={owner}")
    return {
        "agent": {"id": row["id"], "name": name, "role": role, "owner": owner, "specialization": spec},
        "token": token,
        "token_id": token_id,
        "note": "Store this token now; it is never shown again.",
    }


def _issue(db: Executor, agent_id: int, label: str, ttl_days: int) -> tuple[str, int]:
    token = new_token()
    expires = now_ms() + ttl_days * DAY_MS if ttl_days else None
    row = db.one(
        "INSERT INTO tokens(agent_id, token_hash, label, created_at, expires_at) VALUES (?, ?, ?, ?, ?) RETURNING id",
        (agent_id, hash_token(token), label, now_ms(), expires),
    )
    assert row is not None
    return token, int(row["id"])


def issue_token(db: Database, p: Principal, agent_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    p.require_admin()
    agent = agent_by_name(db, v.name(agent_name))
    label = v.text(payload.get("label"), "label", 100) or "rotated"
    ttl_days = v.integer(payload.get("ttl_days"), "ttl_days", default=0, hi=3650)
    with db.transaction() as tx:
        token, token_id = _issue(tx, int(agent["id"]), label, ttl_days)
        audit(tx, p, "token.create", agent["name"], label)
    return {"agent": agent["name"], "token": token, "token_id": token_id}


def revoke_token(db: Database, p: Principal, token_id: int) -> dict[str, Any]:
    p.require_admin()
    n = db.execute("UPDATE tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (now_ms(), token_id))
    if not n:
        raise NotFound("token not found or already revoked")
    audit(db, p, "token.revoke", str(token_id))
    return {"revoked": token_id}


def set_agent_disabled(db: Database, p: Principal, agent_name: str, disabled: bool) -> dict[str, Any]:
    p.require_admin()
    agent = agent_by_name(db, v.name(agent_name))
    if agent["name"] == ADMIN_AGENT:
        raise BadRequest("the bootstrap admin is controlled by AGENTLINK_ADMIN_TOKEN")
    db.execute("UPDATE agents SET disabled = ? WHERE id = ?", (1 if disabled else 0, agent["id"]))
    audit(db, p, "agent.disable" if disabled else "agent.enable", agent["name"])
    return {"agent": agent["name"], "disabled": disabled}


def update_agent(db: Database, p: Principal, agent_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Admin-only: identity metadata (owner, specialization, coordinator priority). Never self-service."""
    p.require_admin()
    payload = v.obj(payload)
    agent = agent_by_name(db, v.name(agent_name))
    sets: list[str] = []
    params: list[Any] = []
    for f, lim in (("owner", 64), ("specialization", 64), ("description", 500)):
        if f in payload:
            sets.append(f"{f} = ?")
            val = v.text(payload[f], f, lim).strip()
            params.append(val.lower() if f == "specialization" else val)
    if "coord_priority" in payload:
        sets.append("coord_priority = ?")
        params.append(v.integer(payload["coord_priority"], "coord_priority", lo=0, hi=100))
    if sets:
        db.execute(f"UPDATE agents SET {', '.join(sets)} WHERE id = ?", (*params, agent["id"]))
        audit(db, p, "agent.update", agent["name"], ",".join(sorted(k for k in payload)))
    row = db.one("SELECT name, role, owner, specialization, coord_priority FROM agents WHERE id = ?", (agent["id"],))
    return dict(row or {})


DELETED_MARK = " (deleted #"  # never matches NAME_RE, so a tombstone can't be targeted or recreated


def delete_agent(db: Database, p: Principal, agent_name: str, purge_messages: bool = False) -> dict[str, Any]:
    """Admin-only: remove an agent and its credentials, memberships, leases, presence and mentions.

    The agents row survives as a disabled tombstone renamed ``"<name> (deleted #<id>)"`` so past
    messages, tasks and context keep a readable author and the name can be reused.
    ``purge_messages`` also deletes every message the agent wrote.
    """
    p.require_admin()
    agent = agent_by_name(db, v.name(agent_name))
    aid = int(agent["id"])
    if agent["name"] == ADMIN_AGENT:
        raise BadRequest("the bootstrap admin is controlled by AGENTLINK_ADMIN_TOKEN and can't be deleted")
    if aid == p.agent_id:
        raise BadRequest("you can't delete the agent you are authenticated as")
    with db.transaction() as tx:
        purged = 0
        if purge_messages:
            tx.execute("DELETE FROM mentions WHERE message_id IN (SELECT id FROM messages WHERE author_id = ?)", (aid,))
            purged = tx.execute("DELETE FROM messages WHERE author_id = ?", (aid,))
        tokens = tx.execute("DELETE FROM tokens WHERE agent_id = ?", (aid,))
        for sql in (
            "DELETE FROM members WHERE agent_id = ?",
            "DELETE FROM leases WHERE holder_id = ?",
            "DELETE FROM presence WHERE agent_id = ?",
            "DELETE FROM mentions WHERE agent_id = ?",
            "UPDATE tasks SET status = CASE WHEN status IN ('claimed', 'in_progress', 'blocked', 'review', 'orphaned') "
            "THEN 'open' ELSE status END, assignee_id = NULL, assignee_session = NULL, lease_expires_at = NULL "
            "WHERE assignee_id = ? AND status NOT IN ('done', 'cancelled', 'failed')",
            "UPDATE sessions SET ended_at = COALESCE(ended_at, 0), end_reason = COALESCE(end_reason, 'agent deleted') "
            "WHERE agent_id = ?",
            "DELETE FROM project_members WHERE agent_id = ?",
        ):
            tx.execute(sql, (aid,))
        tx.execute(
            "UPDATE agents SET name = ?, disabled = 1, description = '' WHERE id = ?",
            (f"{agent['name']}{DELETED_MARK}{aid})", aid),
        )
        audit(tx, p, "agent.delete", agent["name"], f"tokens={tokens} messages_purged={purged}")
    return {"deleted": agent["name"], "tokens": tokens, "messages_purged": purged}


def list_agents(db: Database, p: Principal) -> list[dict[str, Any]]:
    agents = db.query(
        "SELECT id, name, role, description, disabled, created_at, owner, specialization FROM agents "
        "WHERE name NOT LIKE ? ORDER BY name",
        (f"%{DELETED_MARK}%",),
    )
    tokens: dict[int, list[Row]] = {}
    if p.is_admin:
        for t in db.query("SELECT id, agent_id, label, created_at, expires_at, revoked_at, last_used_at FROM tokens"):
            tokens.setdefault(int(t["agent_id"]), []).append(t)
    out = []
    for a in agents:
        item = {"name": a["name"], "role": a["role"], "description": a["description"], "disabled": bool(a["disabled"]),
                "owner": a["owner"], "specialization": a["specialization"]}  # fmt: skip
        if p.is_admin:
            item["tokens"] = [
                {k: t[k] for k in ("id", "label", "created_at", "expires_at", "revoked_at", "last_used_at")}
                for t in tokens.get(int(a["id"]), [])
            ]
        out.append(item)
    return out


# -- channels & access ---------------------------------------------------------


def _channel_row(db: Executor, ident: str | int) -> Row:
    if isinstance(ident, int) or (isinstance(ident, str) and ident.isdigit()):
        row = db.one("SELECT * FROM channels WHERE id = ?", (int(ident),))
    else:
        row = db.one("SELECT * FROM channels WHERE name = ?", (ident,))
    if row is None:
        raise NotFound(f"channel '{ident}' not found")
    return row


def is_member(db: Executor, channel_id: int, agent_id: int) -> bool:
    return (
        db.one("SELECT 1 AS x FROM members WHERE channel_id = ? AND agent_id = ?", (channel_id, agent_id)) is not None
    )


def channel_for(db: Executor, p: Principal, ident: str | int, *, write: bool = False) -> Row:
    """Resolve a channel and enforce access. Every channel-scoped operation calls this."""
    ch = _channel_row(db, ident)
    if write:
        p.require_writer()
    if p.is_admin:
        return ch
    if p.role == "monitor" and not write:
        scoped = db.one("SELECT 1 AS x FROM project_members WHERE agent_id = ?", (p.agent_id,))
        if scoped is None or db.one(
            "SELECT 1 AS x FROM project_members WHERE agent_id = ? AND project_id = ?", (p.agent_id, ch["project_id"])
        ):
            return ch  # unscoped monitors read everything (legacy); scoped ones only their projects
        raise Forbidden(f"monitor not scoped to the project of channel '{ch['name']}'")
    if not is_member(db, int(ch["id"]), p.agent_id):
        raise Forbidden(f"not a member of channel '{ch['name']}'")
    return ch


def visible_channel_ids_sql(p: Principal, column: str) -> tuple[str, tuple[Any, ...]]:
    """SQL fragment restricting ``column`` (a channel id) to channels ``p`` may read.

    ``column`` must be a trusted literal such as ``"m.channel_id"``, never user input.
    """
    if p.is_admin:
        return "1 = 1", ()
    if p.role == "monitor":
        return (
            f"(NOT EXISTS (SELECT 1 FROM project_members pmx WHERE pmx.agent_id = ?) OR {column} IN "
            f"(SELECT cx.id FROM channels cx JOIN project_members pmy ON pmy.project_id = cx.project_id WHERE pmy.agent_id = ?))",
            (p.agent_id, p.agent_id),
        )
    return f"{column} IN (SELECT mb.channel_id FROM members mb WHERE mb.agent_id = ?)", (p.agent_id,)


def create_channel(db: Database, p: Principal, payload: dict[str, Any]) -> dict[str, Any]:
    p.require_writer()
    name = v.name(payload.get("name"), "channel name")
    repo = v.repo(payload.get("repo"))
    desc = v.text(payload.get("description"), "description", 1000)
    member_names = v.str_list(payload.get("members"), "members", max_items=200, max_len=64)
    from .projects import default_project_id, ensure_member, project_for

    pid = int(project_for(db, p, payload["project"], write=True)["id"]) if payload.get("project") else None
    with db.transaction() as tx:
        if tx.one("SELECT 1 AS x FROM channels WHERE name = ?", (name,)):
            raise Conflict(f"channel '{name}' already exists")
        if pid is None:
            pid = default_project_id(tx)
        row = tx.one(
            "INSERT INTO channels(name, repo, description, created_by, created_at, project_id) "
            "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            (name, repo, desc, p.agent_id, now_ms(), pid),
        )
        assert row is not None
        cid = int(row["id"])
        ids = {p.agent_id} | {int(agent_by_name(tx, v.name(m, "member"))["id"]) for m in member_names}
        for aid in ids:
            tx.execute("INSERT INTO members(channel_id, agent_id) VALUES (?, ?)", (cid, aid))
            ensure_member(tx, pid, aid)  # channel members are project participants
        audit(tx, p, "channel.create", name, f"repo={repo or ''}")
    return get_channel(db, p, name)


def get_channel(db: Database, p: Principal, ident: str | int) -> dict[str, Any]:
    ch = channel_for(db, p, ident)
    members = db.query(
        "SELECT a.name, a.role FROM members m JOIN agents a ON a.id = m.agent_id "
        "WHERE m.channel_id = ? ORDER BY a.name",
        (ch["id"],),
    )
    last = db.one("SELECT MAX(id) AS last_id FROM messages WHERE channel_id = ?", (ch["id"],))
    proj = db.one("SELECT name FROM projects WHERE id = ?", (ch["project_id"],)) if ch["project_id"] else None
    return {
        "id": ch["id"],
        "name": ch["name"],
        "project": proj["name"] if proj else None,
        "repo": ch["repo"],
        "description": ch["description"],
        "members": [m["name"] for m in members],
        "last_message_id": (last or {}).get("last_id") or 0,
    }


def list_channels(db: Database, p: Principal) -> list[dict[str, Any]]:
    where, params = visible_channel_ids_sql(p, "c.id")
    rows = db.query(
        "SELECT c.id, c.name, c.repo, c.description, (SELECT pr.name FROM projects pr WHERE pr.id = c.project_id) AS project, "
        "(SELECT MAX(m.id) FROM messages m WHERE m.channel_id = c.id) AS last_message_id "
        f"FROM channels c WHERE {where} ORDER BY c.name",
        params,
    )
    return [{**r, "last_message_id": r["last_message_id"] or 0} for r in rows]


def set_membership(db: Database, p: Principal, ident: str, agent_name: Any, add: bool) -> dict[str, Any]:
    ch = channel_for(db, p, ident, write=True)  # members may invite; admin may do anything
    agent = agent_by_name(db, v.name(agent_name))
    if not add and not p.is_admin and int(agent["id"]) != p.agent_id:
        raise Forbidden("only admins can remove other members")
    if add:
        from .projects import ensure_member

        db.execute(
            "INSERT INTO members(channel_id, agent_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
            (ch["id"], agent["id"]),
        )
        if ch["project_id"] is not None:
            ensure_member(db, int(ch["project_id"]), int(agent["id"]))
    else:
        db.execute("DELETE FROM members WHERE channel_id = ? AND agent_id = ?", (ch["id"], agent["id"]))
    audit(db, p, "member.add" if add else "member.remove", ch["name"], agent["name"])
    return get_channel(db, p, ch["name"]) if (add or p.is_admin) else {"left": ch["name"]}


def delete_channel(db: Database, p: Principal, ident: str) -> dict[str, Any]:
    """Admin-only: remove a channel and everything in it (messages, tasks, leases, context)."""
    p.require_admin()
    with db.transaction() as tx:
        ch = _channel_row(tx, ident)
        cid = int(ch["id"])
        tx.execute("DELETE FROM mentions WHERE message_id IN (SELECT id FROM messages WHERE channel_id = ?)", (cid,))
        n = tx.execute("DELETE FROM messages WHERE channel_id = ?", (cid,))
        tx.execute("DELETE FROM task_deps WHERE task_id IN (SELECT id FROM tasks WHERE channel_id = ?) "
                   "OR depends_on IN (SELECT id FROM tasks WHERE channel_id = ?)", (cid, cid))  # fmt: skip
        tx.execute("DELETE FROM checkpoints WHERE task_id IN (SELECT id FROM tasks WHERE channel_id = ?)", (cid,))
        for table in ("tasks", "leases", "kv", "members"):
            tx.execute(f"DELETE FROM {table} WHERE channel_id = ?", (cid,))
        tx.execute("UPDATE presence SET channel_id = NULL WHERE channel_id = ?", (cid,))
        tx.execute("DELETE FROM channels WHERE id = ?", (cid,))
        audit(tx, p, "channel.delete", ch["name"], f"messages={n}")
    return {"deleted": ch["name"], "messages": n}


def channels_for_repo(db: Executor, repo: str) -> list[Row]:
    return db.query("SELECT id, name FROM channels WHERE LOWER(repo) = LOWER(?)", (repo,))


def whoami(db: Database, p: Principal) -> dict[str, Any]:
    me = db.one("SELECT owner, specialization FROM agents WHERE id = ?", (p.agent_id,)) or {}
    projects = db.query(
        "SELECT pr.name, pm.role FROM project_members pm JOIN projects pr ON pr.id = pm.project_id WHERE pm.agent_id = ? "
        "ORDER BY pr.name",
        (p.agent_id,),
    )
    return {
        "agent": p.name,
        "role": p.role,
        "owner": me.get("owner") or "",
        "specialization": me.get("specialization") or "",
        "channels": [c["name"] for c in list_channels(db, p)],
        "projects": projects,
    }
