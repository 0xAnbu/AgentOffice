"""WebSocket event stream (auth by single-use ticket, replay by cursor, filtering) and the dashboard read model."""

from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect

from agentlink.office import DEFAULT_BUILDING, floor_for

from .office_helpers import Office


def _ticket(office: Office, who: str | None = None) -> str:
    h = office.h(who) if who else office.admin
    return office.c.post("/api/ws-ticket", headers=h).json()["ticket"]


def test_ws_requires_valid_single_use_ticket(office: Office) -> None:
    with pytest.raises(WebSocketDisconnect):
        with office.c.websocket_connect("/ws/events?ticket=nope") as ws:
            ws.receive_json()
    t = _ticket(office)
    with office.c.websocket_connect(f"/ws/events?ticket={t}") as ws:
        assert ws.receive_json()["type"] == "hello"
    with pytest.raises(WebSocketDisconnect):  # reused
        with office.c.websocket_connect(f"/ws/events?ticket={t}") as ws:
            ws.receive_json()


def test_ws_replays_from_cursor_and_streams_live(office: Office) -> None:
    office.c.post("/api/channels/alpha-dev/messages", json={"body": "before"}, headers=office.h("ana-claude"))
    cursor = office.events()[-1]["id"]
    office.c.post(
        "/api/channels/alpha-dev/messages", json={"body": "missed while offline"}, headers=office.h("ana-claude")
    )
    with office.c.websocket_connect(f"/ws/events?ticket={_ticket(office, 'ben-claude')}&after={cursor}") as ws:
        assert ws.receive_json()["type"] == "hello"
        batch = ws.receive_json()
        assert batch["type"] == "events" and [e["data"]["summary"] for e in batch["events"]] == ["missed while offline"]
        office.c.post("/api/channels/alpha-dev/messages", json={"body": "live"}, headers=office.h("ana-claude"))
        live = ws.receive_json()
        assert live["events"][0]["data"]["summary"] == "live" and live["events"][0]["id"] > batch["next_after"]


def test_ws_filters_unauthorized_projects(office: Office) -> None:
    with office.c.websocket_connect(f"/ws/events?ticket={_ticket(office, 'zed')}") as ws:
        ws.receive_json()
        office.c.post("/api/channels/alpha-dev/messages", json={"body": "alpha only"}, headers=office.h("ana-claude"))
        office.c.post("/api/channels/beta-dev/messages", json={"body": "beta"}, headers=office.h("zed"))
        got = []
        while not any(e.get("data", {}).get("summary") == "beta" for e in got):
            got += ws.receive_json().get("events", [])
        assert all(e["project"] == "beta" for e in got)


def test_snapshot_places_sessions_on_specialization_floors(office: Office) -> None:
    ids = {
        n: office.session(n)["id"] for n in ("ana-claude", "ben-claude", "cris-opencode", "dani-codex", "eva-claude")
    }
    sub = office.c.post("/api/sessions", json={"parent_session_id": ids["ana-claude"], "specialization": "frontend",
                        "runtime": "claude-code"}, headers=office.h("ana-claude")).json()  # fmt: skip
    snap = office.c.get("/api/office/snapshot", params={"project": "alpha"}, headers=office.admin).json()
    floors = {s["agent"] + ("/sub" if s["parent_id"] else ""): s["floor"] for s in snap["sessions"]}
    assert floors == {"ana-claude": "coordination", "ben-claude": "backend", "cris-opencode": "frontend",
                      "dani-codex": "devops", "eva-claude": "qa", "ana-claude/sub": "frontend"}  # fmt: skip
    owners = {s["agent"]: s["owner"] for s in snap["sessions"]}
    assert owners["ana-claude"] == "dev01" and owners["eva-claude"] == "dev05"
    assert next(s for s in snap["sessions"] if s["id"] == sub["id"])["parent_id"] == ids["ana-claude"]
    assert [f["key"] for f in snap["building"]["floors"]] == ["coordination", "qa", "devops", "backend", "frontend"]


def test_floor_mapping_fallback() -> None:
    assert floor_for("", DEFAULT_BUILDING["floors"]) == "backend"
    assert floor_for("Security", DEFAULT_BUILDING["floors"]) == "qa"
    assert floor_for("unknown-thing", DEFAULT_BUILDING["floors"]) == "backend"


def test_deep_health(office: Office) -> None:
    r = office.c.get("/api/health?deep=1")
    assert r.status_code == 503 and r.json()["db"] is True and r.json()["reconciler"] is False  # loop disabled in tests
    assert office.c.get("/api/health").status_code == 200


def test_message_search_pagination_and_project_filter(office: Office) -> None:
    ids = [office.c.post("/api/channels/alpha-dev/messages", json={"body": f"item {i} {'needle' if i % 2 else ''}"},
                         headers=office.h("ana-claude")).json()["id"] for i in range(6)]  # fmt: skip
    r = office.c.get("/api/feed", params={"q": "NEEDLE"}, headers=office.admin).json()["messages"]
    assert [m["id"] for m in r] == [ids[1], ids[3], ids[5]]
    page = office.c.get(
        "/api/channels/alpha-dev/messages", params={"before": ids[3], "limit": 2}, headers=office.admin
    ).json()
    assert [m["id"] for m in page["messages"]] == ids[1:3]
    assert office.c.get("/api/feed", params={"project": "beta"}, headers=office.admin).json()["messages"] == []
    assert (
        office.c.get("/api/feed", params={"author": "ana-claude", "limit": 3}, headers=office.admin).json()["messages"][
            -1
        ]["id"]
        == ids[-1]
    )
