"""Codex auto-resume: new messages are queued on the participant's own Codex thread (`codex queue --thread`), so an
open terminal shows them as a prompt, instead of a separate `codex exec resume` that conflicts with it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentlink import hooks
from agentlink.adapters import ADAPTERS

from .office_helpers import Office
from .test_client import Participant, _deliv, server  # noqa: F401  (fixture)
from .test_review_fixes import _fake_codex


def _codex(tmp_path: Path, server: str, office: Office, mp: pytest.MonkeyPatch) -> Participant:  # noqa: F811
    p = Participant(tmp_path, server, office.tokens["dani-codex"], "alpha", mp, "dev04-laptop")
    p.sup.cfg["runtime"] = "codex"
    p.store.put("adapter.codex", {"auto_resume": True})
    return p


def _hook(p: Participant, event: str, thread: str = "thread-1") -> dict[str, Any] | None:
    p.use()
    return hooks.handle("codex", event, {"session_id": thread, "cwd": "/repo"}, p.store, p.api)


def _calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _recorder(tmp_path: Path, mp: pytest.MonkeyPatch, rc: int = 0) -> Path:
    log = tmp_path / "codex-calls.jsonl"
    _fake_codex(
        tmp_path, mp, f"python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' \"$@\" >> {log}\nexit {rc}\n"
    )
    return log


def test_idle_codex_session_gets_the_message_queued_on_its_thread(
    tmp_path: Path,
    server: str,  # noqa: F811
    office: Office,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _recorder(tmp_path, monkeypatch)
    codex = _codex(tmp_path, server, office, monkeypatch)
    asker = Participant(tmp_path, server, office.tokens["ana-claude"], "alpha", monkeypatch, "dev01-laptop")
    _hook(codex, "SessionStart")
    _hook(codex, "Stop")  # the terminal is idle, waiting for the human
    mid = asker.api.call("POST", "/api/channels/alpha-dev/messages", {"body": "@dani-codex ping"})["id"]
    codex.use()
    assert codex.sup.once(wait=0)["pushed"] == ["codex queue"]
    (argv,) = _calls(log)
    assert argv[:4] == ["queue", "--thread", "thread-1", "--message"]
    assert f"#{mid}" in argv[4] and "ping" in argv[4] and "agentlink reply" in argv[4]  # the agent answers itself
    assert _deliv(office, mid) == "injected_to_runtime"
    codex.sup.once(wait=0)
    assert len(_calls(log)) == 1  # never queued twice
    _hook(codex, "UserPromptSubmit")  # the queued prompt starts the turn: no duplicate injection
    assert codex.store.pending(only_new=True) == []


def test_busy_codex_session_is_not_pushed(
    tmp_path: Path,
    server: str,  # noqa: F811
    office: Office,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _recorder(tmp_path, monkeypatch)
    codex = _codex(tmp_path, server, office, monkeypatch)
    asker = Participant(tmp_path, server, office.tokens["ana-claude"], "alpha", monkeypatch, "dev01-laptop")
    _hook(codex, "SessionStart")
    _hook(codex, "Stop")
    _hook(codex, "UserPromptSubmit")  # the human started a new turn: busy again, the hooks deliver at Stop
    asker.api.call("POST", "/api/channels/alpha-dev/messages", {"body": "@dani-codex ping"})
    codex.use()
    assert codex.sup.once(wait=0)["pushed"] == [] and _calls(log) == []


def test_failed_queue_leaves_the_message_pending_and_backs_off(
    tmp_path: Path,
    server: str,  # noqa: F811
    office: Office,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _recorder(tmp_path, monkeypatch, rc=1)
    codex = _codex(tmp_path, server, office, monkeypatch)
    asker = Participant(tmp_path, server, office.tokens["ana-claude"], "alpha", monkeypatch, "dev01-laptop")
    _hook(codex, "SessionStart")
    _hook(codex, "Stop")
    mid = asker.api.call("POST", "/api/channels/alpha-dev/messages", {"body": "@dani-codex ping"})["id"]
    codex.use()
    assert codex.sup.once(wait=0)["pushed"] == []
    assert _deliv(office, mid) == "delivered_to_supervisor"  # not reported as injected
    codex.sup.once(wait=0)
    assert len(_calls(log)) == 1  # backoff: not retried every iteration
    assert codex.store.get("push_failed.codex.thread-1")["at"]


def test_queue_is_off_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = _recorder(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENTLINK_HOME", str(tmp_path))
    from agentlink.local import Store

    assert ADAPTERS["codex"].push(Store(tmp_path / "s.db"), {"local_id": "t"}, "hi") is None and _calls(log) == []
