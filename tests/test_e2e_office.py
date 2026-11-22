"""Phase 8 integral scenario: five participants, each an independent client (own device dir, config, local store,
supervisor and HTTP connection) against a real uvicorn server. Synthetic: no LLM is called — this verifies
coordination and recovery; real-runtime communication is verified separately (scripts/e2e_runtimes.py).

five participants -> several sessions -> tasks -> claims -> messaging -> progress -> agent failure -> automatic
recovery -> reassignment -> coordinator failure -> succession -> completion -> dashboard verification
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentlink.local import ApiError

from .office_helpers import Office
from .test_client import Participant, server  # noqa: F401  (fixture)

TEAM = {  # participant (owner) -> agent
    "dev01": "ana-claude",
    "dev02": "ben-claude",
    "dev03": "cris-opencode",
    "dev04": "dani-codex",
    "dev05": "eva-claude",
}


def test_five_participants_full_lifecycle(
    tmp_path: Path,
    server: str,  # noqa: F811 (fixture)
    office: Office,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    st = office.hub.settings
    ps = {
        owner: Participant(tmp_path, server, office.tokens[a], "alpha", monkeypatch, f"{owner}-laptop")
        for owner, a in TEAM.items()
    }
    api = {a: ps[o].api for o, a in TEAM.items()}
    sup = {a: ps[o].sup for o, a in TEAM.items()}

    def call(agent: str, method: str, path: str, body: Any = None) -> Any:
        return api[agent].call(method, path, body)

    def refused(agent: str, method: str, path: str, body: Any) -> ApiError:
        with pytest.raises(ApiError) as e:
            call(agent, method, path, body)
        return e.value

    # -- several sessions (dev01 also runs a sub-agent) -------------------------------------------------------------
    sid = {
        a: call(a, "POST", "/api/sessions", {"project": "alpha", "runtime": "synthetic"})["id"] for a in TEAM.values()
    }
    sub = call(
        "ana-claude",
        "POST",
        "/api/sessions",
        {"parent_session_id": sid["ana-claude"], "runtime": "synthetic", "specialization": "qa"},
    )
    assert sub["parent_id"] == sid["ana-claude"]

    def beat(*agents: str, **kw: Any) -> None:
        for a in agents:
            call(a, "POST", f"/api/sessions/{sid[a]}/heartbeat", {"progress": True, "state": "executing", **kw})

    # -- automatic coordinator election: the best candidate (ana, priority 10) is offered and accepts ---------------------
    office.reconcile()
    c = call("eva-claude", "GET", "/api/projects/alpha/coordinator")
    assert c["status"] == "offered" and c["offered_session"] == sid["ana-claude"]
    c = call(
        "ana-claude",
        "POST",
        "/api/projects/alpha/coordinator/accept",
        {"session_id": sid["ana-claude"], "epoch": c["epoch"]},
    )
    assert c["status"] == "active"
    epoch1 = c["epoch"]

    # -- tasks with a dependency; the coordinator assigns; claims -----------------------------------------------------
    t1 = call("ana-claude", "POST", "/api/channels/alpha-dev/tasks", {"title": "Login API", "priority": 1})["id"]
    t2 = call("ana-claude", "POST", "/api/channels/alpha-dev/tasks", {"title": "Login screen", "depends_on": [t1]})[
        "id"
    ]
    t3 = call("ana-claude", "POST", "/api/channels/alpha-dev/tasks", {"title": "CI pipeline"})["id"]
    call(
        "ana-claude",
        "POST",
        "/api/projects/alpha/coordinator/assign",
        {"session_id": sid["ana-claude"], "epoch": epoch1, "task_id": t1, "assignee": "ben-claude"},
    )
    ben = call("ben-claude", "POST", f"/api/tasks/{t1}/claim", {"session_id": sid["ben-claude"]})
    assert ben["assignee"] == "ben-claude" and ben["lease_epoch"] == 1
    dani3 = call("dani-codex", "POST", f"/api/tasks/{t3}/claim", {"session_id": sid["dani-codex"]})
    assert (
        refused("cris-opencode", "POST", f"/api/tasks/{t2}/claim", {"session_id": sid["cris-opencode"]}).status == 409
    )  # t1 not done
    assert (
        refused("cris-opencode", "POST", f"/api/tasks/{t1}/claim", {"session_id": sid["cris-opencode"]}).status == 409
    )  # held by ben

    # -- inter-agent messaging through the supervisors, with delivery states ----------------------------------------------
    q = call(
        "cris-opencode",
        "POST",
        "/api/channels/alpha-dev/messages",
        {"body": "@ben-claude what does POST /login return?", "kind": "question"},
    )
    assert q["id"] in sup["ben-claude"].once(wait=0)["new"]
    r = call(
        "ben-claude",
        "POST",
        "/api/channels/alpha-dev/messages",
        {"body": "200 {token}", "reply_to": q["id"], "mentions": ["cris-opencode"]},
    )
    call(
        "ben-claude",
        "POST",
        "/api/deliveries",
        {
            "message_ids": [q["id"]],
            "state": "processed",
            "session_id": sid["ben-claude"],
            "evidence": {"reply_message_id": r["id"]},
        },
    )
    assert r["id"] in sup["cris-opencode"].once(wait=0)["new"]
    assert call("cris-opencode", "GET", f"/api/messages/{q['id']}/deliveries")["recipients"][0]["state"] == "processed"

    # -- progress + checkpoint ---------------------------------------------------------------------------------------
    beat(*TEAM.values())
    call(
        "ben-claude",
        "PATCH",
        f"/api/tasks/{t1}",
        {"status": "in_progress", "session_id": sid["ben-claude"], "lease_epoch": 1},
    )
    call("ben-claude", "POST", f"/api/tasks/{t1}/checkpoints", {"session_id": sid["ben-claude"], "lease_epoch": 1,
         "summary": "handler + tests written", "branch": "feat/login-api", "commit": "abc1234", "pending": "wire JWT"})  # fmt: skip

    # -- agent failure: ben's device dies (no more heartbeats); everybody else keeps working -----------------------------
    office.advance(st.session_ttl + 1)
    beat("ana-claude", "cris-opencode", "dani-codex", "eva-claude")
    stats = office.reconcile()
    assert stats["sessions_expired"] >= 1 and stats["tasks_orphaned"] == 1
    orphan = [e for e in office.events(types="task.orphaned") if e["task_id"] == t1][-1]
    assert orphan["data"]["previous_holder"] == "ben-claude" and orphan["data"]["checkpoint_age_s"] is not None

    # -- automatic recovery + reassignment: dani takes over from the checkpoint, generation 2 ----------------------------
    dani1 = call("dani-codex", "POST", f"/api/tasks/{t1}/claim", {"session_id": sid["dani-codex"]})
    assert dani1["assignee"] == "dani-codex" and dani1["lease_epoch"] == 2
    cps = call("dani-codex", "GET", f"/api/tasks/{t1}/checkpoints")
    assert (
        cps[0]["branch"] == "feat/login-api" and cps[0]["pending"] == "wire JWT" and cps[0]["epoch"] == 1
    )  # newest first
    # ben comes back late and tries to finish with his old generation: refused (fencing)
    e = refused(
        "ben-claude", "PATCH", f"/api/tasks/{t1}", {"status": "done", "session_id": sid["ben-claude"], "lease_epoch": 1}
    )
    assert e.status == 409

    # -- coordinator failure: ana disappears; eva (priority 5) is offered and accepts epoch+1 ----------------------------
    office.advance(st.coordinator_lease + 1)
    beat("cris-opencode", "dani-codex", "eva-claude")
    stats = office.reconcile()
    assert stats["coordinators_lost"] == 1
    c = call("eva-claude", "GET", "/api/projects/alpha/coordinator")
    assert c["status"] == "offered" and c["offered_session"] == sid["eva-claude"] and c["epoch"] == epoch1 + 1
    assert any("COORDINATOR SUCCESSION" in m["body"] for m in call("eva-claude", "GET", "/api/inbox")["messages"])
    c = call(
        "eva-claude",
        "POST",
        "/api/projects/alpha/coordinator/accept",
        {"session_id": sid["eva-claude"], "epoch": c["epoch"]},
    )
    assert c["status"] == "active" and c["epoch"] == epoch1 + 1
    # the old coordinator cannot act with its old epoch (no split brain)
    assert refused("ana-claude", "POST", "/api/projects/alpha/coordinator/assign",
                   {"session_id": sid["ana-claude"], "epoch": epoch1, "task_id": t2, "assignee": "ana-claude"}).status in (403, 409)  # fmt: skip

    # -- completion: t1 and t3 done, which unblocks t2; cris completes it ------------------------------------------------
    call(
        "dani-codex", "PATCH", f"/api/tasks/{t1}", {"status": "done", "session_id": sid["dani-codex"], "lease_epoch": 2}
    )
    call(
        "dani-codex",
        "PATCH",
        f"/api/tasks/{t3}",
        {"status": "done", "session_id": sid["dani-codex"], "lease_epoch": dani3["lease_epoch"]},
    )
    cris2 = call("cris-opencode", "POST", f"/api/tasks/{t2}/claim", {"session_id": sid["cris-opencode"]})
    call(
        "cris-opencode",
        "PATCH",
        f"/api/tasks/{t2}",
        {"status": "done", "session_id": sid["cris-opencode"], "lease_epoch": cris2["lease_epoch"]},
    )

    # -- dashboard verification: what the 3D office and /chat render is the real state -----------------------------------
    mon = office.c.post("/api/admin/agents", json={"name": "wall", "role": "monitor"}, headers=office.admin).json()[
        "token"
    ]
    snap = office.c.get("/api/office/snapshot", headers={"Authorization": f"Bearer {mon}"}).json()
    ses = {s["id"]: s for s in snap["sessions"]}
    assert ses[sid["ben-claude"]]["status"] in ("offline", "terminated")
    assert ses[sid["eva-claude"]]["status"] == "executing" and ses[sid["eva-claude"]]["floor"] == "qa"
    assert ses[sid["dani-codex"]]["floor"] == "devops" and ses[sub["id"]]["parent_id"] == sid["ana-claude"]
    assert snap["coordinators"]["alpha"]["session_id"] == sid["eva-claude"] and snap["coordinators"]["alpha"]["valid"]
    done = {t["id"]: t for t in snap["tasks"] if t["project"] == "alpha"}
    assert all(done[t]["status"] == "done" for t in (t1, t2, t3))
    types = set(office.types())
    for needed in ("session.registered", "coordinator.offered", "coordinator.accepted", "task.claimed", "task.checkpoint",
                   "session.expired", "task.orphaned", "task.stale_write", "coordinator.lost", "message.created"):  # fmt: skip
        assert needed in types, needed
    # invariants at the end: one valid coordinator, no task with two holders
    assert sum(1 for x in snap["coordinators"].values() if x["valid"]) == 1
