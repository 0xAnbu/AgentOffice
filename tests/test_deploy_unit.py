"""Unit tests of deployment/provision.py: configuration, secrets, platform/Python detection, state interpretation of
Tailscale and Healthchecks.io, redaction, HTTP retry policy, report. No network, no root, no system changes."""

from __future__ import annotations

import json
import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from .deploy_fakes import REPO, load_provision

P = load_provision()
EXAMPLE = (REPO / "deployment" / "deployment.example.toml").read_text()


def cfg(text: str = "") -> dict[str, dict[str, Any]]:
    return P.parse_config(text)


def errors_of(text: str) -> str:
    with pytest.raises(P.DeployError) as e:
        P.parse_config(text)
    return str(e.value)


# -- configuration --------------------------------------------------------------------------------------------------


def test_example_config_is_valid_and_matches_the_defaults() -> None:
    assert P.parse_config(EXAMPLE) == cfg("")


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("[deploymnet]\nmode='production'", "unknown section [deploymnet]"),
        ("[network]\nprovidr='tailscale'", "unknown key network.providr"),
        ("[application]\nport='8080'", "application.port must be int"),
        ("[network]\nhttps=1", "network.https must be bool"),  # 1 is not true
        ("[application]\nport=true", "application.port must be int"),  # true is not 1
        ("[application]\nbind_host='0.0.0.0'", "bind_host must be 127.0.0.1"),
        ("[application]\nport=80", "between 1024 and 65535"),
        ("[application]\ngit_ref='../x'", "git_ref"),
        ("[network]\npublic_access=true", "public_access = true is not supported"),
        ("[network]\nhttps=false", "network.https must be true"),
        ("[network]\ntag='agentoffice'", "network.tag"),
        ("[monitoring]\ninterval_seconds=60", "interval_seconds must be between 300"),
        ("[monitoring]\ngrace_seconds=10", "grace_seconds"),
        ("[monitoring]\napi_url='http://healthchecks.io'", "https:// URL"),
        ("[monitoring]\nchannels='a,,b'", "monitoring.channels"),
        ("[python]\nversion='3.11'", "python.version"),
        ("[python]\ninterpreter='python3'", "absolute path"),
        ("[deployment]\nmode='staging'", "deployment.mode"),
        ("[deployment]\nhostname='Agent_Office'", "deployment.hostname"),
        ("[network]\nprovider='none'", "production requires network.provider"),
        ("[monitoring]\nprovider='none'", "production requires monitoring.provider"),
        ("[backup]\nlocal_enabled=false", "backup.local_enabled must be true"),
        ("[github_webhook]\nenabled=true\nfunnel_port=443", "must differ from network.https_port"),
        ("[github_webhook]\nenabled=true\nfunnel_port=9443", "Tailscale Funnel ports"),
        ("[github_webhook]\nenabled=true\nport=8080", "different from application.port"),
        ("[[github_webhook.repositories]]\nrepo='not a repo'", "owner/name"),
        ("[[github_webhook.repositories]]\nrepo='o/r'\nchannel='Bad Name'", "channel must match"),
        ("[[github_webhook.repositories]]\nrepo='o/r'\nbranch='main'", "must be a table with 'repo'"),
        ("this is = = not toml", "invalid TOML"),
    ],
)
def test_invalid_configurations_are_rejected_with_a_precise_message(text: str, needle: str) -> None:
    assert needle in errors_of(text)


def test_all_errors_are_reported_at_once() -> None:
    msg = errors_of("[x]\n[network]\nfoo=1\n[application]\nport='a'")
    assert "unknown section [x]" in msg and "unknown key network.foo" in msg and "application.port" in msg


def test_development_mode_may_skip_tailscale_and_monitoring() -> None:
    c = cfg("[deployment]\nmode='development'\n[network]\nprovider='none'\n[monitoring]\nprovider='none'")
    assert c["network"]["provider"] == "none" and c["monitoring"]["provider"] == "none"


def test_webhook_requires_tailscale() -> None:
    text = "[deployment]\nmode='development'\n[network]\nprovider='none'\n[monitoring]\nprovider='none'\n"
    assert "requires network.provider = 'tailscale'" in errors_of(text + "[github_webhook]\nenabled=true")


def test_api_url_allows_plain_http_only_on_loopback() -> None:
    assert P.check_api_url("http://127.0.0.1:9000/", "x") == "http://127.0.0.1:9000"
    assert P.check_api_url("https://hc.example.org", "x") == "https://hc.example.org"
    for bad in ("http://10.0.0.1", "ftp://x", "file:///etc/passwd", "https://"):
        with pytest.raises(P.DeployError):
            P.check_api_url(bad, "x")


def test_default_channel_name_follows_the_backend_validator() -> None:
    assert P.default_channel("0xAnbu/AgentOffice") == "agentoffice"
    assert P.CHANNEL_RE.match(P.default_channel("org/.Weird Repo!"))


def test_config_search_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    etc = tmp_path / "etc"
    etc.mkdir()
    paths = P.Paths.from_env({"AO_ETC": str(etc)})
    monkeypatch.chdir(tmp_path)
    assert P.find_config(None, paths) is None
    (etc / "deployment.toml").write_text("")
    assert P.find_config(None, paths) == etc / "deployment.toml"
    (tmp_path / "deployment.toml").write_text("")
    assert P.find_config(None, paths) == tmp_path / "deployment.toml"
    with pytest.raises(P.DeployError, match="config file not found"):
        P.find_config(str(tmp_path / "nope.toml"), paths)
    with pytest.raises(P.DeployError, match=r"no deployment\.toml found"):
        (tmp_path / "deployment.toml").unlink()
        (etc / "deployment.toml").unlink()
        P.load_config(None, paths, required=True)


# -- secrets --------------------------------------------------------------------------------------------------------

TS_KEY = "tskey-auth-kUNITtest0CNTRL-0123456789abcdef"
HC_KEY = "hcw_unit_test_key_abcdefghij"


def write_secrets(path: Path, data: dict[str, Any], mode: int = 0o600) -> Path:
    path.write_text(json.dumps(data))
    path.chmod(mode)
    return path


def test_secrets_file_is_read(tmp_path: Path) -> None:
    f = write_secrets(
        tmp_path / "s.json", {"tailscale_auth_key": TS_KEY, "healthchecks_api_key": HC_KEY, "github_token": ""}
    )
    s = P.load_secrets(str(f), env={})
    assert s == {"tailscale_auth_key": TS_KEY, "healthchecks_api_key": HC_KEY}


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_secrets_file_readable_by_others_is_rejected(tmp_path: Path, mode: int) -> None:
    f = write_secrets(tmp_path / "s.json", {"tailscale_auth_key": TS_KEY}, mode)
    with pytest.raises(P.DeployError) as e:
        P.load_secrets(str(f), env={})
    assert "readable by other users" in str(e.value) and "chmod 600" in e.value.hint
    assert TS_KEY not in str(e.value)


def test_secrets_file_symlink_is_rejected(tmp_path: Path) -> None:
    real = write_secrets(tmp_path / "real.json", {"tailscale_auth_key": TS_KEY})
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(P.DeployError, match="regular file"):
        P.load_secrets(str(link), env={})


@pytest.mark.parametrize(
    ("data", "needle"),
    [
        ({"tailscale_key": TS_KEY}, "unknown keys"),
        ({"tailscale_auth_key": "REPLACE_ME"}, "placeholder"),
        ({"tailscale_auth_key": "not-a-key"}, "does not look like a Tailscale auth key"),
        ({"healthchecks_api_key": 123}, "must be a string"),
        ({"healthchecks_api_key": "a b c d e f"}, "whitespace"),
    ],
)
def test_bad_secret_values_are_rejected_without_echoing_them(tmp_path: Path, data: dict[str, Any], needle: str) -> None:
    f = write_secrets(tmp_path / "s.json", data)
    with pytest.raises(P.DeployError) as e:
        P.load_secrets(str(f), env={})
    assert needle in str(e.value)
    for v in data.values():
        if isinstance(v, str) and len(v) > 8:
            assert v not in str(e.value)


def test_invalid_json_error_does_not_quote_the_file(tmp_path: Path) -> None:
    f = tmp_path / "s.json"
    f.write_text('{"tailscale_auth_key": "' + TS_KEY + '",,}')
    f.chmod(0o600)
    with pytest.raises(P.DeployError) as e:
        P.load_secrets(str(f), env={})
    assert "not valid JSON" in str(e.value) and TS_KEY not in str(e.value)


def test_environment_variables_override_the_file(tmp_path: Path) -> None:
    f = write_secrets(tmp_path / "s.json", {"healthchecks_api_key": "hcw_from_file_aaaaaaaa"})
    s = P.load_secrets(str(f), env={"AO_HEALTHCHECKS_API_KEY": HC_KEY, "AO_TAILSCALE_AUTH_KEY": TS_KEY})
    assert s["healthchecks_api_key"] == HC_KEY and s["tailscale_auth_key"] == TS_KEY


def test_explicit_missing_secrets_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(P.DeployError, match="secrets file not found"):
        P.load_secrets(str(tmp_path / "nope.json"), env={})


def test_the_original_secrets_file_is_never_modified(tmp_path: Path) -> None:
    f = write_secrets(tmp_path / "s.json", {"tailscale_auth_key": TS_KEY})
    before = (f.read_bytes(), f.stat().st_mode, f.stat().st_mtime_ns)
    P.load_secrets(str(f), env={})
    assert (f.read_bytes(), f.stat().st_mode, f.stat().st_mtime_ns) == before


def test_secret_file_helper_is_private_and_removed() -> None:
    with P.secret_file(TS_KEY) as f:
        assert f.read_text() == TS_KEY
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
        assert stat.S_IMODE(f.parent.stat().st_mode) == 0o700
        d = f.parent
    assert not d.exists()


def test_secret_file_is_removed_on_error() -> None:
    with pytest.raises(RuntimeError), P.secret_file(TS_KEY) as f:
        d = f.parent
        raise RuntimeError
    assert not d.exists()


# -- redaction ------------------------------------------------------------------------------------------------------


def test_redaction_of_registered_and_pattern_secrets() -> None:
    P.register_secret("super-secret-value-123")
    text = (
        "a super-secret-value-123 b tskey-auth-kXYZ-123 c https://hc-ping.com/0f1e2d3c-aaaa-bbbb-cccc-111122223333 "
        "d al_abcdefghijklmnopqrstuvwxyz e ghp_abcdefghijklmnopqrstuvwxyz0123"
    )
    out = P.redact(text)
    for s in ("super-secret-value-123", "tskey-auth-kXYZ-123", "0f1e2d3c-aaaa", "al_abcdefghij", "ghp_abcdefghij"):
        assert s not in out
    assert "https://hc-ping.com/[REDACTED]" in out


def test_log_lines_are_redacted(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    P.register_secret("hcw_logged_secret_0001")
    P.open_log(tmp_path / "logs" / "bootstrap.log")
    try:
        P.log("value hcw_logged_secret_0001 and tskey-auth-kLOG-1")
    finally:
        P._LOG_FILE = None
    err = capsys.readouterr().err
    assert "hcw_logged_secret_0001" not in err and "tskey-auth-kLOG-1" not in err
    logged = (tmp_path / "logs" / "bootstrap.log").read_text()
    assert "hcw_logged_secret_0001" not in logged and "[REDACTED]" in logged
    assert stat.S_IMODE((tmp_path / "logs" / "bootstrap.log").stat().st_mode) == 0o600


# -- env file -------------------------------------------------------------------------------------------------------


def test_env_set_is_atomic_and_keeps_mode(tmp_path: Path) -> None:
    f = tmp_path / "agent-office.env"
    f.write_text("AGENTLINK_ADMIN_TOKEN=al_x\nHC_PING_URL=\n# comment\n")
    f.chmod(0o640)
    assert P.env_set(f, "HC_PING_URL", "https://hc-ping.com/u") is True
    assert P.env_set(f, "HC_PING_URL", "https://hc-ping.com/u") is False  # idempotent
    assert P.env_set(f, "AGENTLINK_WEBHOOK_PORT", "8081") is True
    assert (
        f.read_text()
        == "AGENTLINK_ADMIN_TOKEN=al_x\nHC_PING_URL=https://hc-ping.com/u\n# comment\nAGENTLINK_WEBHOOK_PORT=8081\n"
    )
    assert stat.S_IMODE(f.stat().st_mode) == 0o640
    assert P.env_get(f, "HC_PING_URL") == "https://hc-ping.com/u"
    with pytest.raises(P.DeployError):
        P.env_set(f, "X", "a\nINJECTED=1")


# -- platform -------------------------------------------------------------------------------------------------------


def osr(**kw: str) -> dict[str, str]:
    return kw


@pytest.mark.parametrize(
    ("release", "machine", "ok", "tested", "codename", "apt", "sys_py"),
    [
        (osr(ID="ubuntu", VERSION_ID="24.04", VERSION_CODENAME="noble"), "x86_64", True, True, "noble", "ubuntu", True),
        (osr(ID="ubuntu", VERSION_ID="26.04", VERSION_CODENAME="resolute"), "aarch64", True, True, "resolute", "ubuntu",
         True),
        # older Ubuntu: allowed, not tested, Python comes from uv
        (osr(ID="ubuntu", VERSION_ID="22.04", VERSION_CODENAME="jammy"), "x86_64", True, False, "jammy", "ubuntu", False),
        (osr(ID="debian", VERSION_ID="12", VERSION_CODENAME="bookworm"), "x86_64", True, True, "bookworm", "debian",
         False),
        (osr(ID="debian", VERSION_ID="13", VERSION_CODENAME="trixie"), "aarch64", True, True, "trixie", "debian", True),
        (osr(ID="debian", VERSION_ID="12"), "arm64", True, True, "bookworm", "debian", False),  # Raspberry Pi OS 64-bit
        (osr(ID="debian", VERSION_ID="11", VERSION_CODENAME="bullseye"), "x86_64", True, False, "bullseye", "debian",
         False),
        (osr(ID="debian", VERSION_CODENAME="forky"), "x86_64", True, False, "forky", "debian", False),
        # derivatives: allowed; the Tailscale repository of the parent is used when os-release names its codename
        (osr(ID="linuxmint", ID_LIKE="ubuntu debian", VERSION_ID="22", UBUNTU_CODENAME="jammy"), "x86_64", True, False,
         "jammy", "ubuntu", False),
        (osr(ID="kali", ID_LIKE="debian", VERSION_ID="2026.4", VERSION_CODENAME="kali-rolling"), "x86_64", True, False,
         "kali-rolling", "", False),
        (osr(ID="fedora", VERSION_ID="43"), "x86_64", True, False, "", "", False),
        (osr(ID="arch"), "aarch64", True, False, "", "", False),
        (osr(), "x86_64", True, False, "", "", False),
        # refused: 32-bit, other architectures
        (osr(ID="raspbian", VERSION_ID="12", VERSION_CODENAME="bookworm"), "armv7l", False, False, "", "", False),
        (osr(ID="raspbian", VERSION_ID="12", VERSION_CODENAME="bookworm"), "aarch64", False, False, "", "", False),
        (osr(ID="debian", VERSION_ID="12"), "armv7l", False, False, "", "", False),
        (osr(ID="fedora", VERSION_ID="43"), "riscv64", False, False, "", "", False),
    ],
)  # fmt: skip
def test_platform_support_matrix(
    release: dict[str, str], machine: str, ok: bool, tested: bool, codename: str, apt: str, sys_py: bool
) -> None:
    p = P.detect_platform(release, machine)
    assert p.supported is ok, p.reason
    if ok:
        assert p.tested is tested
        assert p.codename == codename and p.apt_distro == apt and p.system_python_ok is sys_py
        assert p.arch in ("x86_64", "aarch64")
        assert bool(p.reason) is not tested  # untested distributions carry a warning
    else:
        assert p.reason


def test_32_bit_userland_and_non_linux_are_refused() -> None:
    debian = {"ID": "debian", "VERSION_ID": "12"}
    assert not P.detect_platform(debian, "aarch64", userland_bits=32).supported
    assert not P.detect_platform(debian, "x86_64", system="Darwin").supported


def test_raspberry_pi_model_is_recorded() -> None:
    p = P.detect_platform({"ID": "debian", "VERSION_ID": "12"}, "aarch64", "Raspberry Pi 5 Model B Rev 1.0")
    assert p.supported and p.raspberry_pi and p.apt_distro == "debian"


def test_os_release_parsing() -> None:
    text = '# c\nPRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nID=debian\nVERSION_ID="12"\nVERSION_CODENAME=bookworm\n\nBAD\n'
    d = P.parse_os_release(text)
    assert d == {
        "PRETTY_NAME": "Debian GNU/Linux 12 (bookworm)",
        "ID": "debian",
        "VERSION_ID": "12",
        "VERSION_CODENAME": "bookworm",
    }


def test_detect_os_cli_with_an_os_release_override(tmp_path: Path) -> None:
    import subprocess
    import sys

    f = tmp_path / "os-release"
    f.write_text("ID=ubuntu\nVERSION_ID=24.04\nVERSION_CODENAME=noble\nPRETTY_NAME=Ubuntu 24.04\n")
    r = subprocess.run([sys.executable, "-I", str(P.__file__), "detect-os"], capture_output=True, text=True,
                       env={**os.environ, "AO_OS_RELEASE": str(f)}, check=False)  # fmt: skip
    if os.uname().machine in ("x86_64", "aarch64"):
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["codename"] == "noble"


# -- Python detection -----------------------------------------------------------------------------------------------


def test_python_probe_of_the_running_interpreter() -> None:
    import sys

    info = P.probe_python(sys.executable)
    assert info is not None and info.version[:2] == tuple(sys.version_info[:2])
    assert info.compatible is (sys.version_info >= (3, 12))


def test_python_probe_reports_problems() -> None:
    assert "Python < 3.12" in P.PyInfo("/x", (3, 11, 2), (3, 40, 1), True).describe()
    assert "SQLite < 3.35" in P.PyInfo("/x", (3, 12, 0), (3, 31, 1), True).describe()
    assert not P.PyInfo("/x", (3, 12, 0), (3, 40, 0), False).compatible
    assert P.probe_python("/nonexistent/python3") is None


def test_python_candidates_prefer_the_recorded_interpreter(tmp_path: Path) -> None:
    import sys

    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "deploy.conf").write_text(f"AO_SRC=/src\nAO_PYTHON={sys.executable}\n")
    managed = tmp_path / "home" / "python" / "cpython-3.12.12-linux-x86_64-gnu" / "bin"
    managed.mkdir(parents=True)
    (managed / "python3").write_text("")
    ctx = make_ctx(tmp_path, {"AO_ETC": str(etc), "AO_HOME": str(tmp_path / "home")})
    c = P.python_candidates(ctx)
    assert c[0] == sys.executable and c[1] == str(managed / "python3")
    assert len(c) == len(set(c))


def make_ctx(tmp_path: Path, env: dict[str, str] | None = None, text: str = "") -> Any:
    paths = P.Paths.from_env({"AO_ETC": str(tmp_path / "etc"), "AO_HOME": str(tmp_path / "home"), **(env or {})})
    plat = P.detect_platform({"ID": "debian", "VERSION_ID": "12"}, "x86_64")
    return P.Ctx(P.parse_config(text), None, {}, paths, plat, P.State(tmp_path / "state.json", False))


# -- Tailscale state ------------------------------------------------------------------------------------------------


def ts_status(backend: str = "Running", **kw: Any) -> dict[str, Any]:
    return {
        "BackendState": backend,
        "Self": {
            "DNSName": "agentoffice.tail1234.ts.net.",
            "TailscaleIPs": ["100.64.0.5", "fd7a::5"],
            "Tags": ["tag:agentoffice-server"],
        },
        "CurrentTailnet": {"Name": "example.com", "MagicDNSSuffix": "tail1234.ts.net", "MagicDNSEnabled": True},
        "CertDomains": ["agentoffice.tail1234.ts.net"],
        **kw,
    }


@pytest.mark.parametrize(
    ("backend", "kind"),
    [("Running", "running"), ("NeedsLogin", "needs_login"), ("NeedsMachineAuth", "needs_approval"),
     ("Stopped", "stopped"), ("Starting", "starting"), ("NoState", "starting"), ("Weird", "unknown")],
)  # fmt: skip
def test_tailscale_backend_states(backend: str, kind: str) -> None:
    assert P.classify_tailscale(ts_status(backend)).kind == kind


def test_tailscale_running_details() -> None:
    st = P.classify_tailscale(ts_status())
    assert st.dns_name == "agentoffice.tail1234.ts.net" and st.https_enabled and st.magicdns
    assert st.in_tailnet("example.com") and st.in_tailnet("tail1234.ts.net") and st.in_tailnet("")
    assert not st.in_tailnet("other.org")


def test_tailscale_https_disabled_and_missing_fields() -> None:
    st = P.classify_tailscale(ts_status(CertDomains=None))
    assert not st.https_enabled
    assert P.classify_tailscale({}).kind == "unknown"
    assert P.classify_tailscale(ts_status(Self={"DNSName": ""})).kind == "starting"  # Running without a name yet


@pytest.mark.parametrize(
    ("stderr", "needle"),
    [
        ("backend error: invalid key: API key does not exist", "rejected the auth key"),
        ("backend error: requested tags [tag:x] are invalid or not permitted", "refused the tag"),
        ("dial tcp: lookup controlplane.tailscale.com: no such host", "DNS problem"),
        ("timeout waiting for Tailscale service to enter a Running state", "timed out"),
        ("open /var/lib/tailscale: permission denied", "permission problem"),
        ("something else", "tailscale up failed"),
    ],
)
def test_tailscale_up_errors_are_diagnosed(stderr: str, needle: str) -> None:
    e = P.diagnose_up(stderr, "tag:x")
    assert needle in str(e)


SERVE_OURS = {
    "TCP": {"443": {"HTTPS": True}},
    "Web": {"agentoffice.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8080"}}}},
}


@pytest.mark.parametrize(
    ("serve", "kind"),
    [
        ({}, "absent"),
        (SERVE_OURS, "ours"),
        ({**SERVE_OURS, "Web": {"agentoffice.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": "http://localhost:8080"}}}}}, "ours"),
        ({**SERVE_OURS, "Web": {"agentoffice.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}}, "conflict"),
        ({**SERVE_OURS, "Web": {"agentoffice.tail1234.ts.net:443": {"Handlers": {"/": {"Path": "/srv/www"}}}}}, "conflict"),
        ({**SERVE_OURS, "Web": {"agentoffice.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8080"}, "/x": {"Proxy": "http://127.0.0.1:9"}}}}}, "conflict"),
        ({"TCP": {"443": {"TCPForward": "127.0.0.1:22"}}}, "conflict"),
        ({**SERVE_OURS, "AllowFunnel": {"agentoffice.tail1234.ts.net:443": True}}, "exposed"),
        ({"AllowFunnel": {"agentoffice.tail1234.ts.net:443": True}}, "exposed"),
    ],
)  # fmt: skip
def test_serve_config_interpretation_for_the_api(serve: dict[str, Any], kind: str) -> None:
    assert P.classify_port(serve, "agentoffice.tail1234.ts.net", 443, "http://127.0.0.1:8080", funnel=False)[0] == kind


def test_serve_config_interpretation_for_the_webhook_funnel() -> None:
    web = {"agentoffice.tail1234.ts.net:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8081"}}}}
    on = {"TCP": {"8443": {"HTTPS": True}}, "Web": web, "AllowFunnel": {"agentoffice.tail1234.ts.net:8443": True}}
    off = {"TCP": {"8443": {"HTTPS": True}}, "Web": web}
    dns = "agentoffice.tail1234.ts.net"
    assert P.classify_port(on, dns, 8443, "http://127.0.0.1:8081", funnel=True)[0] == "ours"
    assert P.classify_port(off, dns, 8443, "http://127.0.0.1:8081", funnel=True)[0] == "conflict"
    assert P.funnel_ports(on) == [f"{dns}:8443"]
    # the API port is unaffected by the webhook funnel
    assert (
        P.classify_port(
            {**on, **SERVE_OURS, "Web": {**web, **SERVE_OURS["Web"]}}, dns, 443, "http://127.0.0.1:8080", funnel=False
        )[0]
        == "ours"
    )


# -- Healthchecks.io state ------------------------------------------------------------------------------------------

CHANNELS = [{"id": "c1", "name": "mail", "kind": "email"}, {"id": "c2", "name": "sms", "kind": "sms"}]


def test_channel_selection() -> None:
    assert P.select_channels(CHANNELS, "*") == ["c1", "c2"]
    assert P.select_channels(CHANNELS, "sms") == ["c2"]
    with pytest.raises(P.DeployError, match="not found"):
        P.select_channels(CHANNELS, "pager")
    with pytest.raises(P.DeployError, match="no integrations"):
        P.select_channels([], "*")


def test_desired_check_and_drift() -> None:
    m = cfg("")["monitoring"]
    want = P.desired_check(m, ["c1", "c2"])
    assert want["timeout"] == 300 and want["grace"] == 600 and want["name"] == "agentoffice-production"
    assert P.MANAGED_MARKER in want["desc"] and want["channels"] == "c1,c2"
    same = {**want, "channels": "c2,c1", "tags": "agentoffice"}
    assert P.check_drift(same, want) == []
    assert P.check_drift({**same, "grace": 3600}, want) == ["grace 3600 -> 600"]
    assert any("integrations" in d for d in P.check_drift({**same, "channels": "c1"}, want))
    assert P.check_drift({**same, "desc": "mine"}, want) == ["description"]


def test_check_validation() -> None:
    with pytest.raises(P.DeployError, match="ping_url"):
        P.valid_check({"name": "x"})
    c = P.valid_check(
        {"ping_url": "https://hc-ping.com/abc-uuid-0000", "update_url": "https://healthchecks.io/api/v3/checks/u-1"}
    )
    assert P.check_uuid(c) == "u-1"
    assert "https://hc-ping.com/abc-uuid-0000" not in P.redact("x https://hc-ping.com/abc-uuid-0000")


# -- HTTP retry policy ----------------------------------------------------------------------------------------------


class Script:
    """A tiny HTTP server answering a scripted list of status codes."""

    def __init__(self, codes: list[int], headers: dict[str, str] | None = None) -> None:
        self.codes, self.calls = list(codes), 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:
                outer.calls += 1
                code = outer.codes.pop(0) if outer.codes else 200
                body = json.dumps({"ok": code == 200}).encode()
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/"

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AO_HTTP_BACKOFF", "0.01")


@pytest.mark.parametrize(
    ("codes", "status", "calls"),
    [([500, 502, 200], 200, 3), ([429, 200], 200, 2), ([401], 401, 1), ([403], 403, 1), ([404], 404, 1)],
)
def test_http_retries_only_transient_errors(codes: list[int], status: int, calls: int) -> None:
    s = Script(codes, {"Retry-After": "0"})
    try:
        code, _ = P.http_json("GET", s.url, headers={}, name="svc")
        assert (code, s.calls) == (status, calls)
    finally:
        s.close()


def test_http_gives_up_after_bounded_retries() -> None:
    s = Script([503, 503, 503, 503])
    try:
        with pytest.raises(P.DeployError, match="unavailable after 3 attempts"):
            P.http_json("GET", s.url, headers={}, name="svc")
        assert s.calls == 3
    finally:
        s.close()


def test_http_unreachable_is_a_clear_error() -> None:
    with pytest.raises(P.DeployError, match="unavailable after 2 attempts"):
        P.http_json("GET", "http://127.0.0.1:1/", headers={}, name="svc", retries=2, timeout=1)


# -- report ---------------------------------------------------------------------------------------------------------


def test_overall_result() -> None:
    R = P.Row
    assert P.overall([R("A", "PASS", ""), R("B", "NOT_VERIFIED", "", required=False)]) == "SUCCESS"
    assert P.overall([R("A", "PASS", ""), R("B", "NOT_VERIFIED", "")]) == "INCOMPLETE"
    assert P.overall([R("A", "FAIL", ""), R("B", "NOT_VERIFIED", "")]) == "FAILED"
    assert P.overall([R("A", "PASS", ""), R("B", "SKIPPED", "")]) == "SUCCESS"


def test_report_format_and_redaction(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    P.register_secret("al_report_secret_value_0123456789")
    rows = [P.Row("Linux", "PASS", "Debian 12"), P.Row("API", "FAIL", "token al_report_secret_value_0123456789 leaked")]
    text = P.render_report(ctx, rows)
    assert "AGENTOFFICE DEPLOYMENT REPORT" in text and "Deployment:" in text and "FAILED" in text
    assert "al_report_secret_value_0123456789" not in text
    assert text.index("Linux:") < text.index("API:")


def test_state_file_is_private(tmp_path: Path) -> None:
    s = P.State(tmp_path / "etc" / "bootstrap-state.json", True)
    s.mark("install", "failed", "boom tskey-auth-kSTATE-1")
    data = json.loads((tmp_path / "etc" / "bootstrap-state.json").read_text())
    assert data["stages"]["install"]["status"] == "failed" and "tskey-auth-kSTATE-1" not in json.dumps(data)
    assert stat.S_IMODE((tmp_path / "etc" / "bootstrap-state.json").stat().st_mode) == 0o600


def test_wizard_config_rendering_is_valid_toml_and_escapes() -> None:
    ans = {"mode": "production", "hostname": "agentoffice", "port": "8080", "network": "tailscale",
           "tag": "tag:agentoffice-server", "tailnet": "", "monitoring": "healthchecks", "check_name": "agentoffice-production",
           "channels": "*", "webhook": True, "funnel_port": "8443", "repos": ["o/r", "o/s"]}  # fmt: skip
    c = P.parse_config(P.render_config(ans))
    assert c["github_webhook"]["enabled"] and [r["repo"] for r in c["github_webhook"]["repositories"]] == ["o/r", "o/s"]
    assert P.toml_str('a"b\\c') == '"a\\"b\\\\c"'
    assert P.parse_config(
        P.render_config({**ans, "mode": "development", "network": "none", "monitoring": "none", "webhook": False})
    )
