"""Local supervisor + hooks + adapters against a real HTTP server (uvicorn on a free localhost port).

Phase 2 vertical flow (A -> server -> supervisor B -> runtime B -> reply -> A), restart/reconnect recovery,
reversible installation, lease renewal policy. No model is ever invoked: hook payloads are the documented
Claude Code / Codex hook inputs, produced deterministically.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from agentlink import hooks
from agentlink.adapters import ADAPTERS, HOOK_MARK
from agentlink.app import create_app
from agentlink.local import Api, ApiError, Store
from agentlink.supervisor import Supervisor

from .office_helpers import Office


@pytest.fixture
def server(hub, office: Office) -> Iterator[str]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    cfg = uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    th.join(5)


class Participant:
    """One device: its own AGENTLINK_HOME, config, store, supervisor and a fake runtime session."""

    def __init__(
        self, tmp: Path, server: str, token: str, project: str, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        self.home = tmp / name
        self.home.mkdir()
        (self.home / "config.json").write_text(
            json.dumps({"server": server, "token": token, "project": project, "device": name})
        )
        self.mp = monkeypatch
        self.name = name
        self.use()
        self.store = Store(self.home / "state.db")
        self.api = Api(server, token)
        self.sup = Supervisor(self.api, self.store, {"device": name, "project": project}, poll_wait=0)

    def use(self) -> None:
        self.mp.setenv("AGENTLINK_HOME", str(self.home))
        for k in ("AGENTLINK_URL", "AGENTLINK_TOKEN", "AGENTLINK_PROJECT", "AGENTLINK_SESSION"):
            self.mp.delenv(k, raising=False)

    def hook(self, event: str, local_id: str = "rt-1", **payload: Any) -> dict[str, Any] | None:
        self.use()
        return hooks.handle(
            "claude-code", event, {"session_id": local_id, "cwd": "/repo", **payload}, self.store, self.api
        )


@pytest.fixture
def pair(
    tmp_path: Path, server: str, office: Office, monkeypatch: pytest.MonkeyPatch
) -> tuple[Participant, Participant]:
    a = Participant(tmp_path, server, office.tokens["ben-claude"], "alpha", monkeypatch, "dev02-laptop")
    b = Participant(tmp_path, server, office.tokens["cris-opencode"], "alpha", monkeypatch, "dev03-laptop")
    return a, b


def _deliv(office: Office, mid: int) -> str:
    return office.c.get(f"/api/messages/{mid}/deliveries", headers=office.admin).json()["recipients"][0]["state"]


def test_vertical_flow_two_supervisors(pair: tuple[Participant, Participant], office: Office) -> None:
    a, b = pair
    ctx_a = a.hook("SessionStart")
    assert "connected to Agent Office" in ctx_a["hookSpecificOutput"]["additionalContext"]
    b.hook("SessionStart", local_id="rt-b")
    sa = a.store.session("rt-1")["server_id"]
    sb = b.store.session("rt-b")["server_id"]
    assert sa.startswith("ses_") and sb.startswith("ses_") and sa != sb  # each client has its own identity
    # A asks B (through the server)
    msg = a.api.call(
        "POST", "/api/channels/alpha-dev/messages", {"body": "@cris-opencode what is the API contract for /login?"}
    )
    assert _deliv(office, msg["id"]) == "queued"
    # B's supervisor receives and persists it, then confirms
    assert b.sup.once(wait=0)["new"] == [msg["id"]]
    assert _deliv(office, msg["id"]) == "delivered_to_supervisor"
    # next safe point in B's runtime: the hook injects it into the model context
    out = b.hook("UserPromptSubmit", local_id="rt-b", prompt="continue")
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert f"#{msg['id']}" in ctx and "@ben-claude" in ctx and "API contract" in ctx
    assert _deliv(office, msg["id"]) == "injected_to_runtime"
    assert b.hook("UserPromptSubmit", local_id="rt-b", prompt="x") is None  # not injected twice
    # B's agent answers through the protocol (what `agentlink reply` does)
    reply = b.api.call("POST", "/api/channels/alpha-dev/messages", {"body": "POST /login {user, pass} -> 200 {token}",
                       "reply_to": msg["id"], "mentions": ["ben-claude"]})  # fmt: skip
    b.api.call("POST", "/api/deliveries", {"message_ids": [msg["id"]], "state": "processed", "session_id": sb,
               "evidence": {"reply_message_id": reply["id"]}})  # fmt: skip
    assert _deliv(office, msg["id"]) == "processed"
    # A receives the response
    assert a.sup.once(wait=0)["new"] == [reply["id"]]
    stop = a.hook("Stop")
    assert stop["decision"] == "block" and "200 {token}" in stop["reason"]
    assert a.hook("Stop", stop_hook_active=True) is None  # never loops
    # the dashboard stream reflects the exchange
    types = office.types()
    assert types.count("message.created") >= 2 and "session.registered" in types


def test_supervisor_restart_and_store_loss_lose_nothing(pair: tuple[Participant, Participant], office: Office) -> None:
    a, b = pair
    b.hook("SessionStart", local_id="rt-b")
    ids = [
        a.api.call("POST", "/api/channels/alpha-dev/messages", {"body": f"@cris-opencode n{i}"})["id"] for i in range(3)
    ]
    b.sup.once(wait=0)
    # supervisor process dies before the runtime got anything: a fresh one finds it all in the local store
    sup2 = Supervisor(b.api, Store(b.home / "state.db"), {"device": "dev03-laptop", "project": "alpha"}, poll_wait=0)
    assert sup2.once(wait=0)["new"] == []  # no duplicates
    assert [m["message_id"] for m in b.store.pending()] == ids
    # worst case: the local store is wiped; the server still knows what is pending
    for f in b.home.glob("state.db*"):
        f.unlink()
    fresh = Store(b.home / "state.db")
    sup3 = Supervisor(b.api, fresh, {"device": "dev03-laptop", "project": "alpha"}, poll_wait=0)
    assert sup3.reconcile_pending() == 3
    assert [m["message_id"] for m in fresh.pending()] == ids


def test_supervisor_heartbeats_and_renewal_policy(pair: tuple[Participant, Participant], office: Office) -> None:
    a, _ = pair
    a.hook("SessionStart")
    sid = a.store.session("rt-1")["server_id"]
    t = office.task()
    claim = a.api.call("POST", f"/api/tasks/{t['id']}/claim", {"session_id": sid, "ttl_seconds": 120})
    a.hook("PostToolUse", tool_name="Edit")  # observable progress
    a.sup.session_beats(force=True)
    assert [h["task_id"] for h in a.store.q("SELECT * FROM held")] == [t["id"]]
    assert a.sup.renew_leases(force=True)[0]["renewed"] is True
    # runtime goes quiet (quota exhausted): renewals carry no progress; once the window passes, authority is lost
    a.store.x("UPDATE sessions SET last_progress = last_progress - 10000")
    office.advance(office.hub.settings.progress_window + 1)
    a.sup.session_beats(force=True)
    res = a.sup.renew_leases(force=True)
    assert res[0]["renewed"] is False and res[0]["reason"] in ("no_progress", "stale_lease")
    notice = a.hook("PostToolUse", tool_name="Bash")  # the loss is surfaced at the next safe point
    assert "AUTHORITY LOST" in notice["hookSpecificOutput"]["additionalContext"]
    assert claim["lease_epoch"] == 1


def test_subagent_sessions_from_hooks(pair: tuple[Participant, Participant], office: Office) -> None:
    a, _ = pair
    a.hook("SessionStart")
    a.hook("SubagentStart", agent_id="sub-7", agent_type="qa")
    parent = a.store.session("rt-1")["server_id"]
    subs = [s for s in office.c.get("/api/sessions", headers=office.admin).json() if s["parent_id"] == parent]
    assert len(subs) == 1 and subs[0]["specialization"] == "qa"
    a.hook("SubagentStop", agent_id="sub-7")
    assert not [s for s in office.c.get("/api/sessions", headers=office.admin).json() if s["parent_id"] == parent]
    a.hook("SessionEnd", reason="exit")
    assert all(s["id"] != parent for s in office.c.get("/api/sessions", headers=office.admin).json())


def test_hook_fails_open_when_server_is_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTLINK_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"server": "http://127.0.0.1:9", "token": "al_x", "project": "p"}))
    store = Store(tmp_path / "state.db")
    out = hooks.handle("claude-code", "SessionStart", {"session_id": "s1"}, store, Api("http://127.0.0.1:9", "al_x"))
    assert "connected to Agent Office" in out["hookSpecificOutput"]["additionalContext"]  # agent keeps working
    assert store.session("s1")["server_id"] == ""  # the supervisor will register it later


def test_install_is_reversible_and_preserves_existing_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTLINK_HOME", str(tmp_path / "home"))
    proj = tmp_path / "repo"
    (proj / ".claude").mkdir(parents=True)
    original = {
        "permissions": {"allow": ["Bash(ls)"]},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}]},
    }
    (proj / ".claude" / "settings.json").write_text(json.dumps(original))
    ad = ADAPTERS["claude-code"]
    r = ad.install("project", str(proj))
    assert set(r["added"]) >= {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"} and r["backup"]
    again = ad.install("project", str(proj))
    assert again["added"] == []  # idempotent, no duplicates
    data = json.loads((proj / ".claude" / "settings.json").read_text())
    assert any("notify-send" in h["command"] for e in data["hooks"]["Stop"] for h in e["hooks"])
    assert ad.doctor("project", str(proj))["hooks_missing"] == []
    ad.uninstall("project", str(proj))
    assert json.loads((proj / ".claude" / "settings.json").read_text()) == original
    assert not any(HOOK_MARK in json.dumps(x) for x in [original])


def test_runtime_unavailable_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    for name in ("claude-code", "codex", "opencode"):
        assert ADAPTERS[name].detect()["available"] is False
    assert ADAPTERS["generic"].capabilities["injection"] == "UNSUPPORTED"


def test_stale_supervisor_report_is_rejected_cleanly(pair: tuple[Participant, Participant], office: Office) -> None:
    a, b = pair
    msg = a.api.call("POST", "/api/channels/alpha-dev/messages", {"body": "@cris-opencode hello"})
    with pytest.raises(ApiError) as e:  # a supervisor cannot confirm messages addressed to someone else
        a.api.call("POST", "/api/deliveries", {"message_ids": [msg["id"]], "state": "acknowledged"})
    assert e.value.status == 404


def test_opt_in_headless_delivery_runs_once_and_relays_answer(
    tmp_path: Path, server: str, office: Office, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex without trusted hooks: the supervisor answers NEW messages with one headless turn (fake `codex` binary)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls"
    fake = bindir / "codex"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo run >> "{calls}"\n'
        'while [ $# -gt 0 ]; do if [ "$1" = "-o" ]; then out="$2"; shift; fi; shift; done\n'
        'printf "POST /login returns 200 {token}" > "$out"\n'
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{__import__('os').environ['PATH']}")
    asker = Participant(tmp_path, server, office.tokens["ben-claude"], "alpha", monkeypatch, "dev02-laptop")
    codex = Participant(tmp_path, server, office.tokens["dani-codex"], "alpha", monkeypatch, "dev04-laptop")
    codex.sup.cfg["runtime"] = "codex"
    assert codex.sup.once(wait=0)["headless"] is None  # not enabled: nothing runs
    codex.store.put("adapter.codex", {"headless": True, "workdir": str(tmp_path)})
    q = asker.api.call(
        "POST", "/api/channels/alpha-dev/messages", {"body": "@dani-codex what does POST /login return?"}
    )
    res = codex.sup.once(wait=0)["headless"]
    assert res["exit_code"] == 0 and res["processed"] == [q["id"]]
    assert _deliv(office, q["id"]) == "processed"
    reply = next(m for m in asker.api.call("GET", "/api/inbox")["messages"] if m.get("reply_to") == q["id"])
    assert reply["author"] == "dani-codex" and "200 {token}" in reply["body"]
    assert codex.sup.once(wait=0)["headless"] is None  # already handled: no second paid turn
    assert calls.read_text().count("run") == 1


class _Clock:
    """Fake time for the rewake waiter: every sleep advances it, so loops end without real waiting."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def test_rewake_waiter_wakes_idle_session_once(pair: tuple[Participant, Participant], office: Office) -> None:
    a, b = pair
    b.hook("SessionStart", local_id="rt-b")
    b.hook("Stop", local_id="rt-b")  # the session is idle: only the waiter can reach it now
    clock = _Clock()
    assert hooks.wait("claude-code", {"session_id": "rt-b"}, b.store, b.api, max_seconds=30, clock=clock,
                      sleep=clock.sleep) is None  # fmt: skip
    assert clock.t >= 1030  # nothing arrived: it waited the whole budget and gave up quietly
    msg = a.api.call("POST", "/api/channels/alpha-dev/messages", {"body": "@cris-opencode are you there?"})
    b.sup.once(wait=0)
    text = hooks.wait("claude-code", {"session_id": "rt-b"}, b.store, b.api, max_seconds=30, clock=clock,
                      sleep=clock.sleep)  # fmt: skip
    assert text and f"#{msg['id']}" in text and "are you there?" in text
    assert _deliv(office, msg["id"]) == "injected_to_runtime"
    assert b.hook("UserPromptSubmit", local_id="rt-b", prompt="x") is None  # the waiter won it: not shown twice
    assert b.hook("Stop", local_id="rt-b") is None
    assert b.store.get("rewake.rt-b") is None  # the hold is released on exit


def test_rewake_waiter_is_single_instance_and_stops_with_the_session(
    pair: tuple[Participant, Participant], office: Office
) -> None:
    a, b = pair
    b.hook("SessionStart", local_id="rt-b")
    clock = _Clock()
    assert b.store.hold("rewake.rt-b", "other-waiter", clock(), hooks.WAIT_STALE)
    a.api.call("POST", "/api/channels/alpha-dev/messages", {"body": "@cris-opencode ping"})
    b.sup.once(wait=0)
    w = {"session_id": "rt-b"}
    assert hooks.wait("claude-code", w, b.store, b.api, clock=clock, sleep=clock.sleep) is None  # a live one serves it
    assert len(b.store.pending(only_new=True)) == 1  # untouched for the live waiter
    clock.t += hooks.WAIT_STALE + 1  # the other waiter died without releasing: it is taken over
    assert "ping" in (hooks.wait("claude-code", w, b.store, b.api, clock=clock, sleep=clock.sleep) or "")
    b.hook("SessionEnd", local_id="rt-b", reason="exit")
    a.api.call("POST", "/api/channels/alpha-dev/messages", {"body": "@cris-opencode later"})
    b.sup.once(wait=0)
    assert hooks.wait("claude-code", w, b.store, b.api, clock=clock, sleep=clock.sleep) is None  # session over
    assert hooks.wait("claude-code", {}, b.store, b.api) is None  # no session id: nothing to wake


def test_rewake_hook_exit_codes(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import io

    for text, code in (("[AgentOffice] 1 message(s)", 2), (None, 0)):
        monkeypatch.setattr(hooks, "wait", lambda *a, _t=text, **k: _t)
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b'{"session_id": "s"}')))
        with pytest.raises(SystemExit) as e:
            hooks.main("claude-code", "Wait")
        assert e.value.code == code
        err = capsys.readouterr()
        assert err.out == "" and ((text or "") in err.err)


def test_install_arms_rewake_only_where_supported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTLINK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    proj = tmp_path / "repo"
    proj.mkdir()
    ad = ADAPTERS["claude-code"]
    ad.install("project", str(proj))
    data = json.loads((proj / ".claude" / "settings.json").read_text())["hooks"]
    for ev in ("SessionStart", "Stop"):
        waiters = [h for e in data[ev] for h in e["hooks"] if h["command"].endswith(" Wait")]
        assert len(waiters) == 1 and waiters[0]["asyncRewake"] is True and waiters[0]["timeout"] >= hooks.WAIT_MAX
    assert not any(h["command"].endswith(" Wait") for e in data["UserPromptSubmit"] for h in e["hooks"])
    assert ad.doctor("project", str(proj))["rewake_missing"] == []
    assert ad.install("project", str(proj))["added"] == []  # idempotent with the extra hook too
    ad.uninstall("project", str(proj))
    assert "hooks" not in json.loads((proj / ".claude" / "settings.json").read_text())
    ADAPTERS["codex"].install("user")
    assert "Wait" not in (tmp_path / "codex" / "hooks.json").read_text()
