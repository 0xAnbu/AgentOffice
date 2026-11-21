"""Regression tests for the external review of 2026-11-21 (findings 1-8, 12-16, 20, 22).

They replace the reviewer's canaries with behavioural checks: each one exercises the failure the review described
(not the shape of the code), so any correct fix passes and a regression fails.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentlink import hooks, supervisor
from agentlink.local import Api, ApiError, Store, check_server_url
from agentlink.recovery import reconcile

from .conftest import World
from .office_helpers import Office
from .test_client import Participant, _deliv, server  # noqa: F401  (fixture)


def _fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "codex"
    fake.write_text("#!/bin/sh\n" + script)
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return fake


# -- 1. an active task can never be left without a holder -------------------------------------------------
def test_active_task_cannot_lose_its_holder(world: World, hub) -> None:
    c, admin = world.c, world.admin
    tid = c.post("/api/channels/dev/tasks", json={"title": "t"}, headers=admin).json()["id"]
    assert c.post(f"/api/tasks/{tid}/claim", json={}, headers=world.h("alice")).status_code == 200
    r = c.patch(f"/api/tasks/{tid}", json={"assignee": None, "status": "in_progress"}, headers=world.h("alice"))
    assert r.status_code == 409 and r.json()["reason"] == "holder_required"
    assert c.get(f"/api/tasks/{tid}", headers=admin).json()["assignee"] == "alice"  # nothing changed
    # unassigning returns it to open, and someone else can take it
    assert c.patch(f"/api/tasks/{tid}", json={"assignee": None}, headers=world.h("alice")).json()["status"] == "open"
    assert c.post(f"/api/tasks/{tid}/claim", json={}, headers=world.h("bob")).status_code == 200
    # an unassigned task cannot be activated without a claim (open -> blocked -> in_progress by the creator)
    t2 = c.post("/api/channels/dev/tasks", json={"title": "t2"}, headers=admin).json()["id"]
    assert c.patch(f"/api/tasks/{t2}", json={"status": "blocked"}, headers=admin).status_code == 200
    r = c.patch(f"/api/tasks/{t2}", json={"status": "in_progress"}, headers=admin)
    assert r.status_code == 409 and r.json()["reason"] == "holder_required"
    # safety net: a row that ended up active without holder (old data, manual SQL) is recovered by the reconciler
    hub.db.execute("UPDATE tasks SET status = 'in_progress', assignee_id = NULL WHERE id = ?", (t2,))
    assert reconcile(hub.db, hub.settings)["tasks_recovered"] == 1
    assert c.get(f"/api/tasks/{t2}", headers=admin).json()["status"] == "open"
    assert c.post(f"/api/tasks/{t2}/claim", json={}, headers=world.h("bob")).status_code == 200


# -- 2. a runtime turn that times out is a failed turn, not a dead supervisor ------------------------------
def test_supervisor_survives_runtime_timeout(
    tmp_path: Path,
    server: str,  # noqa: F811 (fixture)
    office: Office,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_codex(tmp_path, monkeypatch, "sleep 5\n")
    asker = Participant(tmp_path, server, office.tokens["ben-claude"], "alpha", monkeypatch, "dev02-laptop")
    codex = Participant(tmp_path, server, office.tokens["dani-codex"], "alpha", monkeypatch, "dev04-laptop")
    codex.sup.cfg["runtime"] = "codex"
    codex.store.put("adapter.codex", {"headless": True, "workdir": str(tmp_path), "timeout": 1})
    q = asker.api.call("POST", "/api/channels/alpha-dev/messages", {"body": "@dani-codex ping"})
    res = codex.sup.once(wait=0)["headless"]  # must not raise
    assert res["exit_code"] == 124 and "processed" not in res
    assert _deliv(office, q["id"]) == "injected_to_runtime"  # pending, never a false "processed"
    # and the main loop survives any subprocess error raised by an iteration
    calls: list[int] = []

    def once(*_: Any, **__: Any) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired("codex", 1)
        codex.sup.running = False

    monkeypatch.setattr(codex.sup, "once", once)
    monkeypatch.setattr(codex.sup, "reconcile_pending", lambda: 0)
    monkeypatch.setattr(supervisor.time, "sleep", lambda *_: None)
    codex.sup.run_forever()
    assert len(calls) == 2


# -- 3. one bad receipt never blocks the others; processed keeps its evidence ------------------------------
def test_receipt_queue_is_not_poisoned(
    tmp_path: Path,
    server: str,  # noqa: F811 (fixture)
    office: Office,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ben = Participant(tmp_path, server, office.tokens["ben-claude"], "alpha", monkeypatch, "dev02-laptop")
    cris = Participant(tmp_path, server, office.tokens["cris-opencode"], "alpha", monkeypatch, "dev03-laptop")
    m = [
        ben.api.call("POST", "/api/channels/alpha-dev/messages", {"body": f"@cris-opencode q{i}"})["id"]
        for i in range(4)
    ]
    not_mine = ben.api.call("POST", "/api/channels/alpha-dev/messages", {"body": "no mention"})["id"]
    cris.sup.once(wait=0)
    reply = cris.api.call("POST", "/api/channels/alpha-dev/messages", {"body": "a1", "reply_to": m[1]})["id"]
    cris.store.mark([m[0]], "processed")  # e.g. written by an older client, no evidence
    cris.store.mark([m[1]], "processed", evidence={"reply_message_id": reply})
    cris.store.store_messages([{"id": not_mine, "channel": "alpha-dev", "body": "x"}])  # server will say 404
    cris.store.mark([not_mine, m[2]], "injected_to_runtime")
    cris.sup.flush_reports()  # must not raise
    assert _deliv(office, m[0]) == "acknowledged"  # the most the server accepts without evidence
    assert _deliv(office, m[1]) == "processed"
    assert _deliv(office, m[2]) == "injected_to_runtime"  # went through despite the bad neighbour
    assert cris.store.unreported() == []  # nothing left to retry forever
    assert cris.sup.once(wait=0) is not None  # the loop keeps working


# -- 4. the token never follows a redirect, and never travels over plain http to a remote host --------------
def test_token_not_forwarded_on_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    class Dest(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers.get("Authorization") or "")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a: Any) -> None:
            pass

    dest = ThreadingHTTPServer(("127.0.0.1", 0), Dest)

    class Redir(Dest):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{dest.server_port}/x")
            self.end_headers()

    redir = ThreadingHTTPServer(("127.0.0.1", 0), Redir)
    for s in (dest, redir):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        with pytest.raises(ApiError) as e:
            Api(f"http://127.0.0.1:{redir.server_port}", "al_secret").call("GET", "/x")
        assert e.value.status == 302
        from agentlink.cli import Client

        with pytest.raises(SystemExit):
            Client(f"http://127.0.0.1:{redir.server_port}", "al_secret").request("GET", "/x")
    finally:
        redir.shutdown()
        dest.shutdown()
    assert seen == []  # the destination never received anything
    for ok in (
        "https://office.example.ts.net",
        "http://127.0.0.1:8080",
        "http://localhost:1",
        "http://100.101.102.103",
    ):
        check_server_url(ok)
    with pytest.raises(ValueError):
        check_server_url("http://office.example.com")
    monkeypatch.setenv("AGENTLINK_ALLOW_HTTP", "1")
    check_server_url("http://office.example.com")


# -- 5. bodies are limited while streaming, before authentication / HMAC ---------------------------------
def test_body_limit_enforced_while_streaming(world: World) -> None:
    big = (b"x" * 65536 for _ in range(10))  # 640 KiB, chunked: no Content-Length to trust
    r = world.c.post(
        "/api/channels/dev/messages", content=big, headers={**world.h("alice"), "content-type": "application/json"}
    )
    assert r.status_code == 413
    huge = (b"x" * 1_048_576 for _ in range(6))  # 6 MiB to the anonymous webhook: rejected before the HMAC check
    r = world.c.post(
        "/api/github/webhook", content=huge, headers={"x-github-event": "push", "content-type": "application/json"}
    )
    assert r.status_code == 413
    declared = world.c.post(
        "/api/channels/dev/messages", content=b"{}", headers={**world.h("alice"), "content-length": str(10**9)}
    )
    assert declared.status_code == 413


# -- 6. the reconciler survives a failed first start -------------------------------------------------------
def test_reconcile_loop_retries_failed_start(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentlink import app, hub, recovery

    attempts: list[int] = []
    fake = SimpleNamespace(
        settings=SimpleNamespace(reconcile_interval=0.05), db=object(), notifier=SimpleNamespace(bump=lambda: None)
    )

    def get_hub() -> Any:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("database is locked")
        return fake

    passes: list[int] = []
    monkeypatch.setattr(hub, "get_hub", get_hub)
    monkeypatch.setattr(recovery, "reconcile", lambda db, settings: passes.append(1) or {})

    async def run() -> None:
        task = asyncio.create_task(app._reconcile_loop())
        await asyncio.sleep(1.5)
        task.cancel()

    asyncio.run(run())
    assert len(attempts) == 2 and len(passes) >= 2


# -- 7. binding a channel to a repository is a project-level decision -----------------------------------------
def test_channel_repo_binding_is_project_level(office: Office) -> None:
    c = office.c
    member = office.h("ben-claude")  # alpha member; alpha's repo is acme/alpha
    assert (
        c.post(
            "/api/channels", json={"name": "evil", "project": "alpha", "repo": "acme/beta"}, headers=member
        ).status_code
        == 403
    )
    assert (
        c.post(
            "/api/channels", json={"name": "own", "project": "alpha", "repo": "acme/alpha"}, headers=member
        ).status_code
        == 200
    )
    assert c.post("/api/channels", json={"name": "plain", "project": "alpha"}, headers=member).status_code == 200
    assert (
        c.post(
            "/api/channels", json={"name": "ops", "project": "alpha", "repo": "acme/beta"}, headers=office.admin
        ).status_code
        == 200
    )
    # a beta push never reaches the member's channels
    body = json.dumps({"repository": {"full_name": "acme/beta"}, "ref": "refs/heads/main", "commits": []}).encode()
    sig = "sha256=" + hmac.new(b"whsec-test", body, hashlib.sha256).hexdigest()
    c.post("/api/github/webhook", content=body, headers={"x-github-event": "push", "x-github-delivery": "d1",
           "x-hub-signature-256": sig, "content-type": "application/json"})  # fmt: skip
    for ch in ("own", "plain"):
        assert c.get(f"/api/channels/{ch}/messages", headers=member).json()["messages"] == []


# -- 8. removing a member revokes its authority immediately --------------------------------------------------
def test_removed_member_loses_sessions_tasks_and_coordinator(office: Office) -> None:
    c = office.c
    ana = office.session("ana-claude")["id"]
    ben = office.session("ben-claude")["id"]
    e = c.post("/api/projects/alpha/coordinator/designate", json={"session_id": ana}, headers=office.admin).json()[
        "epoch"
    ]
    assert c.post("/api/projects/alpha/coordinator/accept", json={"session_id": ana, "epoch": e},
                  headers=office.h("ana-claude")).json()["status"] == "active"  # fmt: skip
    tid = office.task()["id"]
    assert office.claim("ben-claude", tid, ben).status_code == 200
    legacy = office.task()["id"]
    assert office.claim("ben-claude", legacy).status_code == 200  # legacy claim without a session
    for who in ("ana-claude", "ben-claude"):
        assert c.delete(f"/api/projects/alpha/members/{who}", headers=office.admin).status_code == 200
    st = c.get("/api/projects/alpha/coordinator", headers=office.admin).json()
    assert not st["valid"] and st["session_id"] is None
    tasks = {t: c.get(f"/api/tasks/{t}", headers=office.admin).json() for t in (tid, legacy)}
    assert all(t["status"] == "orphaned" and t["assignee"] is None for t in tasks.values())
    r = office.beat("ana-claude", ana, state="executing")
    assert r.status_code == 409 and r.json()["reason"] == "session_ended"
    assert "coordinator.lost" in office.types()


def test_heartbeat_of_non_member_ends_the_session(office: Office, hub) -> None:
    ben = office.session("ben-claude")["id"]
    hub.db.execute(  # membership removed behind the API's back (e.g. restored data)
        "DELETE FROM project_members WHERE agent_id = (SELECT id FROM agents WHERE name = 'ben-claude')"
    )
    r = office.beat("ben-claude", ben, state="executing")
    assert r.status_code == 409 and r.json()["reason"] == "session_ended"
    assert office.beat("ben-claude", ben).status_code == 409


# -- 12-14. admin census, receipts and directory data respect roles and project scope ---------------------------
def test_admin_census_requires_admin(world: World) -> None:
    for who in ("alice", "watch"):
        assert world.c.get("/api/admin/agents", headers=world.h(who)).status_code == 403
    assert world.c.get("/api/admin/agents", headers=world.admin).status_code == 200


def test_scoped_monitor_cannot_read_other_projects(office: Office) -> None:
    c = office.c
    mons = {p: c.post("/api/admin/agents", json={"name": f"mon-{p}", "role": "monitor", "projects": [p]},
                      headers=office.admin).json()["token"] for p in ("alpha", "beta")}  # fmt: skip
    mid = c.post(
        "/api/channels/alpha-dev/messages", json={"body": "@ben-claude hi"}, headers=office.h("ana-claude")
    ).json()["id"]
    assert (
        c.get(f"/api/messages/{mid}/deliveries", headers={"Authorization": f"Bearer {mons['alpha']}"}).status_code
        == 200
    )
    assert c.get(
        f"/api/messages/{mid}/deliveries", headers={"Authorization": f"Bearer {mons['beta']}"}
    ).status_code in (403, 404)
    for who in ("ana-claude", "zed"):
        c.post("/api/devices/heartbeat", json={"device": f"{who}-box"}, headers=office.h(who))
        c.post("/api/presence", json={"status": "online"}, headers=office.h(who))
    zed = office.h("zed")  # beta only
    owners = {d["owner"] for d in c.get("/api/office/snapshot", headers=zed).json()["devices"]}
    assert owners == {"dev09"}
    assert {p["agent"] for p in c.get("/api/presence", headers=zed).json()} == {"zed"}
    beta_mon = {"Authorization": f"Bearer {mons['beta']}"}
    assert {p["agent"] for p in c.get("/api/presence", headers=beta_mon).json()} == {"zed"}
    assert {p["agent"] for p in c.get("/api/presence", headers=office.admin).json()} >= {"zed", "ana-claude"}


# -- 15. anonymous deep health cannot turn into a write flood ----------------------------------------------------
def test_deep_health_write_probe_is_rate_limited(world: World, hub) -> None:
    first = world.c.get("/api/health?deep=1").json()
    probe = hub.db.one("SELECT value FROM meta WHERE key = 'health_probe'")
    for _ in range(20):
        again = world.c.get("/api/health?deep=1").json()
    assert hub.db.one("SELECT value FROM meta WHERE key = 'health_probe'") == probe  # one write, not 21
    assert first["db"] is True and again["db"] is True and again.get("cached") is True


# -- 16. local state (tokens, message bodies) is private to the user -----------------------------------------------
def test_local_state_is_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o755)
    home.chmod(0o755)
    monkeypatch.setenv("AGENTLINK_HOME", str(home))
    store = Store()
    store.store_messages([{"id": 1, "body": "secret"}])
    assert home.stat().st_mode & 0o777 == 0o700
    assert Store().path.stat().st_mode & 0o777 == 0o600


# -- 20. only what the model was shown counts as injected / processed -----------------------------------------------
def test_only_rendered_messages_are_injected_or_processed(
    tmp_path: Path,
    server: str,  # noqa: F811 (fixture)
    office: Office,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ben = Participant(tmp_path, server, office.tokens["ben-claude"], "alpha", monkeypatch, "dev02-laptop")
    cris = Participant(tmp_path, server, office.tokens["cris-opencode"], "alpha", monkeypatch, "dev03-laptop")
    n = hooks.MAX_INJECT + 3
    ids = [
        ben.api.call("POST", "/api/channels/alpha-dev/messages", {"body": f"@cris-opencode m{i}"})["id"]
        for i in range(n)
    ]
    cris.sup.once(wait=0)
    cris.hook("SessionStart", local_id="rt-c")
    states = [_deliv(office, i) for i in ids]
    assert states.count("injected_to_runtime") == hooks.MAX_INJECT and states.count("delivered_to_supervisor") == 3
    cris.hook("UserPromptSubmit", local_id="rt-c", prompt="go on")  # the rest at the next safe point
    assert all(_deliv(office, i) == "injected_to_runtime" for i in ids)
    # headless: one turn answers at most MAX_INJECT messages; the rest stay pending for the next turn
    _fake_codex(
        tmp_path, monkeypatch, 'while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done\nprintf ok > "$out"\n'
    )
    dani = Participant(tmp_path, server, office.tokens["dani-codex"], "alpha", monkeypatch, "dev04-laptop")
    more = [
        ben.api.call("POST", "/api/channels/alpha-dev/messages", {"body": f"@dani-codex q{i}"})["id"] for i in range(n)
    ]
    dani.sup.cfg["runtime"] = "codex"
    dani.store.put("adapter.codex", {"headless": True, "workdir": str(tmp_path)})
    res = dani.sup.once(wait=0)["headless"]
    assert len(res["processed"]) == hooks.MAX_INJECT
    assert [_deliv(office, i) for i in more].count("processed") == hooks.MAX_INJECT


# -- 22. MCP: positional params are a JSON-RPC error, not a 500 ----------------------------------------------------
def test_mcp_array_params_is_invalid_params(world: World) -> None:
    r = world.c.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": [1]}, headers=world.admin
    )
    assert r.status_code == 200 and r.json()["error"]["code"] == -32602
