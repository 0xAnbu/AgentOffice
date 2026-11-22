"""Deterministic multi-agent fixtures for the Agent Office tests (no LLMs, no network, controllable clock)."""

from __future__ import annotations

from typing import Any

from starlette.testclient import TestClient

from agentlink.hub import Hub
from agentlink.recovery import reconcile
from agentlink.security import advance_clock

from .conftest import ADMIN, auth


class Office:
    """Two projects. alpha: 5 owners, several agents each. beta: an isolated team."""

    def __init__(self, client: TestClient, hub: Hub) -> None:
        self.c = client
        self.hub = hub
        self.admin = auth(ADMIN)
        self.tokens: dict[str, str] = {}
        for proj in ("alpha", "beta"):
            r = client.post("/api/projects", json={"name": proj, "repo": f"acme/{proj}"}, headers=self.admin)
            assert r.status_code == 200, r.text
        agents = [
            ("ana-claude", "dev01", "coordination", "alpha", 10),
            ("ana-codex", "dev01", "backend", "alpha", 0),
            ("ben-claude", "dev02", "backend", "alpha", 0),
            ("cris-opencode", "dev03", "frontend", "alpha", 0),
            ("dani-codex", "dev04", "devops", "alpha", 0),
            ("eva-claude", "dev05", "qa", "alpha", 5),
            ("zed", "dev09", "backend", "beta", 0),
        ]
        for name, owner, spec, proj, prio in agents:
            r = client.post(
                "/api/admin/agents",
                json={"name": name, "owner": owner, "specialization": spec, "projects": [proj], "coord_priority": prio},
                headers=self.admin,
            )
            assert r.status_code == 200, r.text
            self.tokens[name] = r.json()["token"]
        for proj, members in (("alpha", [a[0] for a in agents if a[3] == "alpha"]), ("beta", ["zed"])):
            r = client.post(
                "/api/channels", json={"name": f"{proj}-dev", "project": proj, "members": members}, headers=self.admin
            )
            assert r.status_code == 200, r.text

    def h(self, name: str) -> dict[str, str]:
        return auth(self.tokens[name])

    def session(self, name: str, **kw: Any) -> dict[str, Any]:
        body = {"project": kw.pop("project", "beta" if name == "zed" else "alpha"), "runtime": "synthetic", **kw}
        r = self.c.post("/api/sessions", json=body, headers=self.h(name))
        assert r.status_code == 200, r.text
        return r.json()

    def beat(self, name: str, sid: str, **kw: Any) -> Any:
        return self.c.post(f"/api/sessions/{sid}/heartbeat", json=kw, headers=self.h(name))

    def task(self, who: str = "ana-claude", channel: str = "alpha-dev", **kw: Any) -> dict[str, Any]:
        r = self.c.post(
            f"/api/channels/{channel}/tasks", json={"title": kw.pop("title", "work"), **kw}, headers=self.h(who)
        )
        assert r.status_code == 200, r.text
        return r.json()

    def claim(self, who: str, task_id: int, sid: str | None = None, **kw: Any) -> Any:
        body = {"session_id": sid, **kw} if sid else kw
        return self.c.post(f"/api/tasks/{task_id}/claim", json=body, headers=self.h(who))

    def events(self, who: str | None = None, **q: Any) -> list[dict[str, Any]]:
        h = self.h(who) if who else self.admin
        return self.c.get("/api/events", params={"limit": 1000, **q}, headers=h).json()["events"]

    def types(self, **q: Any) -> list[str]:
        return [e["type"] for e in self.events(**q)]

    def reconcile(self) -> dict[str, int]:
        return reconcile(self.hub.db, self.hub.settings)

    def advance(self, seconds: float) -> None:
        advance_clock(seconds)
