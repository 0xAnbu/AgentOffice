"""/tareas: the task manager for project administrators (read model + the existing write paths it relies on)."""

from __future__ import annotations

from typing import Any

from .office_helpers import Office


def board(o: Office, who: str | None = None, project: str = "alpha") -> Any:
    h = o.h(who) if who else o.admin
    return o.c.get("/api/board", params={"project": project}, headers=h)


def make_project_admin(o: Office, name: str, project: str = "alpha") -> None:
    r = o.c.post(f"/api/projects/{project}/members", json={"agent": name, "role": "admin"}, headers=o.admin)
    assert r.status_code == 200, r.text


def inbox_for(o: Office, who: str) -> list[dict[str, Any]]:
    r = o.c.get("/api/inbox", headers=o.h(who))
    assert r.status_code == 200, r.text
    body = r.json()
    return body["messages"] if isinstance(body, dict) else body


def test_page_is_served(office: Office) -> None:
    r = office.c.get("/tareas")
    assert r.status_code == 200
    assert "/static/tareas/app.js" in r.text
    assert "<script>" not in r.text  # CSP: no inline scripts


def test_access_admins_only(office: Office) -> None:
    assert office.c.get("/api/board", params={"project": "alpha"}).status_code == 401
    assert board(office, "ben-claude").status_code == 403  # plain member
    assert office.c.get("/api/board/projects", headers=office.h("ben-claude")).json() == []
    assert board(office).status_code == 200  # global admin
    make_project_admin(office, "ben-claude")
    assert board(office, "ben-claude").status_code == 200
    assert [p["name"] for p in office.c.get("/api/board/projects", headers=office.h("ben-claude")).json()] == ["alpha"]
    assert board(office, "ben-claude", "beta").status_code == 403  # admin of alpha only


def test_monitor_is_refused(office: Office) -> None:
    r = office.c.post("/api/admin/agents", json={"name": "watch", "role": "monitor"}, headers=office.admin)
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    assert office.c.get("/api/board", params={"project": "alpha"}, headers=h).status_code == 403
    assert office.c.get("/api/board/projects", headers=h).json() == []


def test_board_shows_current_tasks_and_items(office: Office) -> None:
    t = office.task(title="current")
    done = office.task(title="finished")
    office.c.patch(f"/api/tasks/{done['id']}", json={"status": "cancelled"}, headers=office.admin)
    q = office.c.post("/api/channels/alpha-dev/messages", json={"kind": "question", "body": "which db?",
                      "mentions": ["ana-codex"]}, headers=office.h("ben-claude")).json()  # fmt: skip
    office.c.post("/api/channels/alpha-dev/messages", json={"kind": "handoff", "body": "take over the api"},
                  headers=office.h("ana-codex"))  # fmt: skip
    b = board(office).json()
    assert [x["id"] for x in b["tasks"]] == [t["id"]]
    types = {x["type"] for x in b["items"]}
    assert {"question", "handoff"} <= types
    item = next(x for x in b["items"] if x["id"] == q["id"])
    assert item["status"] == "pending" and item["to"][0]["agent"] == "ana-codex"
    assert {c["name"] for c in b["channels"]} == {"alpha-dev"}
    assert "ben-claude" in {a["name"] for a in b["agents"]}
    assert "zed" not in {a["name"] for a in b["agents"]}  # other project


def test_items_disappear_when_resolved(office: Office) -> None:
    q = office.c.post("/api/channels/alpha-dev/messages", json={"kind": "question", "body": "?"},
                      headers=office.h("ben-claude")).json()  # fmt: skip
    h = office.c.post("/api/channels/alpha-dev/messages", json={"kind": "handoff", "body": "yours"},
                      headers=office.h("ben-claude")).json()  # fmt: skip
    office.c.post("/api/channels/alpha-dev/messages", json={"kind": "answer", "body": "sqlite", "reply_to": q["id"]},
                  headers=office.h("ana-codex"))  # fmt: skip
    office.c.post("/api/channels/alpha-dev/messages", json={"body": "done", "reply_to": h["id"], "data": {"resolves": h["id"]}},
                  headers=office.admin)  # fmt: skip
    ids = {x["id"] for x in board(office).json()["items"]}
    assert q["id"] not in ids and h["id"] not in ids


def test_assign_notifies_assignee_with_task_priority(office: Office) -> None:
    t = office.task(title="urgent fix", priority=1)
    r = office.c.patch(f"/api/tasks/{t['id']}", json={"if_version": t["version"], "assignee": "ben-claude"},
                       headers=office.admin)  # fmt: skip
    assert r.status_code == 200, r.text
    msgs = [m for m in inbox_for(office, "ben-claude") if f"#{t['id']}" in m["body"]]
    assert msgs, "assignee got no notice"
    assert msgs[-1]["priority"] == 1
    assert msgs[-1]["channel"] == "alpha-dev"
    assert "urgent fix" in msgs[-1]["body"]


def test_version_conflict(office: Office) -> None:
    t = office.task()
    ok = office.c.patch(f"/api/tasks/{t['id']}", json={"if_version": t["version"], "title": "a"}, headers=office.admin)
    assert ok.status_code == 200, ok.text
    stale = office.c.patch(
        f"/api/tasks/{t['id']}", json={"if_version": t["version"], "title": "b"}, headers=office.admin
    )
    assert stale.status_code == 409
    assert stale.json().get("current", {}).get("title") == "a"


def test_project_admin_can_edit_other_agents_tasks(office: Office) -> None:
    t = office.task(who="ana-codex", title="theirs")
    denied = office.c.patch(f"/api/tasks/{t['id']}", json={"priority": 1}, headers=office.h("ben-claude"))
    assert denied.status_code == 403
    make_project_admin(office, "ben-claude")
    r = office.c.patch(
        f"/api/tasks/{t['id']}", json={"priority": 1, "assignee": "eva-claude"}, headers=office.h("ben-claude")
    )
    assert r.status_code == 200, r.text
    assert r.json()["assignee"] == "eva-claude" and r.json()["priority"] == 1


def test_designate_from_board_is_an_urgent_offer(office: Office) -> None:
    s = office.session("ben-claude")
    r = office.c.post("/api/projects/alpha/coordinator/designate", json={"session_id": s["id"]}, headers=office.admin)
    assert r.status_code == 200, r.text
    b = board(office).json()
    assert b["coordinator"]["status"] == "offered" and b["coordinator"]["agent"] == "ben-claude"
    assert s["id"] in {x["id"] for x in b["sessions"]}
    offers = [m for m in inbox_for(office, "ben-claude") if m.get("data", {}).get("event") == "coordinator.offered"]
    assert offers and offers[-1]["priority"] == 1
    assert all(x["body"] != offers[-1]["body"] for x in b["items"])  # shown as coordinator state, not as an item
