"""Client smoke on any OS: a real server process and the real `agentlink` CLI as subprocesses (no LLM, no network).

connect → status → doctor → a hook run by the platform's shell → receive (supervise --once) → inbox → reply →
disconnect, with AGENTLINK_HOME and the repository under paths with spaces and non-ASCII characters. This is the
test the Windows CI job relies on; scripts/smoke.sh keeps covering the server side on Linux.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ADMIN = "al_smoke-admin-token-0123456789abcdefghijklmnop"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http(url: str, method: str = "GET", token: str = ADMIN, body: Any = None) -> Any:
    req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode())
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("content-type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"null")


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    port = _free_port()
    env = {**os.environ, "AGENTLINK_ADMIN_TOKEN": ADMIN, "AGENTLINK_DB_PATH": str(tmp_path / "server.db")}
    log = open(tmp_path / "server.log", "w")  # noqa: SIM115
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentlink.serve", "--port", str(port), "--env-file", str(tmp_path / "none.env")],
        env=env, stdout=log, stderr=subprocess.STDOUT,
    )  # fmt: skip
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(150):
            try:
                _http(f"{url}/api/health")
                break
            except OSError:
                assert proc.poll() is None, (tmp_path / "server.log").read_text()
                time.sleep(0.2)
        yield url
    finally:
        proc.terminate()
        proc.wait(10)
        log.close()


def test_client_lifecycle_on_this_os(server: str, tmp_path: Path) -> None:
    _http(f"{server}/api/projects", "POST", body={"name": "alpha"})
    tokens = {}
    for name in ("ana", "ben"):
        agent = {"name": name, "owner": f"dev-{name}", "specialization": "backend", "projects": ["alpha"]}
        tokens[name] = _http(f"{server}/api/admin/agents", "POST", body=agent)["token"]
    _http(f"{server}/api/channels", "POST", body={"name": "alpha-dev", "project": "alpha", "members": ["ana", "ben"]})

    home = tmp_path / "ben home ñ"
    repo = tmp_path / "my repo ñ"
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTLINK_")}
    user_home = tmp_path / "user home ñ"  # `disconnect` removes the supervisor service under the user's home:
    user_home.mkdir()  # never let it reach the real one of whoever runs the suite
    env.update(AGENTLINK_HOME=str(home), HOME=str(user_home), USERPROFILE=str(user_home), PYTHONIOENCODING="utf-8")
    if os.name != "nt":  # the installed command itself lives under a path with spaces and non-ASCII characters
        bindir = tmp_path / "bin dir ñ"
        bindir.mkdir()
        wrapper = bindir / "agentlink"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m agentlink.cli "$@"\n', "utf-8")
        wrapper.chmod(0o755)
        env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"

    def cli(*args: str, ok: bool = True) -> Any:
        r = subprocess.run([sys.executable, "-m", "agentlink.cli", *args], env=env, capture_output=True,
                           encoding="utf-8", timeout=60, check=False)  # fmt: skip
        assert (r.returncode == 0) == ok, r.stdout + r.stderr
        return json.loads(r.stdout) if r.stdout.strip() else None

    scope = ["--scope", "project", "--dir", str(repo)]
    con = cli("connect", "--server", server, "--token", tokens["ben"], "--project", "alpha", "--runtime", "claude-code",
              "--no-service", *scope)  # fmt: skip
    assert con["agent"] == "ben" and con["doctor"]["hooks_missing"] == []
    assert cli("status")["agent"] == "ben"
    doc = cli("doctor", *scope)
    assert doc["server_ok"] is True and doc["config"]["token"] == "***"
    assert doc["runtimes"]["claude-code"]["hooks_missing"] == []

    # the runtime runs the installed hook through its shell: sh on POSIX, cmd.exe on Windows (Git Bash parses the
    # same quoting as sh, covered by tests/test_cross_platform.py)
    settings = json.loads((repo / ".claude" / "settings.json").read_text("utf-8"))
    command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    if os.name != "nt":
        assert "bin dir ñ/agentlink" in command
    shells: list[Any] = [["sh", "-c", command]]
    if os.name == "nt":  # a string, so cmd.exe gets the line verbatim (a list would be re-quoted for C programs)
        shells = [f"cmd.exe /d /c {command}"]
        git_bash = Path(os.environ.get("PROGRAMFILES", "C:\\Program Files")) / "Git" / "bin" / "bash.exe"
        if git_bash.exists():  # what Claude Code uses on Windows
            shells.append([str(git_bash), "-c", command])
    for i, shell in enumerate(shells):
        payload = json.dumps({"session_id": f"rt-{i}", "cwd": str(repo)}, ensure_ascii=False).encode("utf-8")
        hook = subprocess.run(shell, input=payload, env=env, capture_output=True, timeout=60, check=False)
        assert hook.returncode == 0 and hook.stdout, (shell, hook.stdout, hook.stderr)
        ctx = json.loads(hook.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "connected to Agent Office" in ctx

    msg = _http(f"{server}/api/channels/alpha-dev/messages", "POST", tokens["ana"], {"body": "@ben ¿listo? ✓"})
    cli("supervise", "--once", "--wait", "0")  # receives it (`--once` first re-reads what the server has pending)
    pending = cli("inbox", "--pending")
    assert [m["body"] for m in pending if m["message_id"] == msg["id"]] == ["@ben ¿listo? ✓"]
    rep = cli("reply", str(msg["id"]), "sí, ñandú")
    assert rep["ok"] is True
    history = _http(f"{server}/api/channels/alpha-dev/messages?limit=10", token=tokens["ana"])
    msgs = history["messages"] if isinstance(history, dict) else history
    assert any(m.get("reply_to") == msg["id"] and m["body"] == "sí, ñandú" for m in msgs)

    dis = cli("disconnect", "--purge", *scope)
    assert dis["ok"] is True and dis["uninstall"]["removed"]
    assert "hooks" not in json.loads((repo / ".claude" / "settings.json").read_text("utf-8"))
    assert not (home / "config.json").exists()
