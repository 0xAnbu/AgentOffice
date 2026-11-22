"""Projects: the unit of isolation. A project owns channels (and through them messages, tasks and leases),
its participants, its sessions and exactly one coordinator slot.

Authorization is resolved from the authenticated principal plus persisted membership, never from
fields the client sends (`owner`, `coordinator`, ...).
"""

from __future__ import annotations

from typing import Any

from . import validation as v
from .db import Database, Executor, Row
from .directory import agent_by_name, audit
from .errors import Conflict, Forbidden, NotFound
from .events import emit, scoped_monitor
from .security import Principal, now_ms

PROJECT_ROLES = ("member", "admin")


def _row(db: Executor, ident: str | int) -> Row:
    if isinstance(ident, int) or (isinstance(ident, str) and ident.isdigit()):
        row = db.one("SELECT * FROM projects WHERE id = ?", (int(ident),))
    else:
        row = db.one("SELECT * FROM projects WHERE name = ?", (ident,))
    if row is None:
        raise NotFound(f"project '{ident}' not found")
    return row


def membership(db: Executor, project_id: int, agent_id: int) -> str | None:
    row = db.one("SELECT role FROM project_members WHERE project_id = ? AND agent_id = ?", (project_id, agent_id))
    return str(row["role"]) if row else None


def project_for(db: Executor, p: Principal, ident: str | int, *, write: bool = False, admin: bool = False) -> Row:
    """Resolve a project and enforce access. Every project-scoped operation calls this."""
    pr = _row(db, ident)
    if write:
        p.require_writer()
    if p.is_admin:
        return pr
    role = membership(db, int(pr["id"]), p.agent_id)
    if p.role == "monitor" and not write and (role or not scoped_monitor(db, p)):
        return pr
    if role is None:
        raise Forbidden(f"not a member of project '{pr['name']}'")
    if admin and role != "admin":
        raise Forbidden(f"project admin role required in '{pr['name']}'")
    return pr


def ensure_member(tx: Executor, project_id: int, agent_id: int, role: str = "member") -> None:
    tx.execute(
        "INSERT INTO project_members(project_id, agent_id, role) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
        (project_id, agent_id, role),
    )


def default_project_id(db: Executor) -> int:
    row = db.one("SELECT id FROM projects WHERE name = 'default'")
    if row is None:  # recreated if an admin deleted it
        row = db.one(
            "INSERT INTO projects(name, description, created_at) VALUES ('default', 'Default project', ?) RETURNING id",
            (now_ms(),),
        )
    assert row is not None
    return int(row["id"])


def create_project(db: Database, p: Principal, payload: dict[str, Any]) -> dict[str, Any]:
    p.require_writer()
    payload = v.obj(payload)
    name = v.name(payload.get("name"), "project name")
    repo = v.repo(payload.get("repo"))
    desc = v.text(payload.get("description"), "description", 1000)
    members = v.str_list(payload.get("members"), "members", max_items=200, max_len=64)
    with db.transaction() as tx:
        if tx.one("SELECT 1 AS x FROM projects WHERE name = ?", (name,)):
            raise Conflict(f"project '{name}' already exists")
        row = tx.one(
            "INSERT INTO projects(name, repo, description, created_by, created_at) VALUES (?, ?, ?, ?, ?) RETURNING id",
            (name, repo, desc, p.agent_id, now_ms()),
        )
        assert row is not None
        pid = int(row["id"])
        ensure_member(tx, pid, p.agent_id, "admin")
        for m in members:
            ensure_member(tx, pid, int(agent_by_name(tx, v.name(m, "member"))["id"]))
        audit(tx, p, "project.create", name, f"repo={repo or ''}")
        emit(tx, "project.created", project_id=pid, actor_id=p.agent_id, data={"name": name, "repo": repo})
    return get_project(db, p, name)


def set_project_member(db: Database, p: Principal, ident: str, payload: dict[str, Any], add: bool) -> dict[str, Any]:
    payload = v.obj(payload)
    pr = project_for(db, p, ident, write=True, admin=True)
    agent = agent_by_name(db, v.name(payload.get("agent"), "agent"))
    role = v.choice(payload.get("role"), "role", PROJECT_ROLES, default="member")
    with db.transaction() as tx:
        if add:
            tx.execute(
                "INSERT INTO project_members(project_id, agent_id, role) VALUES (?, ?, ?) "
                "ON CONFLICT (project_id, agent_id) DO UPDATE SET role = excluded.role",
                (pr["id"], agent["id"], role),
            )
        else:
            from .recovery import orphan_task, terminate_session

            # Removal revokes authority now: the agent's sessions in this project end (their tasks are orphaned, their
            # leases dropped, a coordinator role is lost), and legacy claims without a session are orphaned too.
            for s in tx.query(
                "SELECT * FROM sessions WHERE agent_id = ? AND project_id = ? AND ended_at IS NULL ORDER BY parent_id IS NOT NULL",
                (agent["id"], pr["id"]),
            ):
                terminate_session(tx, s, "removed from project", "session.ended", p.agent_id)
            now = now_ms()
            for t in tx.query(
                "SELECT t.* FROM tasks t JOIN channels c ON c.id = t.channel_id WHERE c.project_id = ? AND t.assignee_id = ? "
                "AND t.status IN ('claimed', 'in_progress', 'blocked', 'review')",
                (pr["id"], agent["id"]),
            ):
                orphan_task(tx, t, "holder removed from project", now)
            tx.execute("DELETE FROM project_members WHERE project_id = ? AND agent_id = ?", (pr["id"], agent["id"]))
            tx.execute(
                "DELETE FROM members WHERE agent_id = ? AND channel_id IN (SELECT id FROM channels WHERE project_id = ?)",
                (agent["id"], pr["id"]),
            )
        audit(tx, p, "project.member.add" if add else "project.member.remove", pr["name"], agent["name"])
    return get_project(db, p, pr["name"])


def list_projects(db: Database, p: Principal) -> list[dict[str, Any]]:
    if p.is_admin or (p.role == "monitor" and not scoped_monitor(db, p)):
        rows = db.query("SELECT id, name, repo, description FROM projects ORDER BY name")
    else:
        rows = db.query(
            "SELECT pr.id, pr.name, pr.repo, pr.description FROM projects pr JOIN project_members pm "
            "ON pm.project_id = pr.id WHERE pm.agent_id = ? ORDER BY pr.name",
            (p.agent_id,),
        )
    return rows


def get_project(db: Database, p: Principal, ident: str | int) -> dict[str, Any]:
    pr = project_for(db, p, ident)
    members = db.query(
        "SELECT a.name, a.owner, a.specialization, pm.role FROM project_members pm JOIN agents a ON a.id = pm.agent_id "
        "WHERE pm.project_id = ? ORDER BY a.name",
        (pr["id"],),
    )
    channels = db.query("SELECT name, repo FROM channels WHERE project_id = ? ORDER BY name", (pr["id"],))
    return {
        "id": pr["id"],
        "name": pr["name"],
        "repo": pr["repo"],
        "description": pr["description"],
        "members": members,
        "channels": channels,
    }
