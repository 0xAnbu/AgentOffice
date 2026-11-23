"""End-to-end run of the REAL `deployment/bootstrap.sh` on this machine, in a throw-away sandbox driven by
`systemd --user`: real install.sh, real venv, real systemd units, real backups, real health check timer and a real
GitHub webhook delivery over TLS. Only the external services are fakes (tests/deploy_fakes.py): the `tailscale` CLI
(with a real TLS proxy and a test CA), the Healthchecks.io API, the GitHub API and dpkg-query.

Scenario: a first run is interrupted by a Healthchecks.io outage, the second run resumes and reports SUCCESS, a third
run changes nothing, verify-live.sh passes, and no secret appears in any output, log, state or report.

Slow (it builds a venv and installs the requirements), so it runs only with AO_DEPLOY_E2E=1 (CI: deployment job).
It is NOT a test against the real Tailscale / Healthchecks.io / GitHub nor on a Raspberry Pi: see
docs/evidence/automated-deployment-validation.md."""

from __future__ import annotations

import json
import os
import random
import shutil
import socket
import stat
import subprocess
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from .deploy_fakes import REPO, FakeGitHub, FakeHealthchecks, FakeTailscale, fake_apt

UNITS = [
    "agent-office.service",
    "agent-office-healthcheck.service",
    "agent-office-healthcheck.timer",
    "agent-office-backup.service",
    "agent-office-backup.timer",
]
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
TS_KEY = "tskey-auth-kE2Evalid0CNTRL-0123456789abcdef"
HC_KEY = "hcw_e2e_rw_key_0123456789abcdef"
GH_TOKEN = "github_pat_E2E0123456789abcdefghijklmnop"
FUNNEL_PORT = 8443


def _systemd_user() -> bool:
    if not shutil.which("systemctl"):
        return False
    r = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, check=False)
    return r.returncode == 0


pytestmark = [
    pytest.mark.skipif(os.environ.get("AO_DEPLOY_E2E") != "1", reason="set AO_DEPLOY_E2E=1 (slow end-to-end)"),
    pytest.mark.skipif(not _systemd_user(), reason="needs a working `systemctl --user`"),
    pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl for the test CA"),
]


def free_port() -> int:
    for _ in range(50):
        p = random.randint(20000, 29999)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
            except OSError:
                continue
            return p
    raise RuntimeError("no free port")


def port_free(ip: str, port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind((ip, port))
        except OSError:
            return False
        return True


def systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, check=False)


class Sandbox:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.port = free_port()
        self.webhook_port = free_port()
        self.https_port = free_port()
        self.etc = root / "etc"
        self.env_file = self.etc / "agent-office.env"
        self.logs = root / "log"
        self.ts = FakeTailscale(root / "ts", valid_key=TS_KEY)
        self.hc = FakeHealthchecks(key=HC_KEY)
        self.gh = FakeGitHub(token=GH_TOKEN, resolve={self.ts.dns: "127.0.0.2"}, cafile=str(self.ts.certs["ca"]))
        fake_apt(root / "bin")
        # the real services (PyPI for install.sh) must keep working: system CAs + the test CA of the fake Tailscale
        bundle = root / "ca-bundle.pem"
        system = Path("/etc/ssl/certs/ca-certificates.crt")
        bundle.write_text((system.read_text() if system.is_file() else "") + self.ts.certs["ca"].read_text())
        osr = root / "os-release"
        osr.write_text('ID=debian\nVERSION_ID="13"\nVERSION_CODENAME=trixie\nPRETTY_NAME="Debian GNU/Linux 13"\n')
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("AO_", "FAKE_TS_"))}
        self.env.update(self.ts.env())
        self.env.update({
            "PATH": f"{self.ts.bin}:{root / 'bin'}:{os.environ['PATH']}",
            "SSL_CERT_FILE": str(bundle),
            "AO_USER_MODE": "1",
            "AO_HOME": str(root / "opt"),
            "AO_DATA": str(root / "data"),
            "AO_ETC": str(self.etc),
            "AO_BACKUP_DIR": str(root / "backups"),
            "AO_UNIT_DIR": str(UNIT_DIR),
            "AO_ENV_FILE": str(self.env_file),
            "AO_LOG_DIR": str(self.logs),
            "AO_PORT": str(self.port),
            "AO_URL": f"http://127.0.0.1:{self.port}",
            "AO_OS_RELEASE": str(osr),
            "AO_GITHUB_API_URL": self.gh.url,
            "AO_HTTP_BACKOFF": "0.01",
        })  # fmt: skip
        self.config = root / "deployment.toml"
        self.config.write_text(f"""\
[deployment]
mode = "production"
hostname = "agentoffice"
[application]
port = {self.port}
[network]
tailnet = "example.com"
https_port = {self.https_port}
[monitoring]
api_url = "{self.hc.url}"
[github_webhook]
enabled = true
port = {self.webhook_port}
funnel_port = {FUNNEL_PORT}
[[github_webhook.repositories]]
repo = "o/r"
""")
        self.secrets = root / "bootstrap-secrets.json"
        self.secrets.write_text(json.dumps({"tailscale_auth_key": TS_KEY, "healthchecks_api_key": HC_KEY,
                                            "github_token": GH_TOKEN}))  # fmt: skip
        self.secrets.chmod(0o600)
        self.outputs: list[str] = []

    def run(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        r = subprocess.run(["bash", str(REPO / "deployment" / script), *args], capture_output=True, text=True,
                           env=self.env, cwd=self.root, timeout=1800, check=False)  # fmt: skip
        self.outputs += [r.stdout, r.stderr]
        return r

    def bootstrap(self) -> subprocess.CompletedProcess[str]:
        return self.run("bootstrap.sh", "--config", str(self.config), "--secrets-file", str(self.secrets),
                        "--non-interactive")  # fmt: skip

    def env_value(self, key: str) -> str:
        for line in self.env_file.read_text().splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1]
        return ""

    def state(self) -> dict[str, Any]:
        return json.loads((self.etc / "bootstrap-state.json").read_text())

    def cleanup(self) -> None:
        systemctl("disable", "--now", *UNITS)
        for u in UNITS:
            (UNIT_DIR / u).unlink(missing_ok=True)
        systemctl("daemon-reload")
        self.ts.stop_proxies()
        self.hc.close()
        self.gh.server.shutdown()


@pytest.fixture
def box(tmp_path: Path) -> Iterator[Sandbox]:
    existing = [u for u in UNITS if (UNIT_DIR / u).exists()]
    if existing:
        pytest.skip(f"refusing to touch existing user units: {existing}")
    if not port_free("127.0.0.2", FUNNEL_PORT):
        pytest.skip(f"127.0.0.2:{FUNNEL_PORT} is busy")
    sb = Sandbox(tmp_path)
    try:
        yield sb
    finally:
        sb.cleanup()


def fmt(r: subprocess.CompletedProcess[str]) -> str:
    return f"exit {r.returncode}\n--- stdout\n{r.stdout[-6000:]}\n--- stderr\n{r.stderr[-6000:]}"


def api(sb: Sandbox, token: str, method: str, path: str, body: Any = None) -> Any:
    req = urllib.request.Request(f"http://127.0.0.1:{sb.port}{path}", method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})  # fmt: skip
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def bg_calls(sb: Sandbox) -> int:
    return sum(1 for c in sb.ts.calls("serve") + sb.ts.calls("funnel") if "--bg" in c)


def test_bootstrap_end_to_end(box: Sandbox) -> None:
    sb = box

    # run 0: Healthchecks.io fails mid-way -> FAILED at the monitoring stage, state records it, nothing leaks
    sb.hc.mode = "post_500"
    r0 = sb.bootstrap()
    assert r0.returncode != 0, fmt(r0)
    assert "monitoring" in r0.stderr + r0.stdout, fmt(r0)
    st = sb.state()
    assert st["stages"]["install"]["status"] == "done"
    assert st["stages"]["tailscale"]["status"] == "done"
    assert st["stages"]["monitoring"]["status"] == "failed"
    release_0 = (Path(sb.env["AO_HOME"]) / "current").resolve()
    admin = sb.env_value("AGENTLINK_ADMIN_TOKEN")
    assert admin.startswith("al_")
    assert len(sb.ts.calls("up")) == 1
    assert sb.hc.checks == []  # every create attempt failed (and was retried a bounded number of times)
    creates_0 = sb.hc.creates()

    # run 1: the outage is over -> resumes (no reinstall, no re-registration) and reports SUCCESS
    sb.hc.mode = "ok"
    r1 = sb.bootstrap()
    assert r1.returncode == 0, fmt(r1)
    assert "AGENTOFFICE DEPLOYMENT REPORT" in r1.stdout
    assert "SUCCESS" in r1.stdout, fmt(r1)
    assert "already installed and healthy" in (sb.logs / "bootstrap.log").read_text()
    assert (Path(sb.env["AO_HOME"]) / "current").resolve() == release_0
    assert sb.env_value("AGENTLINK_ADMIN_TOKEN") == admin
    assert len(sb.ts.calls("up")) == 1
    assert sb.hc.creates() - creates_0 == 1 and len(sb.hc.checks) == 1
    assert sb.hc.pings >= 1  # the real health check unit sent the heartbeat
    hooks = sb.gh.hooks["o/r"]
    assert len(hooks) == 1 and hooks[0]["last_response"]["code"] == 200, hooks[0][
        "last_response"
    ]  # real signed delivery through the Funnel
    assert systemctl("is-active", "agent-office.service").stdout.strip() == "active"
    assert list(Path(sb.env["AO_BACKUP_DIR"]).glob("*.db.gz"))

    # data written between runs must survive a re-run (same database file, same rows)
    api(sb, admin, "POST", "/api/channels", {"name": "e2e"})
    api(sb, admin, "POST", "/api/channels/e2e/messages", {"body": "kept across bootstrap runs"})
    db = next(Path(sb.env["AO_DATA"]).glob("*.db"))
    db_inode = db.stat().st_ino

    # run 2: idempotent -> no writes anywhere
    counts = (sb.hc.creates(), sb.hc.updates(), len(sb.ts.calls("up")), bg_calls(sb), len(hooks),
              sum(1 for m, *_ in sb.gh.requests if m in ("POST", "PATCH")))  # fmt: skip
    r2 = sb.bootstrap()
    assert r2.returncode == 0, fmt(r2)
    assert "SUCCESS" in r2.stdout
    assert counts == (sb.hc.creates(), sb.hc.updates(), len(sb.ts.calls("up")), bg_calls(sb), len(hooks),
                      sum(1 for m, *_ in sb.gh.requests if m in ("POST", "PATCH")))  # fmt: skip
    assert (Path(sb.env["AO_HOME"]) / "current").resolve() == release_0
    assert sb.env_value("AGENTLINK_ADMIN_TOKEN") == admin

    assert db.stat().st_ino == db_inode
    msgs = api(sb, admin, "GET", "/api/channels/e2e/messages?limit=10")["messages"]
    assert [m["body"] for m in msgs] == ["kept across bootstrap runs"]
    assert systemctl("is-active", "agent-office.service").stdout.strip() == "active"

    # stand-alone live verification
    rv = sb.run("verify-live.sh", "--config", str(sb.config), "--secrets-file", str(sb.secrets))
    assert rv.returncode == 0, fmt(rv)

    # the original secrets file is untouched and still private
    assert stat.S_IMODE(sb.secrets.stat().st_mode) == 0o600
    assert json.loads(sb.secrets.read_text())["tailscale_auth_key"] == TS_KEY

    # no secret anywhere: outputs, logs, state, report; the Healthchecks API key is never stored
    secrets = {
        "tailscale auth key": TS_KEY,
        "healthchecks api key": HC_KEY,
        "github token": GH_TOKEN,
        "admin token": admin,
        "ping url": sb.env_value("HC_PING_URL"),
        "webhook secret": sb.env_value("GITHUB_WEBHOOK_SECRET"),
    }
    assert all(secrets.values())
    files = [*sb.logs.rglob("*"), sb.etc / "bootstrap-state.json", sb.etc / "bootstrap-report.txt"]
    texts = {f"output {i}": t for i, t in enumerate(sb.outputs)}
    texts.update({str(f): f.read_text(errors="replace") for f in files if f.is_file()})
    for where, text in texts.items():
        for name, value in secrets.items():
            assert value not in text, f"{name} leaked in {where}"
    assert HC_KEY not in sb.env_file.read_text()
    assert TS_KEY not in sb.env_file.read_text()
    assert GH_TOKEN not in sb.env_file.read_text()
