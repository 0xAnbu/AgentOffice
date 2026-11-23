"""The /admin page and the project deletion it relies on."""

from __future__ import annotations

from .conftest import World


def _project_with_work(world: World) -> None:
    c, admin = world.c, world.admin
    assert c.post("/api/projects", json={"name": "honey", "members": ["alice"]}, headers=admin).status_code == 200
    r = c.post("/api/channels", json={"name": "honey-dev", "project": "honey", "members": ["alice"]}, headers=admin)
    assert r.status_code == 200, r.text
    assert world.post("alice", "hola @alice", channel="honey-dev").status_code == 200
    t = c.post("/api/channels/honey-dev/tasks", json={"title": "t"}, headers=world.h("alice")).json()
    s = c.post("/api/sessions", json={"project": "honey", "runtime": "opencode"}, headers=world.h("alice")).json()
    r = c.post(f"/api/tasks/{t['id']}/claim", json={"session_id": s["id"]}, headers=world.h("alice"))
    assert r.status_code == 200, r.text


def test_admin_deletes_a_project_with_everything_in_it(world: World) -> None:
    _project_with_work(world)
    r = world.c.delete("/api/admin/projects/honey", headers=world.admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] == "honey" and body["channels"] == 1 and body["messages"] >= 1
    assert world.c.get("/api/projects/honey", headers=world.admin).status_code == 404
    assert world.c.get("/api/channels/honey-dev", headers=world.admin).status_code == 404
    assert all(s["project"] != "honey" for s in world.c.get("/api/sessions", headers=world.admin).json())
    # the agent keeps working elsewhere, and the name can be reused
    assert world.post("alice", "still here").status_code == 200
    assert world.c.post("/api/projects", json={"name": "honey"}, headers=world.admin).status_code == 200
    log = world.c.get("/api/admin/audit", headers=world.admin).json()
    assert any(e["action"] == "project.delete" and e["target"] == "honey" for e in log)


def test_only_a_global_admin_deletes_projects(world: World) -> None:
    _project_with_work(world)
    for who in ("alice", "watch"):
        assert world.c.delete("/api/admin/projects/honey", headers=world.h(who)).status_code == 403
    assert world.c.delete("/api/admin/projects/nope", headers=world.admin).status_code == 404


def test_the_last_project_is_not_deleted(world: World) -> None:
    names = [p["name"] for p in world.c.get("/api/projects", headers=world.admin).json()]
    assert names == ["default"]
    assert world.c.delete("/api/admin/projects/default", headers=world.admin).status_code == 400


def test_admin_page_is_served_without_inline_code(world: World) -> None:
    r = world.c.get("/admin")
    assert r.status_code == 200 and "/static/admin/app.js" in r.text
    assert "<script>" not in r.text and " style=" not in r.text  # CSP: script-src/style-src 'self' only
    assert "msg-form" not in r.text  # deleting a message by ID stays in the chat and ./admin, not on this page
    for asset in ("app.js", "style.css"):
        assert world.c.get(f"/static/admin/{asset}").status_code == 200
