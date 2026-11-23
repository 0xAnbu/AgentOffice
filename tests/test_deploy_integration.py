"""Simulated integration tests of the bootstrap stages: the real provision.py code against a fake `tailscale` CLI
(with a real TLS proxy), a fake Healthchecks.io API, a fake GitHub API and fake apt. Plus the CLI behaviour of the
entry points (missing config, insecure secrets file, permission error, interruption).

These prove the bootstrap's logic and its handling of each external state. They do not prove the real services
behave like the fakes: that is the NOT_VERIFIED part of docs/evidence/automated-deployment-*.md."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from .deploy_fakes import REPO, FakeGitHub, FakeHealthchecks, FakeTailscale, fake_apt, load_provision

P = load_provision()
TS_KEY = "tskey-auth-kFAKEvalid0CNTRL-abcdef"
DNS = "agentoffice.tail1234.ts.net"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AO_HTTP_BACKOFF", "0.01")
    monkeypatch.setenv("AO_USER_MODE", "1")


def config(**sections: dict[str, Any]) -> dict[str, dict[str, Any]]:
    lines = []
    for sec, kv in sections.items():
        lines.append(f"[{sec}]")
        lines += [f"{k} = {json.dumps(v)}" for k, v in kv.items()]
    return P.parse_config("\n".join(lines))


def make_ctx(tmp: Path, cfg: dict[str, dict[str, Any]] | None = None, secrets: dict[str, str] | None = None,
             dry_run: bool = False) -> Any:  # fmt: skip
    etc = tmp / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    envf = etc / "agent-office.env"
    if not envf.exists():
        envf.write_text("AGENTLINK_ADMIN_TOKEN=al_integration_admin_token_0123456789\nHC_PING_URL=\n")
        envf.chmod(0o640)
    paths = P.Paths.from_env({"AO_ETC": str(etc), "AO_HOME": str(tmp / "home"), "AO_USER_MODE": "1",
                              "AO_LOG_DIR": str(tmp / "log")})  # fmt: skip
    plat = P.detect_platform({"ID": "debian", "VERSION_ID": "13", "VERSION_CODENAME": "trixie"}, "x86_64")
    return P.Ctx(
        cfg or config(), None, dict(secrets or {}), paths, plat, P.State(paths.state_file, True), dry_run=dry_run
    )


# -- Tailscale ------------------------------------------------------------------------------------------------------


@pytest.fixture
def ts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    fake = FakeTailscale(tmp_path / "ts")
    monkeypatch.setenv("PATH", f"{fake.bin}:{os.environ['PATH']}")
    for k, v in fake.env().items():
        monkeypatch.setenv(k, v)
    yield fake
    fake.stop_proxies()


def test_tailscale_not_installed_is_planned_in_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(P, "has_apt", lambda: True)
    ctx = make_ctx(tmp_path, dry_run=True)
    P.stage_tailscale(ctx)
    assert any("install Tailscale from the official apt repository" in s for s in ctx.plan)
    assert any("register this device" in s for s in ctx.plan)
    assert P.tailscale_state(ctx).kind == "absent"


def test_tailscale_daemon_not_running_is_detected(ts: Any, tmp_path: Path) -> None:
    ts.update(daemon=False)
    assert P.tailscale_state(make_ctx(tmp_path)).kind == "no_daemon"


def test_disconnected_device_is_registered_with_the_key_from_a_file(ts: Any, tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, secrets={"tailscale_auth_key": TS_KEY})
    P.stage_tailscale(ctx)
    st = ts.read()
    assert st["backend"] == "Running" and st["tags"] == ["tag:agentoffice"] and st["key_was_file"]
    (up,) = ts.calls("up")
    assert all(TS_KEY not in a for a in up), "the auth key must never be in argv"
    assert "--advertise-tags=tag:agentoffice" in up and "--hostname=agentoffice" in up
    assert not Path(st["key_file"]).exists() and not Path(st["key_file"]).parent.exists(), "temp key file removed"
    assert ctx.ts.dns_name == DNS
    saved = json.loads(ctx.paths.state_file.read_text())
    assert saved["tailscale"]["dns_name"] == DNS and TS_KEY not in ctx.paths.state_file.read_text()


def test_connected_device_keeps_its_identity(ts: Any, tmp_path: Path) -> None:
    ts.update(backend="Running", tags=["tag:other"])
    ctx = make_ctx(tmp_path, secrets={"tailscale_auth_key": TS_KEY})
    P.stage_tailscale(ctx)
    assert ts.calls("up") == [] and ctx.ts.tags == ["tag:other"]


def test_connected_device_needs_no_key(ts: Any, tmp_path: Path) -> None:
    ts.update(backend="Running")
    ctx = make_ctx(tmp_path)
    P.stage_tailscale(ctx)
    assert ts.calls("up") == []


def test_wrong_tailnet_is_refused_without_changes(ts: Any, tmp_path: Path) -> None:
    ts.update(backend="Running", tailnet="someone-else.org")
    ctx = make_ctx(tmp_path, config(network={"tailnet": "example.com"}), {"tailscale_auth_key": TS_KEY})
    with pytest.raises(P.DeployError, match=r"belongs to tailnet 'someone-else\.org'"):
        P.stage_tailscale(ctx)
    assert ts.calls("up") == []


def test_expected_tailnet_matches_by_name_or_magicdns_suffix(ts: Any, tmp_path: Path) -> None:
    ts.update(backend="Running")
    for tn in ("example.com", "tail1234.ts.net"):
        P.stage_tailscale(make_ctx(tmp_path, config(network={"tailnet": tn})))


@pytest.mark.parametrize(
    ("setup", "needle"),
    [
        ({"valid_key": "tskey-auth-kOTHER-1"}, "rejected the auth key"),
        ({"up": "bad_tag"}, "refused the tag tag:agentoffice"),
        ({"up": "needs_approval"}, "must be approved"),
        ({"up": "timeout"}, "timed out"),
    ],
)
def test_registration_failures_are_diagnosed(ts: Any, tmp_path: Path, setup: dict[str, Any], needle: str) -> None:
    ts.update(**setup)
    with pytest.raises(P.DeployError) as e:
        P.stage_tailscale(make_ctx(tmp_path, secrets={"tailscale_auth_key": TS_KEY}))
    assert needle in str(e.value) and TS_KEY not in str(e.value) + e.value.hint


def test_device_pending_approval_and_stopped_are_not_overridden(ts: Any, tmp_path: Path) -> None:
    for backend, needle in (("NeedsMachineAuth", "waiting for approval"), ("Stopped", "stopped")):
        ts.update(backend=backend)
        with pytest.raises(P.DeployError, match=needle):
            P.stage_tailscale(make_ctx(tmp_path, secrets={"tailscale_auth_key": TS_KEY}))
    assert ts.calls("up") == []


def test_missing_key_for_an_unregistered_device(ts: Any, tmp_path: Path) -> None:
    with pytest.raises(P.DeployError, match="no tailscale_auth_key"):
        P.stage_tailscale(make_ctx(tmp_path))


def test_dry_run_never_registers(ts: Any, tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, secrets={"tailscale_auth_key": TS_KEY}, dry_run=True)
    P.stage_tailscale(ctx)
    assert ts.calls("up") == [] and any("tailscale up --auth-key=file:<private temp file>" in s for s in ctx.plan)
    assert all(TS_KEY not in s for s in ctx.plan)


# -- Serve / HTTPS --------------------------------------------------------------------------------------------------


class Backend:
    """Stand-in for agent-office on loopback (only for the proxy/TLS tests; the e2e test uses the real one)."""

    def __init__(self) -> None:
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:
                ok = self.path == "/api/health" or self.headers.get("Authorization") == "Bearer al_good"
                body = json.dumps({"ok": True, "role": "admin"} if ok else {"error": "unauthorized"}).encode()
                self.send_response(200 if ok else 401)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


def https_ctx(tmp_path: Path, ts: Any, app_port: int, https_port: int = 18443) -> Any:
    ts.update(backend="Running")
    ctx = make_ctx(tmp_path, config(application={"port": app_port}, network={"https_port": https_port}))
    P.stage_tailscale(ctx)
    return ctx


def test_serve_absent_is_configured_privately_and_verified_through_tls(ts: Any, tmp_path: Path) -> None:
    be = Backend()
    try:
        ctx = https_ctx(tmp_path, ts, be.port)
        P.stage_https(ctx)
        (serve,) = [c for c in ts.calls("serve") if "--bg" in c]
        assert serve == ["serve", "--bg", "--https=18443", f"http://127.0.0.1:{be.port}"]
        assert ts.calls("funnel") == [] and not ts.read()["serve"].get("AllowFunnel")
        status, detail = P.tls_info("127.0.0.2", DNS, 18443)
        assert status == P.PASS and "AgentOffice Test CA" in detail
        assert P.https_call("127.0.0.2", DNS, 18443, "GET", "/api/health")[0] == 200
        assert P.https_call("127.0.0.2", DNS, 18443, "GET", "/api/me")[0] == 401
        assert P.https_call("127.0.0.2", DNS, 18443, "GET", "/api/me", "al_good")[0] == 200
        # second run: already ours, nothing re-applied
        P.stage_https(ctx)
        assert len([c for c in ts.calls("serve") if "--bg" in c]) == 1  # only re-read, never re-applied
    finally:
        be.close()


def test_tls_verification_is_never_disabled(ts: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    be = Backend()
    try:
        ctx = https_ctx(tmp_path, ts, be.port, 18444)
        P.stage_https(ctx)
        status, detail = P.tls_info("127.0.0.2", "wrong-name.tail1234.ts.net", 18444)
        assert status == P.FAIL and "NOT valid" in detail
        monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "nonexistent.pem"))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "nonexistent"))
        assert P.tls_info("127.0.0.2", DNS, 18444)[0] == P.FAIL  # untrusted CA -> FAIL, no fallback to -k
    finally:
        be.close()


def serve_cfg(port: int, proxy: str, funnel: bool = False) -> dict[str, Any]:
    hp = f"{DNS}:{port}"
    s: dict[str, Any] = {"TCP": {str(port): {"HTTPS": True}}, "Web": {hp: {"Handlers": {"/": {"Proxy": proxy}}}}}
    if funnel:
        s["AllowFunnel"] = {hp: True}
    return s


def test_serve_already_configured_is_left_alone(ts: Any, tmp_path: Path) -> None:
    ctx = https_ctx(tmp_path, ts, 8080)
    ts.update(serve=serve_cfg(18443, "http://127.0.0.1:8080"))
    P.stage_https(ctx)
    assert [c for c in ts.calls("serve") if "--bg" in c] == []


def test_incompatible_serve_config_is_not_overwritten(ts: Any, tmp_path: Path) -> None:
    ctx = https_ctx(tmp_path, ts, 8080)
    before = serve_cfg(18443, "http://127.0.0.1:3000")
    ts.update(serve=before)
    with pytest.raises(P.DeployError, match="conflict"):
        P.stage_https(ctx)
    assert ts.read()["serve"] == before and [c for c in ts.calls("serve") if "--bg" in c] == []


def test_api_on_funnel_is_a_security_failure(ts: Any, tmp_path: Path) -> None:
    ctx = https_ctx(tmp_path, ts, 8080)
    ts.update(serve=serve_cfg(18443, "http://127.0.0.1:8080", funnel=True))
    with pytest.raises(P.DeployError, match="SECURITY") as e:
        P.stage_https(ctx)
    assert "not changed for you" in e.value.hint


@pytest.mark.parametrize(
    ("flags", "needle"), [({"magicdns": False}, "MagicDNS"), ({"https": False}, "HTTPS certificates")]
)
def test_tailnet_prerequisites(ts: Any, tmp_path: Path, flags: dict[str, Any], needle: str) -> None:
    ts.update(**flags)
    ctx = https_ctx(tmp_path, ts, 8080)
    with pytest.raises(P.DeployError, match=needle):
        P.stage_https(ctx)


def test_serve_failure_is_reported(ts: Any, tmp_path: Path) -> None:
    ctx = https_ctx(tmp_path, ts, 8080)
    ts.update(serve_fails="serve config denied")
    with pytest.raises(P.DeployError, match="serve config denied"):
        P.stage_https(ctx)


# -- Healthchecks.io ------------------------------------------------------------------------------------------------


@pytest.fixture
def hc() -> Any:
    fake = FakeHealthchecks()
    yield fake
    fake.close()


def hc_ctx(tmp_path: Path, hc: Any, key: str | None = None, **mon: Any) -> Any:
    secrets = {"healthchecks_api_key": key or hc.key}
    return make_ctx(tmp_path, config(monitoring={"api_url": hc.url, **mon}), secrets)


def test_check_is_created_when_none_exists(hc: Any, tmp_path: Path) -> None:
    ctx = hc_ctx(tmp_path, hc)
    P.provision_monitor(ctx)
    (c,) = hc.checks
    assert c["name"] == "agentoffice" and c["timeout"] == 300 and c["grace"] == 600
    assert set(c["channels"].split(",")) == {ch["id"] for ch in hc.channels}
    body = next(b for m, p, b in hc.requests if m == "POST")
    assert body["unique"] == ["name"]
    env = ctx.paths.env_file.read_text()
    assert f"HC_PING_URL={c['ping_url']}" in env and hc.key not in env
    assert stat.S_IMODE(ctx.paths.env_file.stat().st_mode) == 0o640
    assert hc.key not in ctx.paths.state_file.read_text() and c["ping_url"] not in ctx.paths.state_file.read_text()


def test_existing_managed_check_is_reused_without_writes(hc: Any, tmp_path: Path) -> None:
    P.provision_monitor(hc_ctx(tmp_path, hc))
    n = len(hc.requests)
    P.provision_monitor(hc_ctx(tmp_path, hc))
    writes = [r for r in hc.requests[n:] if r[0] == "POST"]
    assert writes == [] and len(hc.checks) == 1


def test_outdated_check_is_updated_in_place(hc: Any, tmp_path: Path) -> None:
    P.provision_monitor(hc_ctx(tmp_path, hc))
    hc.checks[0]["grace"] = 3600
    hc.checks[0]["channels"] = hc.channels[0]["id"]
    P.provision_monitor(hc_ctx(tmp_path, hc))
    assert hc.updates() == 1 and hc.creates() == 1 and len(hc.checks) == 1
    assert hc.checks[0]["grace"] == 600 and len(hc.checks[0]["channels"].split(",")) == 2


def test_foreign_check_with_the_same_name_is_not_touched(hc: Any, tmp_path: Path) -> None:
    hc.add_check("agentoffice", desc="somebody's check", channels=hc.channels[0]["id"])
    with pytest.raises(P.DeployError, match="not created by this bootstrap"):
        P.provision_monitor(hc_ctx(tmp_path, hc))
    assert hc.updates() == 0 and hc.creates() == 0
    P.provision_monitor(hc_ctx(tmp_path, hc, adopt_existing=True))
    assert hc.updates() == 1 and P.MANAGED_MARKER in hc.checks[0]["desc"]


def test_duplicate_checks_are_refused(hc: Any, tmp_path: Path) -> None:
    hc.add_check("agentoffice")
    hc.add_check("agentoffice")
    with pytest.raises(P.DeployError, match=r"2 Healthchecks\.io checks are named"):
        P.provision_monitor(hc_ctx(tmp_path, hc))


def test_selected_integrations_only(hc: Any, tmp_path: Path) -> None:
    P.provision_monitor(hc_ctx(tmp_path, hc, channels="ops-signal"))
    assert hc.checks[0]["channels"] == hc.channels[1]["id"]
    with pytest.raises(P.DeployError, match="not found"):
        P.provision_monitor(hc_ctx(tmp_path / "x", hc, channels="pager"))


@pytest.mark.parametrize(
    ("mode", "needle"),
    [
        ("no_channels", "no integrations"),
        ("unavailable", "unavailable after 3 attempts"),
        ("incomplete", "no usable ping_url"),
        ("post_500", "unavailable after 3 attempts"),
    ],
)
def test_healthchecks_failure_modes(hc: Any, tmp_path: Path, mode: str, needle: str) -> None:
    hc.mode = mode
    ctx = hc_ctx(tmp_path, hc)
    with pytest.raises(P.DeployError, match=needle) as e:
        P.provision_monitor(ctx)
    assert hc.key not in str(e.value) and P.env_get(ctx.paths.env_file, "HC_PING_URL") == ""


def test_invalid_key_is_not_retried(hc: Any, tmp_path: Path) -> None:
    with pytest.raises(P.DeployError, match="rejected the API key") as e:
        P.provision_monitor(hc_ctx(tmp_path, hc, key="hcw_wrong_key_000000000"))
    assert len(hc.requests) == 1 and "read-write API key" in e.value.hint
    assert "hcw_wrong_key_000000000" not in str(e.value)


def test_rate_limit_is_retried(hc: Any, tmp_path: Path) -> None:
    hc.mode = "rate_limit"
    P.provision_monitor(hc_ctx(tmp_path, hc))
    assert hc.rate_limited == 2 and len(hc.checks) == 1


def test_no_key_keeps_an_existing_ping_url(hc: Any, tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, config(monitoring={"api_url": hc.url}))
    P.env_set(ctx.paths.env_file, "HC_PING_URL", "https://hc-ping.com/existing")
    P.provision_monitor(ctx)
    assert hc.requests == []
    with pytest.raises(P.DeployError, match="no healthchecks_api_key"):
        P.provision_monitor(make_ctx(tmp_path / "fresh", config(monitoring={"api_url": hc.url})))


def test_monitor_dry_run_makes_no_request(hc: Any, tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, config(monitoring={"api_url": hc.url}), {"healthchecks_api_key": hc.key}, dry_run=True)
    P.provision_monitor(ctx)
    assert hc.requests == [] and any("look up check" in s for s in ctx.plan)


# -- GitHub webhook registration ------------------------------------------------------------------------------------


def test_hook_is_created_once_and_updated_when_the_secret_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                  ts: Any) -> None:  # fmt: skip
    gh = FakeGitHub()
    monkeypatch.setenv("AO_GITHUB_API_URL", gh.url)
    try:
        ts.update(backend="Running")
        ctx = make_ctx(tmp_path, config(github_webhook={"enabled": True}))
        P.stage_tailscale(ctx)
        hid = P.ensure_hook(ctx, gh.token, "o/r", "s" * 64, new_secret=True)
        (h,) = gh.hooks["o/r"]
        assert str(h["id"]) == hid and h["config"]["url"] == f"https://{DNS}:8443/api/github/webhook"
        assert h["config"]["insecure_ssl"] == "0" and set(h["events"]) == set(P.WEBHOOK_EVENTS)
        assert P.ensure_hook(ctx, gh.token, "o/r", "s" * 64, new_secret=False) == hid
        assert [m for m, *_ in gh.requests].count("PATCH") == 0 and len(gh.hooks["o/r"]) == 1
        P.ensure_hook(ctx, gh.token, "o/r", "t" * 64, new_secret=True)
        assert [m for m, *_ in gh.requests].count("PATCH") == 1 and gh.hooks["o/r"][0]["config"]["secret"] == "t" * 64
        with pytest.raises(P.DeployError, match="refused to list the webhooks") as e:
            P.ensure_hook(ctx, "bad-token", "o/r", "s" * 64, new_secret=False)
        assert "Webhooks: read and write" in e.value.hint
    finally:
        gh.close()


# -- dependencies ---------------------------------------------------------------------------------------------------


def test_missing_packages_are_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = fake_apt(tmp_path / "bin", missing=("gnupg", "iproute2"))
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    P.stage_dependencies(make_ctx(tmp_path))
    calls = log.read_text().splitlines()
    assert calls[0].startswith("update") and calls[1].endswith("gnupg iproute2")


def test_dependency_installation_error_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_apt(tmp_path / "bin", missing=("gnupg",), apt_fails="Unable to locate package gnupg")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    with pytest.raises(P.DeployError, match=r"apt-get update failed .*Unable to locate package"):
        P.stage_dependencies(make_ctx(tmp_path))


def test_present_packages_are_not_reinstalled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = fake_apt(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    P.stage_dependencies(make_ctx(tmp_path))
    assert not log.exists()


def test_without_apt_present_commands_are_enough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(P, "has_apt", lambda: False)
    P.stage_dependencies(make_ctx(tmp_path))  # git, curl, ip, python3 exist on the test machine


def test_without_apt_missing_commands_are_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(P, "has_apt", lambda: False)
    monkeypatch.setattr(P, "missing_commands", lambda: ["git"])
    with pytest.raises(P.DeployError, match="required commands not found: git") as e:
        P.stage_dependencies(make_ctx(tmp_path))
    assert "package manager" in e.value.hint


def untested_ctx(tmp_path: Path, release: dict[str, str], **kw: Any) -> Any:
    ctx = make_ctx(tmp_path)
    ctx.platform = P.detect_platform(release, "x86_64", **kw)
    return ctx


def test_untested_distribution_is_a_warning_not_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(P, "reach", lambda host, port=443, timeout=5: None)
    no_tailscale(monkeypatch)
    monkeypatch.setattr(P, "has_apt", lambda: True)
    ctx = untested_ctx(tmp_path, {"ID": "linuxmint", "ID_LIKE": "ubuntu debian", "UBUNTU_CODENAME": "noble"})
    rows = {r[0]: r for r in P.precheck(ctx)}
    assert rows["Platform"][1] == "WARN" and "not a tested distribution" in rows["Platform"][2]
    # unattended: the Ubuntu repository of the parent distribution is used
    assert rows["Tailscale install"][1] == P.PASS and "apt repository" in rows["Tailscale install"][2]


def test_no_tailscale_repository_requires_a_manual_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(P, "reach", lambda host, port=443, timeout=5: None)
    real_which = shutil.which
    monkeypatch.setattr(P.shutil, "which", lambda c, *a, **k: None if c == "tailscale" else real_which(c, *a, **k))
    ctx = untested_ctx(tmp_path, {"ID": "fedora", "VERSION_ID": "43"})
    rows = {r[0]: r for r in P.precheck(ctx)}
    assert rows["Tailscale install"][1] == P.FAIL and P.TAILSCALE_LINUX_DOC in rows["Tailscale install"][2]
    with pytest.raises(P.DeployError, match="no official Tailscale apt repository") as e:
        P.install_tailscale(ctx)
    assert P.TAILSCALE_LINUX_DOC in e.value.hint


def test_non_glibc_without_python_is_explained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(P, "find_python", lambda ctx: None)
    ctx = untested_ctx(tmp_path, {"ID": "alpine", "VERSION_ID": "3.22"}, libc="other")
    with pytest.raises(P.DeployError, match="uv build needs glibc"):
        P.stage_python(ctx)


# -- missing Tailscale: ask, then run the official install script ---------------------------------------------------


def no_tailscale(monkeypatch: pytest.MonkeyPatch) -> None:
    real_which = shutil.which
    monkeypatch.setattr(P.shutil, "which", lambda c, *a, **k: None if c == "tailscale" else real_which(c, *a, **k))


def in_terminal(monkeypatch: pytest.MonkeyPatch, *answers: str) -> list[str]:
    """Pretend stdin is a terminal and feed the answers; returns the prompts shown."""
    shown: list[str] = []
    queue = list(answers)
    monkeypatch.setattr(P.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": shown.append(prompt) or queue.pop(0))
    return shown


def test_missing_tailscale_asks_and_runs_the_official_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    no_tailscale(monkeypatch)
    ran: list[str] = []
    monkeypatch.setattr(P, "install_tailscale_script", lambda ctx: ran.append("script") or (_ for _ in ()).throw(
        P.DeployError("stop after the install")))  # fmt: skip
    shown = in_terminal(monkeypatch, "y")
    ctx = make_ctx(tmp_path)
    ctx.interactive = True
    with pytest.raises(P.DeployError, match="stop after the install"):
        P.stage_tailscale(ctx)
    assert ran == ["script"] and P.TAILSCALE_INSTALL_CMD in shown[0]


def test_declining_the_tailscale_install_fails_with_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_tailscale(monkeypatch)
    monkeypatch.setattr(P, "install_tailscale_script", lambda ctx: pytest.fail("must not install"))
    in_terminal(monkeypatch, "n")
    ctx = make_ctx(tmp_path)
    ctx.interactive = True
    with pytest.raises(P.DeployError, match="Tailscale is required") as e:
        P.stage_tailscale(ctx)
    assert P.TAILSCALE_INSTALL_CMD in e.value.hint


def test_precheck_reports_how_tailscale_will_be_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    no_tailscale(monkeypatch)
    monkeypatch.setattr(P, "reach", lambda host, port=443, timeout=5: None)
    ctx = untested_ctx(tmp_path, {"ID": "fedora", "VERSION_ID": "43"})
    assert {r[0]: r for r in P.precheck(ctx)}["Tailscale install"][1] == P.FAIL  # unattended, no repository
    ctx.tailscale_script = True
    rows = {r[0]: r for r in P.precheck(ctx)}
    assert rows["Tailscale install"][1] == P.PASS and P.TAILSCALE_INSTALL_CMD in rows["Tailscale install"][2]
    assert rows["Network: Tailscale install script"][2] == "tailscale.com: reachable"
    ctx.tailscale_script, ctx.interactive = False, True
    monkeypatch.setattr(P.sys.stdin, "isatty", lambda: True)
    assert {r[0]: r for r in P.precheck(ctx)}["Tailscale install"][1] == "WARN"
    with pytest.raises(P.DeployError, match="no official Tailscale apt repository"):
        ctx.interactive = False
        P.stage_tailscale(ctx)


class ScriptServer(BaseHTTPRequestHandler):
    body = b""

    def do_GET(self) -> None:
        if self.path != "/install.sh":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a: Any) -> None:
        pass


@pytest.fixture
def script_server() -> Any:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ScriptServer)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_install_script_is_piped_to_sh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script_server: str) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ScriptServer.body = (
        f"printf '#!/bin/sh\\necho fake\\n' > {bindir}/tailscale && chmod +x {bindir}/tailscale\n".encode()
    )
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("AO_TAILSCALE_INSTALL_URL", f"{script_server}/install.sh")
    P.install_tailscale_script(make_ctx(tmp_path))
    assert (bindir / "tailscale").is_file()


def test_failed_script_download_installs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script_server: str
) -> None:
    monkeypatch.setenv("AO_TAILSCALE_INSTALL_URL", f"{script_server}/missing.sh")
    with pytest.raises(P.DeployError, match="Tailscale install script failed"):
        P.install_tailscale_script(make_ctx(tmp_path))


def test_wizard_stops_when_production_tailscale_install_is_declined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    no_tailscale(monkeypatch)
    monkeypatch.setenv("AO_ETC", str(tmp_path / "etc"))
    in_terminal(monkeypatch, "1", "", "", "n")  # production, default hostname and port, do not install Tailscale
    out = tmp_path / "deployment.toml"
    assert P.cmd_wizard(argparse.Namespace(config=str(out))) == 1
    assert not out.exists() and "Nothing written" in capsys.readouterr().out


# -- uv download integrity -------------------------------------------------------------------------------------------


def test_uv_with_a_wrong_checksum_is_not_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import tarfile

    name = "uv-x86_64-unknown-linux-gnu"
    srv = tmp_path / "srv" / P.UV_VERSION
    srv.mkdir(parents=True)
    with tarfile.open(srv / f"{name}.tar.gz", "w:gz") as tf:
        data = b"#!/bin/sh\necho evil\n"
        info = tarfile.TarInfo(f"{name}/uv")
        info.size, info.mode = len(data), 0o755
        tf.addfile(info, io.BytesIO(data))
    import functools
    from http.server import SimpleHTTPRequestHandler

    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(tmp_path / "srv"))
    handler.log_message = lambda *a: None  # type: ignore[attr-defined]
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("AO_UV_DOWNLOAD_URL", f"http://127.0.0.1:{httpd.server_address[1]}")
        ctx = make_ctx(tmp_path)
        with pytest.raises(P.DeployError, match="checksum mismatch"):
            P.install_uv(ctx)
        assert not (ctx.paths.home / "uv").exists()
    finally:
        httpd.shutdown()
        httpd.server_close()


# -- CLI entry points -----------------------------------------------------------------------------------------------


def cli(*args: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    e = {k: v for k, v in os.environ.items() if not k.startswith("AO_")}
    e.update(env or {})
    return subprocess.run(["bash", str(REPO / "deployment" / args[0]), *args[1:]], capture_output=True, text=True,
                          env=e, cwd=cwd, timeout=120, check=False)  # fmt: skip


def test_help_of_every_entry_point() -> None:
    for script in ("bootstrap.sh", "deploy.sh", "preflight.sh", "provision-python.sh", "provision-tailscale.sh",
                   "provision-monitor.sh", "verify-live.sh"):  # fmt: skip
        r = cli(script, "--help")
        assert r.returncode == 0 and "usage" in r.stdout.lower(), (script, r.stderr)
    r = cli("install.sh", "--help")  # never mistaken for a git ref, works without root
    assert r.returncode == 0 and "--wizard" in r.stdout, r.stderr
    assert cli("install.sh", "--bogus").returncode == 2


def test_missing_config_is_a_clear_error(tmp_path: Path) -> None:
    r = cli("bootstrap.sh", "--check", env={"AO_ETC": str(tmp_path)}, cwd=tmp_path)
    assert r.returncode == 1 and "no deployment.toml found" in r.stderr and "HINT" in r.stderr
    r = cli("bootstrap.sh", "--check", "--config", str(tmp_path / "missing.toml"), cwd=tmp_path)
    assert r.returncode == 1 and "config file not found" in r.stderr


def test_invalid_config_lists_the_errors(tmp_path: Path) -> None:
    (tmp_path / "deployment.toml").write_text("[network]\npublic_access = true\n")
    r = cli("bootstrap.sh", "--check", cwd=tmp_path, env={"AO_ETC": str(tmp_path / "etc")})
    assert r.returncode == 1 and "public_access = true is not supported" in r.stderr


def test_insecure_secrets_file_is_refused_and_kept(tmp_path: Path) -> None:
    (tmp_path / "deployment.toml").write_text("")
    s = tmp_path / "secrets.json"
    s.write_text(json.dumps({"tailscale_auth_key": TS_KEY}))
    s.chmod(0o644)
    r = cli("bootstrap.sh", "--check", "--secrets-file", str(s), cwd=tmp_path, env={"AO_ETC": str(tmp_path / "etc")})
    assert r.returncode == 1 and "readable by other users" in r.stderr and "chmod 600" in r.stderr
    assert TS_KEY not in r.stdout + r.stderr and s.exists() and s.read_text().count(TS_KEY) == 1


def test_apply_without_root_is_a_permission_error(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("running as root")
    (tmp_path / "deployment.toml").write_text("")
    r = cli("bootstrap.sh", "--non-interactive", cwd=tmp_path, env={"AO_ETC": str(tmp_path / "etc")})
    assert r.returncode == 1 and "run as root (sudo)" in r.stderr
    assert not (tmp_path / "etc").exists(), "nothing written before the permission check"


def test_stop_and_start_help_and_bad_options() -> None:
    for script in ("stop.sh", "start.sh"):
        r = cli(script, "--help")
        assert r.returncode == 0 and "sudo deployment/" in r.stdout, (script, r.stderr)
        assert cli(script, "--bogus").returncode == 1
    assert "--tailscale" in cli("stop.sh", "--help").stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="as root stop.sh would really stop the services")
def test_stop_tailscale_refuses_to_cut_an_ssh_session_through_the_tailnet() -> None:
    r = cli("stop.sh", "--tailscale", env={"SSH_CONNECTION": "100.101.102.103 50000 100.64.0.1 22"})
    assert r.returncode == 1 and "comes through Tailscale" in r.stderr
    r = cli("stop.sh", "--tailscale", env={"SSH_CONNECTION": "192.168.1.20 50000 192.168.1.2 22"})
    assert r.returncode == 1 and "run as root" in r.stderr  # a LAN session passes the guard
    r = cli("stop.sh", "--tailscale", "--force", env={"SSH_CONNECTION": "100.101.102.103 50000 100.64.0.1 22"})
    assert "run as root" in r.stderr


def test_secrets_are_never_accepted_as_arguments() -> None:
    r = cli("bootstrap.sh", "--tailscale-auth-key", TS_KEY)
    assert r.returncode == 2 and TS_KEY not in r.stdout + r.stderr


def test_interrupt_returns_130(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(a: Any) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(P, "cmd_detect", boom)
    monkeypatch.setitem(P.COMMANDS, "detect-os", boom) if hasattr(P, "COMMANDS") else None
    assert P.main(["detect-os"]) == 130


def test_no_set_x_and_no_secret_flags_in_the_entry_points() -> None:
    import re

    xtrace = re.compile(r"^\s*set\s+(-[a-wyz]*x|-o\s+xtrace)", re.M)
    for f in (REPO / "deployment").glob("*.sh"):
        assert not xtrace.search(f.read_text()), f
    py = (REPO / "deployment" / "provision.py").read_text()
    assert "auth-key=tskey" not in py and "--authkey" not in py
