"""Failure, recovery and concurrency scenarios (INSTRUCTIONS §15 tests 1-10, 15-18; invariants I1-I10).

All deterministic: the server clock is advanced instead of sleeping, the reconciler is invoked explicitly.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from agentlink.hub import build_hub, set_hub

from .conftest import auth
from .office_helpers import Office


# -- Test 1 / I1: concurrent claim ------------------------------------------------------------------
def test_eight_sessions_claim_concurrently_exactly_one_wins(office: Office) -> None:
    t = office.task()
    names = list(office.tokens)[:6]  # alpha agents
    sessions = [(n, office.session(n)["id"]) for n in names] + [(n, office.session(n)["id"]) for n in names[:2]]
    assert len(sessions) == 8

    def go(pair: tuple[str, str]) -> int:
        return office.claim(pair[0], t["id"], pair[1]).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(go, sessions))
    assert codes.count(200) == 1 and codes.count(409) == 7
    final = office.c.get(f"/api/tasks/{t['id']}", headers=office.admin).json()
    assert final["status"] == "claimed" and final["lease_epoch"] == 1 and final["lease_valid"]
    assert office.types(types="task.claimed").count("task.claimed") == 1


# -- Test 2 / I4: holder dies -------------------------------------------------------------------------
def test_dead_holder_task_is_orphaned_recorded_and_reclaimable(office: Office) -> None:
    t = office.task()
    a = office.session("ben-claude")
    assert office.claim("ben-claude", t["id"], a["id"], ttl_seconds=60).status_code == 200
    office.advance(office.hub.settings.session_ttl + 1)  # process vanished: no more heartbeats
    stats = office.reconcile()
    assert stats["sessions_expired"] == 1 and stats["tasks_orphaned"] == 1
    task = office.c.get(f"/api/tasks/{t['id']}", headers=office.admin).json()
    assert task["status"] == "orphaned" and task["assignee"] is None and not task["lease_valid"]
    orphan = next(e for e in office.events(types="task.orphaned") if e["task_id"] == t["id"])
    assert orphan["data"]["previous_holder"] == "ben-claude" and orphan["data"]["epoch"] == 1
    assert "session.expired" in office.types()
    b = office.session("ana-codex")
    r = office.claim("ana-codex", t["id"], b["id"])
    assert r.status_code == 200 and r.json()["lease_epoch"] == 2
    claimed = [e for e in office.events(types="task.claimed") if e["task_id"] == t["id"]]
    assert claimed[-1]["data"]["recovered"] is True


def test_lease_expiry_without_renewal_orphans_then_recovers_to_open(office: Office) -> None:
    t = office.task()
    s = office.session("ben-claude")
    office.claim("ben-claude", t["id"], s["id"], ttl_seconds=30)
    office.advance(31)
    office.beat("ben-claude", s["id"], state="executing")  # alive, but never renewed the task lease
    assert office.reconcile()["tasks_orphaned"] == 1
    office.advance(office.hub.settings.orphan_hold + 1)
    office.beat("ben-claude", s["id"])
    assert office.reconcile()["tasks_recovered"] == 1
    assert office.c.get(f"/api/tasks/{t['id']}", headers=office.admin).json()["status"] == "open"
    assert office.reconcile()["tasks_recovered"] == 0  # idempotent


# -- Test 3 / Test 16 / I2: stale owner ------------------------------------------------------------
def test_old_holder_cannot_complete_with_stale_epoch(office: Office) -> None:
    t = office.task()
    a = office.session("ben-claude")
    old = office.claim("ben-claude", t["id"], a["id"], ttl_seconds=30).json()
    office.c.post(f"/api/tasks/{t['id']}/checkpoints", json={"session_id": a["id"], "lease_epoch": old["lease_epoch"],
                  "branch": "feat/x", "commit": "abc123", "pending": ["tests"]}, headers=office.h("ben-claude"))  # fmt: skip
    office.advance(31)
    office.reconcile()
    b = office.session("ana-codex")
    new = office.claim("ana-codex", t["id"], b["id"]).json()
    assert new["lease_epoch"] == old["lease_epoch"] + 1
    r = office.c.patch(
        f"/api/tasks/{t['id']}",
        json={"status": "done", "session_id": a["id"], "lease_epoch": old["lease_epoch"]},
        headers=office.h("ben-claude"),
    )
    assert r.status_code == 409 and r.json()["reason"] == "stale_lease"
    cur = office.c.get(f"/api/tasks/{t['id']}", headers=office.admin).json()
    assert cur["status"] == "claimed" and cur["assignee"] == "ana-codex" and cur["lease_epoch"] == new["lease_epoch"]
    assert "task.stale_write" in office.types()  # conflict observable
    # the old holder's pre-publish check says it lost authority; its checkpoint is preserved for the new holder
    chk = office.c.get(f"/api/tasks/{t['id']}/lease", params={"session_id": a["id"], "lease_epoch": old["lease_epoch"]},
                       headers=office.h("ben-claude")).json()  # fmt: skip
    assert chk["holds_lease"] is False and chk["current_epoch"] == new["lease_epoch"]
    assert cur["checkpoint"]["branch"] == "feat/x" and cur["checkpoint"]["commit"] == "abc123"
    # renew and checkpoint with the stale epoch are rejected too
    assert office.c.post(f"/api/tasks/{t['id']}/renew", json={"session_id": a["id"], "lease_epoch": old["lease_epoch"],
                         "progress": True}, headers=office.h("ben-claude")).status_code == 409  # fmt: skip
    # the new holder completes normally
    ok = office.c.patch(f"/api/tasks/{t['id']}", json={"status": "done", "session_id": b["id"], "lease_epoch": new["lease_epoch"]},
                        headers=office.h("ana-codex"))  # fmt: skip
    assert ok.status_code == 200 and ok.json()["status"] == "done"


def test_session_bound_lease_requires_epoch(office: Office) -> None:
    t = office.task()
    s = office.session("ben-claude")
    office.claim("ben-claude", t["id"], s["id"])
    r = office.c.patch(f"/api/tasks/{t['id']}", json={"status": "done"}, headers=office.h("ben-claude"))
    assert r.status_code == 409 and r.json()["reason"] == "lease_required"


# -- Test 4 / Test 5 / I3: coordinator succession and split brain ------------------------------------
def _alpha_sessions(office: Office) -> dict[str, str]:
    return {n: office.session(n)["id"] for n in ("ana-claude", "eva-claude", "ben-claude")}


def test_coordinator_death_triggers_single_successor_and_epoch_bump(office: Office) -> None:
    s = _alpha_sessions(office)
    d = office.c.post(
        "/api/projects/alpha/coordinator/designate", json={"session_id": s["ana-claude"]}, headers=office.admin
    )
    assert d.status_code == 200 and d.json()["status"] == "offered"
    e1 = d.json()["epoch"]
    acc = office.c.post("/api/projects/alpha/coordinator/accept", json={"session_id": s["ana-claude"], "epoch": e1},
                        headers=office.h("ana-claude"))  # fmt: skip
    assert acc.status_code == 200 and acc.json()["status"] == "active"
    # ana's coordinator lease runs out (stops heartbeating); the others stay alive
    office.advance(office.hub.settings.coordinator_lease + 1)
    for n in ("eva-claude", "ben-claude"):
        office.beat(n, s[n], state="idle")
    stats = office.reconcile()
    assert stats["coordinators_lost"] == 1 and stats["offers_made"] == 1
    st = office.c.get("/api/projects/alpha/coordinator", headers=office.admin).json()
    assert st["status"] == "offered" and st["offered_session"] == s["eva-claude"]  # higher priority than ben
    assert st["epoch"] == e1 + 1
    offer = [e for e in office.events(types="coordinator.offered")][-1]
    assert "COORDINATOR SUCCESSION" in offer["data"]["prompt"]
    # the succession prompt reached eva's inbox as an urgent mention
    inbox = office.c.get("/api/inbox", headers=office.h("eva-claude")).json()["messages"]
    assert any("COORDINATOR SUCCESSION" in m["body"] for m in inbox)

    # ben cannot accept eva's offer; eva can, exactly once
    def accept(name: str) -> int:
        return office.c.post("/api/projects/alpha/coordinator/accept", json={"session_id": s[name], "epoch": st["epoch"]},
                             headers=office.h(name)).status_code  # fmt: skip

    with ThreadPoolExecutor(max_workers=3) as pool:
        codes = list(pool.map(accept, ["eva-claude", "ben-claude", "eva-claude"]))
    assert sorted(codes) == [200, 409, 409]
    final = office.c.get("/api/projects/alpha/coordinator", headers=office.admin).json()
    assert final["status"] == "active" and final["session_id"] == s["eva-claude"] and final["valid"]
    types = office.types()
    assert types.count("coordinator.accepted") == 2 and "coordinator.lost" in types


def test_old_coordinator_returns_and_is_rejected(office: Office) -> None:
    s = _alpha_sessions(office)
    e1 = office.c.post("/api/projects/alpha/coordinator/designate", json={"session_id": s["ana-claude"]},
                       headers=office.admin).json()["epoch"]  # fmt: skip
    office.c.post("/api/projects/alpha/coordinator/accept", json={"session_id": s["ana-claude"], "epoch": e1},
                  headers=office.h("ana-claude"))  # fmt: skip
    office.advance(office.hub.settings.coordinator_lease + 1)
    for n in ("eva-claude", "ben-claude"):
        office.beat(n, s[n])
    office.reconcile()
    e2 = office.c.get("/api/projects/alpha/coordinator", headers=office.admin).json()["epoch"]
    office.c.post("/api/projects/alpha/coordinator/accept", json={"session_id": s["eva-claude"], "epoch": e2},
                  headers=office.h("eva-claude"))  # fmt: skip
    # ana's partition heals: her old session is gone and a new one does NOT inherit authority
    old = office.beat("ana-claude", s["ana-claude"], state="executing")
    assert old.status_code == 409 and old.json()["reason"] == "session_ended"
    s2 = office.session("ana-claude")["id"]
    hb = office.beat("ana-claude", s2, state="executing").json()
    assert not (hb.get("coordinator") or {}).get("you")
    t = office.task(who="ben-claude")
    r = office.c.post("/api/projects/alpha/coordinator/assign",
                      json={"session_id": s2, "epoch": e1, "task_id": t["id"], "assignee": "ben-claude"},
                      headers=office.h("ana-claude"))  # fmt: skip
    assert r.status_code == 409 and r.json()["reason"] == "stale_epoch"
    st = office.c.get("/api/projects/alpha/coordinator", headers=office.admin).json()
    assert st["session_id"] == s["eva-claude"] and st["epoch"] == e2
    ok = office.c.post("/api/projects/alpha/coordinator/assign",
                       json={"session_id": s["eva-claude"], "epoch": e2, "task_id": t["id"], "assignee": "ben-claude"},
                       headers=office.h("eva-claude"))  # fmt: skip
    assert ok.status_code == 200 and ok.json()["assignee"] == "ben-claude"


def test_decline_with_proposal_and_offer_timeout(office: Office) -> None:
    s = _alpha_sessions(office)
    office.reconcile()  # no coordinator yet: automatic election offers to the best candidate (ana, prio 10)
    st = office.c.get("/api/projects/alpha/coordinator", headers=office.admin).json()
    assert st["offered_session"] == s["ana-claude"]
    r = office.c.post("/api/projects/alpha/coordinator/decline",
                      json={"session_id": s["ana-claude"], "epoch": st["epoch"], "reason": "busy", "propose": s["ben-claude"]},
                      headers=office.h("ana-claude"))  # fmt: skip
    assert r.status_code == 200 and r.json()["offered_session"] == s["ben-claude"]  # proposal honoured
    office.advance(office.hub.settings.offer_timeout + 1)
    for n in s:
        office.beat(n, s[n])
    stats = office.reconcile()
    assert stats["offers_expired"] == 1 and stats["offers_made"] == 1  # ben never answered: next candidate (eva)
    assert (
        office.c.get("/api/projects/alpha/coordinator", headers=office.admin).json()["offered_session"]
        == s["eva-claude"]
    )


# -- Test 6: everybody disconnected -------------------------------------------------------------------
def test_no_candidates_preserves_state_and_alerts_humans(office: Office) -> None:
    s = _alpha_sessions(office)
    t = office.task()
    office.claim("ben-claude", t["id"], s["ben-claude"])
    office.advance(office.hub.settings.session_ttl + 1)
    stats = office.reconcile()
    assert stats["sessions_expired"] == 3
    st = office.c.get("/api/projects/alpha/coordinator", headers=office.admin).json()
    assert st["status"] == "none" and not st["valid"]
    assert "coordinator.unavailable" in office.types()
    msgs = office.c.get("/api/channels/alpha-dev/messages", headers=office.admin).json()["messages"]
    assert any("Human attention needed" in m["body"] for m in msgs)
    assert office.c.get(f"/api/tasks/{t['id']}", headers=office.admin).json()["status"] == "orphaned"  # recoverable
    # work that does not depend on a coordinator keeps going
    assert office.task(who="cris-opencode", title="still possible")["status"] == "open"
    assert office.reconcile()["offers_made"] == 0 and office.types().count("coordinator.unavailable") == 1  # no spam


# -- Test 7 / I7: dependency cycles ----------------------------------------------------------------------
def test_dependency_cycle_rejected_graph_unchanged(office: Office) -> None:
    a = office.task(title="A")
    b = office.task(title="B", depends_on=[a["id"]])
    c = office.task(title="C", depends_on=[b["id"]])
    r = office.c.patch(f"/api/tasks/{a['id']}", json={"depends_on": [c["id"]]}, headers=office.h("ana-claude"))
    assert r.status_code == 409 and r.json()["reason"] == "dependency_cycle"
    assert office.c.get(f"/api/tasks/{a['id']}", headers=office.admin).json()["depends_on"] == []
    r = office.c.patch(f"/api/tasks/{a['id']}", json={"depends_on": [a["id"]]}, headers=office.h("ana-claude"))
    assert r.status_code == 409
    # unmet dependencies block claims explicitly, a failed dependency blocks the dependent, recovery unblocks it
    s = office.session("ben-claude")
    r = office.claim("ben-claude", c["id"], s["id"])
    assert r.status_code == 409 and r.json()["reason"] == "dependencies_unmet"
    office.c.patch(f"/api/tasks/{b['id']}", json={"status": "cancelled"}, headers=office.h("ana-claude"))
    assert office.reconcile()["tasks_blocked"] == 1
    assert (
        office.c.get(f"/api/tasks/{c['id']}", headers=office.admin).json()["block_reason"]
        == f"dependency_failed:#{b['id']}"
    )
    office.c.patch(f"/api/tasks/{b['id']}", json={"status": "open"}, headers=office.h("ana-claude"))
    assert office.reconcile()["tasks_unblocked"] == 1


def test_long_block_raises_wait_timeout_once(office: Office) -> None:
    t = office.task()
    office.c.patch(
        f"/api/tasks/{t['id']}",
        json={"status": "blocked", "note": "waiting on API contract"},
        headers=office.h("ana-claude"),
    )
    office.advance(office.hub.settings.wait_timeout + 1)
    assert office.reconcile()["wait_timeouts"] == 1
    assert office.reconcile()["wait_timeouts"] == 0


# -- Test 8 / I10: token exhaustion / stalled runtime --------------------------------------------------
def test_stalled_runtime_cannot_keep_lease_alive(office: Office) -> None:
    st = office.hub.settings
    t = office.task()
    s = office.session("ben-claude")
    claim = office.claim("ben-claude", t["id"], s["id"], ttl_seconds=600).json()
    office.beat("ben-claude", s["id"], state="executing", progress=True)
    # the quota ran out: the supervisor keeps the session heartbeat alive, but no progress is ever reported
    step = 60
    renew_codes = []
    for _ in range((st.progress_window + st.stall_after) // step + 2):
        office.advance(step)
        office.beat("ben-claude", s["id"], state="executing")
        renew_codes.append(office.c.post(f"/api/tasks/{t['id']}/renew", json={"session_id": s["id"],
                           "lease_epoch": claim["lease_epoch"], "ttl_seconds": 600}, headers=office.h("ben-claude")).status_code)  # fmt: skip
        office.reconcile()
    assert "session.stalled" in office.types()
    assert 409 in renew_codes  # renewal refused once progress is too old (no_progress)
    office.advance(601)
    office.beat("ben-claude", s["id"], state="executing")
    office.reconcile()
    task = office.c.get(f"/api/tasks/{t['id']}", headers=office.admin).json()
    assert task["status"] in ("orphaned", "open") and not task["lease_valid"]  # eventually recoverable


def test_device_heartbeat_does_not_renew_anything(office: Office) -> None:
    t = office.task()
    s = office.session("ben-claude", device="laptop-1")
    office.claim("ben-claude", t["id"], s["id"], ttl_seconds=30)
    for _ in range(10):
        office.advance(20)
        assert (
            office.c.post(
                "/api/devices/heartbeat", json={"device": "laptop-1"}, headers=office.h("ben-claude")
            ).status_code
            == 200
        )
    office.reconcile()
    assert office.c.get(f"/api/tasks/{t['id']}", headers=office.admin).json()["status"] in ("orphaned", "open")


# -- Test 9 / I5: backend restart ----------------------------------------------------------------------
def test_backend_restart_recovers_state_and_reconciles(office: Office, settings) -> None:
    s = _alpha_sessions(office)
    t = office.task()
    office.claim("ben-claude", t["id"], s["ben-claude"], ttl_seconds=60)
    office.c.post("/api/channels/alpha-dev/leases", json={"resource": "src/api/", "ttl_seconds": 60, "session_id": s["ben-claude"]},
                  headers=office.h("ben-claude"))  # fmt: skip
    e = office.c.post(
        "/api/projects/alpha/coordinator/designate", json={"session_id": s["ana-claude"]}, headers=office.admin
    ).json()["epoch"]
    office.c.post(
        "/api/projects/alpha/coordinator/accept",
        json={"session_id": s["ana-claude"], "epoch": e},
        headers=office.h("ana-claude"),
    )
    mid = office.c.post(
        "/api/channels/alpha-dev/messages", json={"body": "@eva-claude review please"}, headers=office.h("ben-claude")
    ).json()["id"]
    # "crash": drop the process state; same SQLite file on disk
    office.hub.db.close()
    office.advance(office.hub.settings.session_ttl + 1)
    hub2 = build_hub(replace(settings))
    set_hub(hub2)
    try:
        office.hub = hub2
        stats = office.reconcile()
        assert stats["tasks_orphaned"] == 1 and stats["coordinators_lost"] == 1
        msgs = office.c.get("/api/channels/alpha-dev/messages", headers=office.admin).json()["messages"]
        assert any(m["id"] == mid for m in msgs)
        assert office.c.get("/api/channels/alpha-dev/leases", headers=office.admin).json() == []
        co = office.c.get("/api/projects/alpha/coordinator", headers=office.admin).json()
        assert co["status"] != "active" or co["valid"]
        actives = hub2.db.query("SELECT COUNT(*) AS n FROM coordinators WHERE status = 'active'")[0]["n"]
        assert actives <= 1
    finally:
        hub2.db.close()


# -- Test 10 / I8: reconnection, at-least-once, idempotency --------------------------------------------
def test_reconnect_recovers_pending_messages_without_duplicates(office: Office) -> None:
    first = office.c.get("/api/inbox", headers=office.h("eva-claude")).json()
    cursor = first["next_after"]
    sent = []
    for i in range(5):  # eva's client is disconnected meanwhile
        r = office.c.post("/api/channels/alpha-dev/messages", json={"body": f"@eva-claude item {i}", "client_id": f"k{i}"},
                          headers=office.h("ben-claude"))  # fmt: skip
        sent.append(r.json()["id"])
    dup = office.c.post("/api/channels/alpha-dev/messages", json={"body": "@eva-claude item 0", "client_id": "k0"},
                        headers=office.h("ben-claude"))  # fmt: skip
    assert dup.json()["duplicate"] is True and dup.json()["id"] == sent[0]
    again = office.c.get("/api/inbox", params={"after": cursor}, headers=office.h("eva-claude")).json()
    got = [m["id"] for m in again["messages"]]
    assert got == sent  # nothing lost, nothing duplicated
    s = office.session("eva-claude")
    for _ in range(2):  # the supervisor re-reports after a restart: idempotent
        r = office.c.post("/api/deliveries", json={"message_ids": got, "state": "delivered_to_supervisor", "session_id": s["id"]},
                          headers=office.h("eva-claude"))  # fmt: skip
        assert r.status_code == 200
    assert r.json()["unchanged"] == got
    # receipts never move backwards
    office.c.post(
        "/api/deliveries", json={"message_ids": got[:1], "state": "acknowledged"}, headers=office.h("eva-claude")
    )
    back = office.c.post(
        "/api/deliveries", json={"message_ids": got[:1], "state": "injected_to_runtime"}, headers=office.h("eva-claude")
    )
    assert back.json()["unchanged"] == got[:1]


# -- Test 15: received but not processed ------------------------------------------------------------------
def test_received_but_unprocessed_message_stays_pending_and_alerts(office: Office) -> None:
    mid = office.c.post("/api/channels/alpha-dev/messages", json={"body": "@cris-opencode urgent: API contract?", "priority": 1},
                        headers=office.h("ben-claude")).json()["id"]  # fmt: skip
    s = office.session("cris-opencode")
    office.c.post("/api/deliveries", json={"message_ids": [mid], "state": "delivered_to_supervisor", "session_id": s["id"]},
                  headers=office.h("cris-opencode"))  # fmt: skip
    office.c.post("/api/deliveries", json={"message_ids": [mid], "state": "injected_to_runtime", "session_id": s["id"]},
                  headers=office.h("cris-opencode"))  # fmt: skip
    bad = office.c.post(
        "/api/deliveries", json={"message_ids": [mid], "state": "processed"}, headers=office.h("cris-opencode")
    )
    assert bad.status_code == 400  # no false "processed" without evidence
    office.c.post(f"/api/sessions/{s['id']}/end", json={"reason": "runtime crashed"}, headers=office.h("cris-opencode"))
    pend = office.c.get("/api/deliveries/pending", headers=office.h("cris-opencode")).json()["pending"]
    assert [p["message_id"] for p in pend] == [mid] and pend[0]["state"] == "injected_to_runtime"
    office.advance(office.hub.settings.delivery_overdue + 1)
    assert office.reconcile()["deliveries_overdue"] == 1
    assert office.reconcile()["deliveries_overdue"] == 0
    st = office.c.get(f"/api/messages/{mid}/deliveries", headers=office.h("ben-claude")).json()
    assert st["recipients"][0]["state"] == "injected_to_runtime"
    # real processing: a reply authored by the recipient is the evidence
    s2 = office.session("cris-opencode")
    reply = office.c.post("/api/channels/alpha-dev/messages", json={"body": "contract is v2", "reply_to": mid},
                          headers=office.h("cris-opencode")).json()  # fmt: skip
    ok = office.c.post("/api/deliveries", json={"message_ids": [mid], "state": "processed", "session_id": s2["id"],
                       "evidence": {"reply_message_id": reply["id"]}}, headers=office.h("cris-opencode"))  # fmt: skip
    assert (
        ok.status_code == 200
        and office.c.get("/api/deliveries/pending", headers=office.h("cris-opencode")).json()["pending"] == []
    )


# -- Test 17 / I6: transactional consistency ----------------------------------------------------------------
def test_crash_between_state_change_and_event_commits_nothing(office: Office, monkeypatch) -> None:
    import agentlink.coordination as coordination

    t = office.task()
    s = office.session("ben-claude")
    real = coordination.emit

    def boom(tx, type_, **kw):
        if type_ == "task.claimed":
            raise RuntimeError("injected crash after UPDATE, before the event")
        return real(tx, type_, **kw)

    monkeypatch.setattr(coordination, "emit", boom)
    try:
        office.claim("ben-claude", t["id"], s["id"])
    except RuntimeError:
        pass  # TestClient re-raises server exceptions
    monkeypatch.setattr(coordination, "emit", real)
    cur = office.c.get(f"/api/tasks/{t['id']}", headers=office.admin).json()
    assert cur["status"] == "open" and cur["lease_epoch"] == 0 and cur["assignee"] is None
    msgs = office.c.get("/api/channels/alpha-dev/messages", headers=office.admin).json()["messages"]
    assert not any("claimed task" in m["body"] for m in msgs)
    r = office.claim("ben-claude", t["id"], s["id"])
    assert r.status_code == 200 and r.json()["lease_epoch"] == 1  # no duplicated transition
    assert office.types(types="task.claimed").count("task.claimed") == 1


def test_every_message_has_its_event(office: Office) -> None:
    for i in range(3):
        office.c.post("/api/channels/alpha-dev/messages", json={"body": f"m{i}"}, headers=office.h("ana-claude"))
    office.task()
    db = office.hub.db
    orphans = db.query("SELECT m.id FROM messages m LEFT JOIN events e ON e.message_id = m.id AND e.type = 'message.created' "
                       "WHERE e.id IS NULL")  # fmt: skip
    assert orphans == []


# -- Test 18 / I9: project isolation ---------------------------------------------------------------------------
def test_project_isolation(office: Office) -> None:
    t = office.task()
    office.c.post("/api/channels/alpha-dev/messages", json={"body": "alpha secret"}, headers=office.h("ana-claude"))
    zed = office.h("zed")
    zs = office.session("zed")
    assert office.c.get("/api/channels/alpha-dev/messages", headers=zed).status_code == 403
    assert office.c.get(f"/api/tasks/{t['id']}", headers=zed).status_code == 403
    assert office.claim("zed", t["id"], zs["id"]).status_code == 403
    assert office.c.get("/api/projects/alpha", headers=zed).status_code == 403
    assert office.c.get("/api/office/snapshot", params={"project": "alpha"}, headers=zed).status_code == 403
    assert office.c.post("/api/sessions", json={"project": "alpha"}, headers=zed).status_code == 403
    assert (
        office.c.post(
            "/api/projects/alpha/coordinator/designate", json={"session_id": zs["id"]}, headers=zed
        ).status_code
        == 403
    )
    evs = office.events("zed")
    assert evs and all(e["project"] == "beta" for e in evs)
    assert all(s["project"] == "beta" for s in office.c.get("/api/sessions", headers=zed).json())
    snap = office.c.get("/api/office/snapshot", headers=zed).json()
    assert [p["name"] for p in snap["projects"]] == ["beta"] and all(s["project"] == "beta" for s in snap["sessions"])
    assert office.c.get(f"/api/tasks/{t['id']}", headers=office.admin).json()["status"] == "open"  # unchanged


def test_sessions_cannot_be_hijacked(office: Office) -> None:
    s = office.session("ben-claude")
    other = office.h("ana-codex")
    assert office.beat("ana-codex", s["id"]).status_code == 403
    assert office.c.post(f"/api/sessions/{s['id']}/end", json={}, headers=other).status_code == 403
    t = office.task()
    assert office.claim("ana-codex", t["id"], s["id"]).status_code == 403
    sub = office.c.post("/api/sessions", json={"project": "alpha", "parent_session_id": s["id"]}, headers=other)
    assert sub.status_code == 403
    # metadata claims are not authority: an agent declaring coordinator=true gets nothing
    r = office.c.post(
        "/api/sessions", json={"project": "alpha", "coordinator": True, "owner": "dev01"}, headers=other
    ).json()
    assert (
        r["owner"] == "dev01"
        and office.c.get("/api/projects/alpha/coordinator", headers=office.admin).json()["status"] == "none"
    )


def test_subagent_sessions_end_with_parent(office: Office) -> None:
    p = office.session("ana-claude")
    kids = [office.c.post("/api/sessions", json={"parent_session_id": p["id"], "specialization": spec},
                          headers=office.h("ana-claude")).json() for spec in ("qa", "frontend")]  # fmt: skip
    assert all(k["parent_id"] == p["id"] for k in kids)
    office.c.post(
        f"/api/sessions/{kids[0]['id']}/end", json={"reason": "subagent done"}, headers=office.h("ana-claude")
    )
    live = {s["id"] for s in office.c.get("/api/sessions", headers=office.admin).json()}
    assert kids[0]["id"] not in live and kids[1]["id"] in live and p["id"] in live
    r = office.c.post(f"/api/sessions/{p['id']}/end", json={}, headers=office.h("ana-claude")).json()
    assert r["sessions_ended"] == 2


def test_path_leases_bound_to_session_release_on_expiry(office: Office) -> None:
    a = office.session("ben-claude")
    b = office.session("cris-opencode")
    assert office.c.post("/api/channels/alpha-dev/leases", json={"resource": "src/auth/", "session_id": a["id"]},
                         headers=office.h("ben-claude")).status_code == 200  # fmt: skip
    r = office.c.post("/api/channels/alpha-dev/leases", json={"resource": "src/auth/jwt.py", "session_id": b["id"]},
                      headers=office.h("cris-opencode"))  # fmt: skip
    assert r.status_code == 409 and r.json()["lease"]["holder"] == "ben-claude"
    office.advance(office.hub.settings.session_ttl + 1)
    office.beat("cris-opencode", b["id"])
    office.reconcile()
    assert office.c.post("/api/channels/alpha-dev/leases", json={"resource": "src/auth/jwt.py", "session_id": b["id"]},
                         headers=office.h("cris-opencode")).status_code == 200  # fmt: skip


def test_legacy_claim_without_session_still_expires(office: Office) -> None:
    t = office.task()
    r = office.claim("ben-claude", t["id"])
    assert r.status_code == 200 and r.json()["lease_valid"]
    office.advance(office.hub.settings.task_lease_default + 1)
    assert office.reconcile()["tasks_orphaned"] == 1


def test_events_cursor_and_resync(office: Office) -> None:
    office.task()
    evs = office.events()
    last = evs[-1]["id"]
    r = office.c.get("/api/events", params={"after": last}, headers=office.admin).json()
    assert r["events"] == [] and r["next_after"] == last and not r["resync"]
    office.hub.db.execute("DELETE FROM events WHERE id <= ?", (last - 1,))  # history archived
    r = office.c.get("/api/events", params={"after": 1}, headers=office.admin).json()
    assert r["resync"] is True
    snap = office.c.get("/api/office/snapshot", headers=office.admin).json()
    assert snap["cursor"] >= last


def test_monitor_scoped_to_project(office: Office, client) -> None:
    r = client.post(
        "/api/admin/agents", json={"name": "watch-beta", "role": "monitor", "projects": ["beta"]}, headers=office.admin
    )
    h = auth(r.json()["token"])
    assert client.get("/api/channels/alpha-dev/messages", headers=h).status_code == 403
    assert client.get("/api/channels/beta-dev/messages", headers=h).status_code == 200
    evs = client.get("/api/events", headers=h).json()["events"]
    assert all(e["project"] == "beta" for e in evs)


def test_stalled_coordinator_with_live_session_loses_authority(office: Office) -> None:
    st = office.hub.settings
    s = _alpha_sessions(office)
    e1 = office.c.post("/api/projects/alpha/coordinator/designate", json={"session_id": s["ana-claude"]},
                       headers=office.admin).json()["epoch"]  # fmt: skip
    office.c.post("/api/projects/alpha/coordinator/accept", json={"session_id": s["ana-claude"], "epoch": e1},
                  headers=office.h("ana-claude"))  # fmt: skip
    office.beat("ana-claude", s["ana-claude"], state="executing", progress=True)
    # ana's process stays alive (heartbeats) but stops making progress: stalled => lease no longer renewed
    for _ in range((st.stall_after + st.coordinator_lease) // 30 + 2):
        office.advance(30)
        for n in s:
            office.beat(n, s[n], state="executing" if n == "ana-claude" else "idle")
        office.reconcile()
    co = office.c.get("/api/projects/alpha/coordinator", headers=office.admin).json()
    assert co["epoch"] > e1 and co.get("session_id") != s["ana-claude"]
    t = office.task(who="ben-claude")
    r = office.c.post("/api/projects/alpha/coordinator/assign",
                      json={"session_id": s["ana-claude"], "epoch": e1, "task_id": t["id"], "assignee": "ben-claude"},
                      headers=office.h("ana-claude"))  # fmt: skip
    assert r.status_code == 409 and r.json()["reason"] == "stale_epoch"
