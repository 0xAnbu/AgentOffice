"""The GitHub webhook must stay reachable by GitHub in production without exposing anything else.

Production publishes the API only inside the tailnet (Tailscale Serve), which GitHub cannot reach. So the backend
opens a second, webhook-only loopback listener (AGENTLINK_WEBHOOK_PORT) and *only that port* goes through Tailscale
Funnel. These tests pin both halves:

- runtime: a real server process with both listeners; the public one serves POST /api/github/webhook and refuses
  every other path, method and WebSocket; the private one keeps serving the whole API;
- deployment contract: the documented Funnel command targets the webhook port, never the API port, and the env
  example, the unit and the docs agree on the ports.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
ADMIN = "al_ingress-admin-token-0123456789abcdefghijklmnop"
SECRET = "whsec-ingress-test"
API_PORT = 8080  # production values (deployment/agent-office.service, docs/DEPLOYMENT.md)
HOOK_PATH = "/api/github/webhook"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _req(
    url: str, method: str = "GET", body: bytes | None = None, headers: dict[str, str] | None = None
) -> tuple[int, Any]:
    req = urllib.request.Request(url, method=method, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if raw.startswith(b"{") else raw


def _signed(event: str, payload: dict[str, Any], delivery: str, secret: str = SECRET) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"x-github-event": event, "x-github-delivery": delivery, "x-hub-signature-256": sig}


def _spawn(tmp_path: Path, port: int, extra_env: dict[str, str]) -> subprocess.Popen[bytes]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENTLINK_", "GITHUB_"))}
    env.update(AGENTLINK_ADMIN_TOKEN=ADMIN, AGENTLINK_DB_PATH=str(tmp_path / "s.db"), AGENTLINK_RECONCILE_INTERVAL="0")
    env.update(extra_env)
    log = open(tmp_path / "server.log", "ab")  # noqa: SIM115
    return subprocess.Popen(
        [sys.executable, "-m", "agentlink.serve", "--port", str(port), "--env-file", str(tmp_path / "none.env")],
        env=env, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT,
    )  # fmt: skip


@pytest.fixture
def listeners(tmp_path: Path) -> Iterator[tuple[str, str]]:
    api, hook = _free_port(), _free_port()
    proc = _spawn(tmp_path, api, {"AGENTLINK_WEBHOOK_PORT": str(hook), "GITHUB_WEBHOOK_SECRET": SECRET})
    api_url, hook_url = f"http://127.0.0.1:{api}", f"http://127.0.0.1:{hook}"
    try:
        for _ in range(150):
            try:
                if _req(f"{api_url}/api/health")[0] == 200:
                    socket.create_connection(("127.0.0.1", hook), timeout=1).close()  # the second listener is up
                    break
            except OSError:
                assert proc.poll() is None, (tmp_path / "server.log").read_text()
            time.sleep(0.2)
        else:
            pytest.fail((tmp_path / "server.log").read_text())
        yield api_url, hook_url
    finally:
        proc.terminate()
        proc.wait(10)


def test_github_reaches_the_webhook_through_the_public_listener(listeners: tuple[str, str]) -> None:
    api, hook = listeners
    admin = {"Authorization": f"Bearer {ADMIN}", "content-type": "application/json"}
    assert (
        _req(f"{api}/api/channels", "POST", json.dumps({"name": "dev", "repo": "acme/app"}).encode(), admin)[0] == 200
    )

    ping, headers = _signed("ping", {"zen": "hi", "hook_id": 1, "repository": {"full_name": "acme/app"}}, "p-1")
    assert _req(f"{hook}{HOOK_PATH}", "POST", ping, headers)[0] == 200

    push = {
        "ref": "refs/heads/main", "after": "0123456789abcdef", "commits": [{}],
        "head_commit": {"message": "fix"}, "repository": {"full_name": "acme/app"}, "sender": {"login": "octo"},
    }  # fmt: skip
    raw, headers = _signed("push", push, "d-1")
    status, body = _req(f"{hook}{HOOK_PATH}", "POST", raw, headers)
    assert status == 200 and body["posted"][0]["channel"] == "dev"
    # same process, same database: the private API sees the event GitHub delivered through the public listener
    _, msgs = _req(f"{api}/api/channels/dev/messages", headers=admin)
    assert [m["author"] for m in msgs["messages"]][-1] == "github"

    # the public listener still enforces the signature
    assert _req(f"{hook}{HOOK_PATH}", "POST", raw, {**headers, "x-hub-signature-256": "sha256=00"})[0] == 401
    assert _req(f"{hook}{HOOK_PATH}", "POST", raw, _signed("push", push, "d-2", secret="wrong")[1])[0] == 401


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/health"),
        ("GET", "/api/me"),
        ("GET", "/api/channels"),
        ("POST", "/api/admin/agents"),
        ("POST", "/mcp"),
        ("GET", "/"),
        ("GET", "/tareas"),
        ("GET", "/static/agent-guide.md"),
        ("GET", "/api/agent-guide"),
        ("GET", HOOK_PATH),
        ("PUT", HOOK_PATH),
        ("POST", HOOK_PATH + "/"),
        ("POST", HOOK_PATH + "/../me"),
        ("POST", "/api/github/webhook%2f..%2fme"),
    ],
)
def test_public_listener_serves_nothing_but_the_webhook(listeners: tuple[str, str], method: str, path: str) -> None:
    api, hook = listeners
    auth = {"Authorization": f"Bearer {ADMIN}"}  # even a valid admin token gets nothing through the public port
    status, body = _req(f"{hook}{path}", method, b"{}" if method in ("POST", "PUT") else None, auth)
    assert status == 404, (path, status, body)
    if path == "/api/me":
        assert _req(f"{api}{path}", headers=auth)[0] == 200  # the private listener is unaffected


def test_unsigned_deliveries_are_refused_before_the_body_is_read(listeners: tuple[str, str]) -> None:
    """Declare 6 MiB but send only 1 KiB: the answer can only arrive if the server did not wait for the body."""
    import http.client

    _, hook = listeners
    for signature in (None, "sha256=short"):
        conn = http.client.HTTPConnection("127.0.0.1", int(hook.rsplit(":", 1)[1]), timeout=5)
        conn.putrequest("POST", HOOK_PATH)
        conn.putheader("x-github-event", "push")
        conn.putheader("content-length", str(6 * 1024 * 1024))
        if signature:
            conn.putheader("x-hub-signature-256", signature)
        conn.endheaders()
        conn.send(b"x" * 1024)
        assert conn.getresponse().status == 401, signature
        conn.close()


def test_public_listener_refuses_websockets(listeners: tuple[str, str]) -> None:
    from websockets.exceptions import InvalidHandshake, InvalidStatus
    from websockets.sync.client import connect

    api, hook = listeners
    admin = {"Authorization": f"Bearer {ADMIN}"}

    def ticket() -> str:
        return str(_req(f"{api}/api/ws-ticket", "POST", b"{}", admin)[1]["ticket"])

    with connect(api.replace("http", "ws") + f"/ws/events?ticket={ticket()}", open_timeout=5):
        pass  # control: a valid ticket opens the event stream on the private listener
    with pytest.raises((InvalidStatus, InvalidHandshake, OSError)):  # the same valid ticket is refused publicly
        with connect(hook.replace("http", "ws") + f"/ws/events?ticket={ticket()}", open_timeout=5):
            pass


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"AGENTLINK_WEBHOOK_PORT": "{hook}"}, "GITHUB_WEBHOOK_SECRET is empty"),
        ({"AGENTLINK_WEBHOOK_PORT": "{api}", "GITHUB_WEBHOOK_SECRET": SECRET}, "must differ"),
    ],
)
def test_public_listener_fails_closed(tmp_path: Path, env: dict[str, str], message: str) -> None:
    api, hook = _free_port(), _free_port()
    proc = _spawn(tmp_path, api, {k: v.format(api=api, hook=hook) for k, v in env.items()})
    assert proc.wait(30) != 0
    assert message in (tmp_path / "server.log").read_text()


# -- deployment contract ------------------------------------------------------------------------------------------


def _env_example() -> dict[str, str]:
    text = (ROOT / "deployment" / "agent-office.env.example").read_text("utf-8")
    return dict(re.findall(r"^#?\s*([A-Z_]+)=(\S*)", text, re.M))


def _tailscale_commands(sub: str) -> list[tuple[str, str, list[str]]]:
    """Every `tailscale <sub> ...` invocation in the docs and deployment files, as (file, command, backend targets).

    A target is any positional argument that is not a flag or a sub-command (`http://127.0.0.1:8081`, `8080`,
    `localhost:8080`, ...): the CLI accepts all of those forms, so a bare `tailscale funnel 8080` is caught too.
    """
    files = [*ROOT.glob("docs/*.md"), ROOT / "README.md", *ROOT.glob("deployment/*")]
    found = []
    for f in files:
        if not f.is_file():
            continue
        for line in f.read_text("utf-8").splitlines():
            for m in re.finditer(rf"tailscale {sub}\b([^`#\n]*)", line):
                targets = _targets(m.group(1))
                if targets is not None:
                    found.append((f.name, m.group(0).strip(), targets))
    return found


def _targets(args_text: str) -> list[str] | None:
    args = args_text.split()
    if args and args[0] in ("status", "reset", "off", "--help"):
        return None
    return [a for a in args if not a.startswith("-") and a not in ("on", "off")]


def _port(target: str) -> str:
    m = re.search(r"(\d+)(?:/.*)?$", target)
    assert m, f"cannot tell the port of {target!r}"
    return m.group(1)


def test_deployment_publishes_only_the_webhook_port() -> None:
    hook_port = _env_example().get("AGENTLINK_WEBHOOK_PORT")
    assert hook_port and hook_port.isdigit() and int(hook_port) != API_PORT, "env example must declare the webhook port"

    funnels = [c for c in _tailscale_commands("funnel") if c[2]]
    assert funnels, "DEPLOYMENT.md must document how GitHub reaches the webhook (tailscale funnel)"
    for f, cmd, targets in funnels:
        assert [_port(t) for t in targets] == [hook_port], f"{f}: Funnel may only target the webhook listener: {cmd}"
        assert all(re.match(r"(https?://)?(127\.0\.0\.1|localhost)?:?\d", t) for t in targets), f"{f}: {cmd}"

    serves = [c for c in _tailscale_commands("serve") if c[2]]
    assert serves, "DEPLOYMENT.md must document the private Serve mapping"
    for f, cmd, targets in serves:
        assert [_port(t) for t in targets] == [str(API_PORT)], f"{f}: Serve publishes the API (tailnet only): {cmd}"


def test_contract_parser_catches_a_funnelled_api() -> None:
    """The contract test above is only as good as its parser: every form of "API on Funnel" must be seen."""
    for cmd in ("tailscale funnel 8080", "tailscale funnel --bg --https=443 http://127.0.0.1:8080",
                "tailscale funnel --bg localhost:8080"):  # fmt: skip
        targets = _targets(cmd.removeprefix("tailscale funnel")) or []
        assert [_port(t) for t in targets] == [str(API_PORT)], cmd


def test_service_keeps_the_api_on_loopback() -> None:
    unit = (ROOT / "deployment" / "agent-office.service").read_text("utf-8")
    exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert f"--host 127.0.0.1 --port {API_PORT}" in exec_start
    assert "--webhook-host" not in exec_start  # the webhook listener stays on its loopback default
