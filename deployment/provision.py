#!/usr/bin/env python3
"""AgentOffice automated deployment: the logic behind deployment/bootstrap.sh and the provision-*.sh scripts.

Standard library only (Python >= 3.11 for tomllib), so it runs with the distribution's python3 before any virtualenv
exists. It orchestrates the existing scripts (install.sh, backup.sh, healthcheck.sh, the systemd units) and adds what
they lack: OS detection, Python provisioning, Tailscale registration + Serve, Healthchecks.io provisioning, the
optional GitHub webhook (reusing the webhook-only listener), and a live verification with PASS/FAIL/NOT_VERIFIED.

Security rules kept throughout: configuration files are parsed as data (never sourced or executed); secrets are never
put in argv, logs, reports or exception texts (every log line goes through `redact`); the Tailscale auth key reaches
`tailscale up` through `--auth-key=file:` in a private temporary directory that is removed afterwards; the
Healthchecks.io API key is used only during provisioning and never persisted; the API is never put on Funnel.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import getpass
import hashlib
import http.client
import ipaddress
import json
import os
import re
import secrets as pysecrets
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

if sys.version_info < (3, 11):  # pragma: no cover - guarded by bootstrap.sh too
    sys.exit("provision.py needs Python >= 3.11 (tomllib)")

import tomllib

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

PASS, FAIL, NOT_VERIFIED, SKIPPED = "PASS", "FAIL", "NOT_VERIFIED", "SKIPPED"
MANAGED_MARKER = "managed-by: agentoffice-bootstrap"
UV_VERSION = "0.13.9"
UV_SHA256 = {  # https://github.com/astral-sh/uv/releases/tag/0.13.9 (*.tar.gz.sha256)
    "x86_64": "a7fbaff2b9a271d85c1bdb540941090fd1ce011797b4abeb48e4dcd39c2471d5",
    "aarch64": "0797542018e541a9288855df555c8077eed06f7a47d206362030480e8ecc9f3d",
}
# Primary key of "Tailscale Inc. (Package repository signing key)", served at pkgs.tailscale.com/stable/<distro>/.
TAILSCALE_KEY_FPR = "2596A99EAAB33821893C0A79458CA832957F5868"
WEBHOOK_EVENTS = ["push", "pull_request", "issues", "issue_comment", "workflow_run", "release"]
DOC = {
    "auth_keys": "https://tailscale.com/kb/1085/auth-keys",
    "device_approval": "https://tailscale.com/kb/1099/device-approval",
    "tags": "https://tailscale.com/kb/1068/tags",
    "https": "https://tailscale.com/kb/1153/enabling-https",
    "funnel": "https://tailscale.com/kb/1223/funnel",
    "hc_api": "https://healthchecks.io/docs/api/",
    "hc_integrations": "https://healthchecks.io/docs/configuring_notifications/",
}


# -- errors, logging, redaction -------------------------------------------------------------------------------------


class DeployError(Exception):
    """A failure with an operator-facing message. Messages must never contain secrets (they go through redact())."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


_SECRETS: set[str] = set()
_SECRET_PATTERNS = [
    re.compile(r"tskey-[A-Za-z0-9_-]+"),
    re.compile(r"(https?://[^/\s]*hc-ping\.com/)[^\s\"']+"),
    re.compile(r"\bal_[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"),
]
_LOG_FILE: Path | None = None


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        _SECRETS.add(value)


def redact(text: str) -> str:
    for s in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(s, "[REDACTED]")
    for p in _SECRET_PATTERNS:
        text = p.sub(lambda m: (m.group(1) if m.groups() and m.group(1) else "") + "[REDACTED]", text)
    return text


def _tty(stream: Any) -> bool:
    return (
        "NO_COLOR" not in os.environ
        and os.environ.get("TERM") != "dumb"
        and bool(getattr(stream, "isatty", None))
        and stream.isatty()
    )


_COLORS = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "blue": "34", "cyan": "36"}


def paint(text: str, *styles: str, stream: Any = None) -> str:
    """ANSI colour only on a terminal (and never with NO_COLOR); log files and pipes stay plain."""
    if not styles or not _tty(stream or sys.stdout):
        return text
    return "\033[" + ";".join(_COLORS[x] for x in styles) + "m" + text + "\033[0m"


_LOG_STYLES = {"DO": ("green", "▸"), "PLAN": ("yellow", "◦"), "ERROR:": ("red", "✖"), "HINT:": ("yellow", "➜"),
               "WARN:": ("yellow", "⚠")}  # fmt: skip


def _pretty(msg: str) -> str:
    """How a log line looks on a terminal: stage headers stand out, DO/PLAN/ERROR get a symbol and a colour."""
    if msg.startswith("== "):
        return "\n" + paint("━━ " + msg[3:], "cyan", "bold", stream=sys.stderr)
    word, _, rest = msg.partition(" ")
    if word in _LOG_STYLES:
        color, mark = _LOG_STYLES[word]
        return paint(f"  {mark} {word.rstrip(':')}", color, "bold", stream=sys.stderr) + " " + rest.lstrip()
    return paint("  · ", "dim", stream=sys.stderr) + msg


def log(msg: str) -> None:
    line = redact(f"[agent-office] {msg}")
    shown = _pretty(redact(msg)) if _tty(sys.stderr) else line
    print(shown, file=sys.stderr, flush=True)
    if _LOG_FILE is not None:
        with contextlib.suppress(OSError), open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now(dt.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')} {line}\n")


def open_log(path: Path) -> None:
    global _LOG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o750)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    _LOG_FILE = path


# -- paths (same defaults and overrides as deployment/lib.sh) -------------------------------------------------------


@dataclass
class Paths:
    home: Path
    data: Path
    etc: Path
    backups: Path
    units: Path
    user: str
    user_mode: bool
    env_file: Path
    log_dir: Path

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Paths:
        e = os.environ if env is None else env
        etc = Path(e.get("AO_ETC", "/etc/agent-office"))
        return cls(
            home=Path(e.get("AO_HOME", "/opt/agent-office")),
            data=Path(e.get("AO_DATA", "/var/lib/agent-office")),
            etc=etc,
            backups=Path(e.get("AO_BACKUP_DIR", "/var/backups/agent-office")),
            units=Path(e.get("AO_UNIT_DIR", "/etc/systemd/system")),
            user=e.get("AO_USER", "agent-office"),
            user_mode=e.get("AO_USER_MODE", "0") == "1",
            env_file=Path(e.get("AO_ENV_FILE", str(etc / "agent-office.env"))),
            log_dir=Path(e.get("AO_LOG_DIR", "/var/log/agent-office")),
        )

    @property
    def state_file(self) -> Path:
        return self.etc / "bootstrap-state.json"

    @property
    def report_file(self) -> Path:
        return self.etc / "bootstrap-report.txt"

    @property
    def deploy_conf(self) -> Path:
        return self.etc / "deploy.conf"

    @property
    def venv_python(self) -> Path:
        return self.home / "current" / ".venv" / "bin" / "python"


# -- configuration (TOML, strict schema) ----------------------------------------------------------------------------

SCHEMA: dict[str, dict[str, tuple[type, Any]]] = {
    "deployment": {"mode": (str, "production"), "hostname": (str, "agentoffice")},
    "application": {"bind_host": (str, "127.0.0.1"), "port": (int, 8080), "git_ref": (str, "HEAD")},
    "python": {"version": (str, "3.12"), "interpreter": (str, "")},
    "network": {
        "provider": (str, "tailscale"),
        "tag": (str, "tag:agentoffice"),
        "tailnet": (str, ""),
        "https": (bool, True),
        "https_port": (int, 443),
        "public_access": (bool, False),
    },
    "monitoring": {
        "provider": (str, "healthchecks"),
        "api_url": (str, "https://healthchecks.io"),
        "interval_seconds": (int, 300),
        "grace_seconds": (int, 600),
        "check_name": (str, "agentoffice"),
        "tags": (str, "agentoffice"),
        "channels": (str, "*"),
        "adopt_existing": (bool, False),
    },
    "backup": {"local_enabled": (bool, True)},
    "github_webhook": {
        "enabled": (bool, False),
        "port": (int, 8081),
        "funnel_port": (int, 8443),
        "repositories": (list, []),
    },
}
DEFAULT_WEBHOOK_REPO = "Pigglet-Company/winnie-the-root"  # proposed by the wizard (install.sh --wizard)
HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
TAG_RE = re.compile(r"^tag:[A-Za-z][A-Za-z0-9-]{0,62}$")
TAILNET_RE = re.compile(r"^[A-Za-z0-9.@_-]{1,253}$")
GIT_REF_RE = re.compile(r"^[A-Za-z0-9._/][A-Za-z0-9._/-]{0,99}$")
CHECK_NAME_RE = re.compile(r"^[A-Za-z0-9._ -]{1,100}$")
HC_TAGS_RE = re.compile(r"^[A-Za-z0-9_ -]{0,200}$")
CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")  # agentlink/validation.py NAME_RE
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
FUNNEL_PORTS = (443, 8443, 10000)
SUPPORTED_PY = ("3.12", "3.13", "3.14")


def parse_config(text: str, source: str = "config") -> dict[str, dict[str, Any]]:
    """Parse and validate a deployment TOML. Unknown sections/keys and unsafe combinations are errors."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise DeployError(f"{source}: invalid TOML: {e}") from None
    errors: list[str] = []
    cfg: dict[str, dict[str, Any]] = {}
    for section in raw:
        if section not in SCHEMA:
            errors.append(f"unknown section [{section}]")
    for section, fields in SCHEMA.items():
        given = raw.get(section, {})
        if not isinstance(given, dict):
            errors.append(f"[{section}] must be a table")
            given = {}
        cfg[section] = {}
        for key in given:
            if key not in fields:
                errors.append(f"unknown key {section}.{key}")
        for key, (typ, default) in fields.items():
            value = given.get(key, default)
            if type(value) is not typ:  # bool is an int subclass: compare exact types
                errors.append(f"{section}.{key} must be {typ.__name__}, got {type(value).__name__}")
                value = default
            cfg[section][key] = value
    if not errors:
        errors.extend(_semantic_errors(cfg))
    if errors:
        raise DeployError(f"{source}: invalid configuration:\n  - " + "\n  - ".join(errors))
    return cfg


def _semantic_errors(c: dict[str, dict[str, Any]]) -> list[str]:
    err: list[str] = []
    d, a, py, n, m, b, g = (c[k] for k in SCHEMA)
    if d["mode"] not in ("production", "development"):
        err.append("deployment.mode must be 'production' or 'development'")
    if not HOSTNAME_RE.match(d["hostname"]):
        err.append("deployment.hostname must be a DNS label (a-z, 0-9, '-', at most 63 characters)")
    if a["bind_host"] != "127.0.0.1":
        err.append("application.bind_host must be 127.0.0.1: the backend is only reachable through Tailscale Serve")
    if not 1024 <= a["port"] <= 65535:
        err.append("application.port must be between 1024 and 65535")
    if not GIT_REF_RE.match(a["git_ref"]) or ".." in a["git_ref"]:
        err.append("application.git_ref is not a valid git reference")
    if py["version"] not in SUPPORTED_PY:
        err.append(f"python.version must be one of {', '.join(SUPPORTED_PY)}")
    if py["interpreter"] and not py["interpreter"].startswith("/"):
        err.append("python.interpreter must be an absolute path (or empty to detect/provision one)")
    if n["provider"] not in ("tailscale", "none"):
        err.append("network.provider must be 'tailscale' or 'none'")
    if n["public_access"]:
        err.append("network.public_access = true is not supported: AgentOffice is never published to the Internet")
    if n["provider"] == "tailscale":
        if not n["https"]:
            err.append("network.https must be true: the tailnet entry point is HTTPS (Tailscale Serve)")
        if not TAG_RE.match(n["tag"]):
            err.append("network.tag must look like tag:agentoffice")
        if n["tailnet"] and not TAILNET_RE.match(n["tailnet"]):
            err.append("network.tailnet must be a tailnet name such as example.com or tail1234.ts.net")
        if not 1 <= n["https_port"] <= 65535:
            err.append("network.https_port must be a TCP port")
    if m["provider"] not in ("healthchecks", "none"):
        err.append("monitoring.provider must be 'healthchecks' or 'none'")
    if m["provider"] == "healthchecks":
        try:
            check_api_url(m["api_url"], "monitoring.api_url")
        except DeployError as e:
            err.append(str(e))
        if not 300 <= m["interval_seconds"] <= 31536000:
            err.append(
                "monitoring.interval_seconds must be between 300 and 31536000: the health check timer pings every "
                "5 minutes, a shorter period would raise false alerts"
            )
        if not 60 <= m["grace_seconds"] <= 31536000:
            err.append("monitoring.grace_seconds must be between 60 and 31536000")
        if not CHECK_NAME_RE.match(m["check_name"]):
            err.append("monitoring.check_name: 1-100 characters among letters, digits, space, '.', '_' and '-'")
        if not HC_TAGS_RE.match(m["tags"]):
            err.append("monitoring.tags: space-separated words (letters, digits, '_', '-')")
        if m["channels"] != "*" and not all(x.strip() for x in m["channels"].split(",")):
            err.append("monitoring.channels must be '*' or a comma-separated list of integration names")
    if d["mode"] == "production":
        if n["provider"] != "tailscale":
            err.append("production requires network.provider = 'tailscale' (private HTTPS)")
        if m["provider"] != "healthchecks":
            err.append("production requires monitoring.provider = 'healthchecks' (external alerting)")
    if not b["local_enabled"]:
        err.append("backup.local_enabled must be true: the local backup timer is part of every installation")
    if g["enabled"]:
        if n["provider"] != "tailscale":
            err.append("github_webhook.enabled requires network.provider = 'tailscale'")
        if not 1024 <= g["port"] <= 65535 or g["port"] == a["port"]:
            err.append("github_webhook.port must be a free port (1024-65535) different from application.port")
        if g["funnel_port"] not in FUNNEL_PORTS:
            err.append(f"github_webhook.funnel_port must be one of {FUNNEL_PORTS} (Tailscale Funnel ports)")
        if g["funnel_port"] == n["https_port"]:
            err.append("github_webhook.funnel_port must differ from network.https_port (the API port stays private)")
    for i, r in enumerate(g["repositories"]):
        if not isinstance(r, dict) or set(r) - {"repo", "channel"} or "repo" not in r:
            err.append(f"github_webhook.repositories[{i}] must be a table with 'repo' and optional 'channel'")
            continue
        if not isinstance(r["repo"], str) or not REPO_RE.match(r["repo"]):
            err.append(f"github_webhook.repositories[{i}].repo must look like owner/name")
        ch = r.get("channel", default_channel(str(r["repo"])))
        if not isinstance(ch, str) or not CHANNEL_RE.match(ch):
            err.append(f"github_webhook.repositories[{i}].channel must match {CHANNEL_RE.pattern}")
    return err


def default_channel(repo: str) -> str:
    return re.sub(r"[^a-z0-9._-]", "-", repo.split("/")[-1].lower())[:64].lstrip("._-") or "github"


def check_api_url(url: str, what: str) -> str:
    u = urllib.parse.urlsplit(url)
    if u.scheme == "https" and u.hostname:
        return url.rstrip("/")
    if u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1"):
        return url.rstrip("/")  # plain HTTP only on loopback (self-hosted on the same machine, or tests)
    raise DeployError(f"{what} must be an https:// URL")


def find_config(explicit: str | None, paths: Paths) -> Path | None:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise DeployError(f"config file not found: {p}")
        return p
    for p in (Path.cwd() / "deployment.toml", paths.etc / "deployment.toml"):
        if p.is_file():
            return p
    return None


def load_config(explicit: str | None, paths: Paths, *, required: bool) -> tuple[dict[str, dict[str, Any]], Path | None]:
    p = find_config(explicit, paths)
    if p is None:
        if required:
            raise DeployError(
                f"no deployment.toml found (looked in ./deployment.toml and {paths.etc}/deployment.toml)",
                hint="cp deployment/deployment.example.toml deployment.toml, edit it, then re-run",
            )
        return parse_config("", "defaults"), None
    return parse_config(p.read_text("utf-8"), str(p)), p


# -- secrets (JSON file with strict permissions, or environment variables) ------------------------------------------

SECRET_ENV = {
    "tailscale_auth_key": "AO_TAILSCALE_AUTH_KEY",
    "healthchecks_api_key": "AO_HEALTHCHECKS_API_KEY",
    "github_webhook_secret": "AO_GITHUB_WEBHOOK_SECRET",
    "github_token": "AO_GITHUB_TOKEN",
}
PLACEHOLDERS = {"REPLACE_ME", "CHANGE_ME", "changeme", "xxx"}


def default_secrets_path() -> Path:
    return HERE / "bootstrap-secrets.json"


def check_private_file(p: Path, what: str) -> None:
    st = p.lstat()
    if not p.is_file() or p.is_symlink():
        raise DeployError(f"{what} {p} must be a regular file (not a symlink)")
    if st.st_mode & 0o077:
        raise DeployError(
            f"{what} {p} has permissions {oct(st.st_mode & 0o777)[2:]}: readable by other users",
            hint=f"chmod 600 {p}",
        )
    allowed = {0, os.getuid()}
    if os.environ.get("SUDO_UID", "").isdigit():
        allowed.add(int(os.environ["SUDO_UID"]))
    if st.st_uid not in allowed:
        raise DeployError(f"{what} {p} is owned by uid {st.st_uid}: it must belong to root or to you")


def load_secrets(explicit: str | None, env: dict[str, str] | None = None) -> dict[str, str]:
    """Secrets from the JSON file (if any) overridden by AO_* environment variables. Values are registered for
    redaction before anything else can print them."""
    e = os.environ if env is None else env
    values: dict[str, str] = {}
    p = Path(explicit) if explicit else default_secrets_path()
    if explicit and not p.exists():
        raise DeployError(f"secrets file not found: {p}")
    if p.exists():
        check_private_file(p, "secrets file")
        try:
            data = json.loads(p.read_text("utf-8"))
        except ValueError:
            raise DeployError(f"secrets file {p} is not valid JSON") from None  # no excerpt: it may hold secrets
        if not isinstance(data, dict):
            raise DeployError(f"secrets file {p} must contain a JSON object")
        unknown = sorted(set(data) - set(SECRET_ENV))
        if unknown:
            raise DeployError(f"secrets file {p}: unknown keys {unknown} (allowed: {sorted(SECRET_ENV)})")
        for k, v in data.items():
            if not isinstance(v, str):
                raise DeployError(f"secrets file {p}: {k} must be a string")
            register_secret(v)
            if v.strip():
                values[k] = v.strip()
    for k, var in SECRET_ENV.items():
        v = e.get(var, "").strip()
        register_secret(v)
        if v:
            values[k] = v
    for k, v in values.items():
        if v in PLACEHOLDERS:
            raise DeployError(f"secret {k} still has the example placeholder value")
        if any(ch.isspace() for ch in v):
            raise DeployError(f"secret {k} contains whitespace")
    if "tailscale_auth_key" in values and not values["tailscale_auth_key"].startswith("tskey-"):
        raise DeployError("tailscale_auth_key does not look like a Tailscale auth key (tskey-...)")
    return values


@contextlib.contextmanager
def secret_file(value: str) -> Iterator[Path]:
    """A private temporary copy of a secret (0600 inside a 0700 directory), always removed afterwards."""
    d = Path(tempfile.mkdtemp(prefix="ao-secret-"))
    try:
        os.chmod(d, 0o700)
        f = d / "value"
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(value)
        yield f
    finally:
        shutil.rmtree(d, ignore_errors=True)


# -- env file (KEY=VALUE, edited in place keeping mode and owner) ---------------------------------------------------


def env_get(path: Path, key: str) -> str:
    if not path.is_file():
        return ""
    value = ""
    for line in path.read_text("utf-8").splitlines():
        if line.startswith(f"{key}="):
            value = line[len(key) + 1 :].strip()
    return value


def env_set(path: Path, key: str, value: str) -> bool:
    """Set KEY=value (last occurrence wins, like lib.sh). Atomic; keeps mode/owner. Returns True if changed."""
    if "\n" in value or "\r" in value:
        raise DeployError(f"refusing a multi-line value for {key}")
    lines = path.read_text("utf-8").splitlines()
    idx = [i for i, line in enumerate(lines) if line.startswith(f"{key}=")]
    if idx and lines[idx[-1]] == f"{key}={value}":
        return False
    if idx:
        lines[idx[-1]] = f"{key}={value}"
    else:
        lines.append(f"{key}={value}")
    st = path.stat()
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, st.st_mode & 0o777)
    with contextlib.suppress(PermissionError):
        os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, path)
    return True


# -- platform detection ---------------------------------------------------------------------------------------------


TESTED_DISTROS = "Ubuntu 24.04+, Debian 12/13, Raspberry Pi OS 64-bit"


@dataclass
class Platform:
    """Any 64-bit Linux (x86_64/aarch64, 64-bit userland) is supported. `tested` marks the distributions the
    bootstrap is validated on; on the others every stage still runs, with a warning, and the parts that need a
    distribution-specific source (the Tailscale apt repository) say what to install by hand."""

    id: str
    version: str
    codename: str
    arch: str
    pretty: str
    supported: bool
    reason: str = ""  # why it is not supported, or (supported but not tested) a warning
    raspberry_pi: bool = False
    tested: bool = False
    apt_distro: str = ""  # Tailscale apt repository family ("ubuntu"/"debian"), "" if there is none for this OS
    libc: str = "glibc"

    @property
    def system_python_ok(self) -> bool:  # distribution python3 is known to be >= 3.12
        return self.tested and not (self.id == "debian" and self.version == "12")


def parse_os_release(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def detect_platform(
    os_release: dict[str, str],
    machine: str,
    model: str = "",
    *,
    system: str = "Linux",
    userland_bits: int = 64,
    libc: str = "glibc",
) -> Platform:
    osid = os_release.get("ID", "").lower()
    ver = os_release.get("VERSION_ID", "")
    code = os_release.get("VERSION_CODENAME", "")
    pretty = os_release.get("PRETTY_NAME", osid or "unknown Linux")
    arch = {"arm64": "aarch64"}.get(machine, machine)
    pi = "raspberry pi" in model.lower()
    p = Platform(osid, ver, code, arch, pretty, False, raspberry_pi=pi, libc=libc)
    if system != "Linux":
        p.reason = f"{system} is not supported: AgentOffice servers run on Linux"
        return p
    if arch not in ("x86_64", "aarch64"):
        p.reason = f"architecture {machine} is not supported (64-bit x86_64 or aarch64 required)"
        return p
    if osid == "raspbian" or userland_bits != 64:
        p.reason = (
            "32-bit userland on a 64-bit kernel is not supported: install the 64-bit OS (e.g. Raspberry Pi OS 64-bit)"
        )
        return p
    p.supported = True
    like = os_release.get("ID_LIKE", "").lower().split()
    if osid == "ubuntu":
        try:
            major, minor = (int(x) for x in ver.split(".")[:2])
        except ValueError:
            major, minor = 0, 0
        p.apt_distro = "ubuntu" if code else ""
        p.tested = (major, minor) >= (24, 4) and bool(code)
    elif osid == "debian":
        p.codename = {"12": "bookworm", "13": "trixie"}.get(ver, code)
        p.apt_distro = "debian" if p.codename else ""
        p.tested = ver in ("12", "13")
    elif os_release.get("UBUNTU_CODENAME") and "ubuntu" in like:  # Linux Mint, Pop!_OS, elementary, ...
        p.codename, p.apt_distro = os_release["UBUNTU_CODENAME"], "ubuntu"
    elif os_release.get("DEBIAN_CODENAME") and "debian" in like:
        p.codename, p.apt_distro = os_release["DEBIAN_CODENAME"], "debian"
    if not p.tested:
        p.reason = (
            f"{pretty} (ID={osid or '?'}{', ID_LIKE=' + ' '.join(like) if like else ''}) is not a tested distribution "
            f"({TESTED_DISTROS}): the bootstrap runs, but report problems with the distribution name"
        )
    return p


def current_platform() -> Platform:
    import platform as _platform
    import struct

    osr = Path(os.environ.get("AO_OS_RELEASE", "/etc/os-release"))  # override: tests only
    model = ""
    with contextlib.suppress(OSError):
        model = Path("/proc/device-tree/model").read_text("utf-8", "replace").strip("\x00")
    text = osr.read_text("utf-8") if osr.is_file() else ""
    if not text and not os.environ.get("AO_OS_RELEASE"):
        alt = Path("/usr/lib/os-release")
        text = alt.read_text("utf-8") if alt.is_file() else ""
    libc = "glibc" if _platform.libc_ver()[0] == "glibc" else (_platform.libc_ver()[0] or "other")
    u = os.uname()
    return detect_platform(parse_os_release(text), u.machine, model, system=u.sysname,
                           userland_bits=struct.calcsize("P") * 8, libc=libc)  # fmt: skip


# -- subprocess helpers ---------------------------------------------------------------------------------------------


@dataclass
class Result:
    code: int
    out: str
    err: str


def run(
    argv: list[str], *, timeout: float = 120, env: dict[str, str] | None = None, input: str | None = None
) -> Result:
    """Run a command (argv never contains secrets by construction). Missing binaries return code 127."""
    try:
        p = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            input=input,
            check=False,
        )
    except FileNotFoundError:
        return Result(127, "", f"{argv[0]}: not found")
    except subprocess.TimeoutExpired:
        return Result(124, "", f"{argv[0]}: timed out after {int(timeout)} s")
    return Result(p.returncode, p.stdout, p.stderr)


def must(argv: list[str], what: str, **kw: Any) -> Result:
    r = run(argv, **kw)
    if r.code != 0:
        raise DeployError(f"{what} failed (exit {r.code}): {tail(r.err or r.out)}")
    return r


def tail(text: str, n: int = 6) -> str:
    return redact(" | ".join(line.strip() for line in text.strip().splitlines()[-n:] if line.strip()))


def sctl(paths: Paths, *args: str, timeout: float = 120) -> Result:
    return run(["systemctl", *(["--user"] if paths.user_mode else []), *args], timeout=timeout)


# -- HTTP (JSON APIs: Healthchecks.io, GitHub, the local backend) ---------------------------------------------------


class HttpError(DeployError):
    def __init__(self, message: str, status: int, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: Any = None,
    name: str,
    timeout: float = 15,
    retries: int = 3,
) -> tuple[int, Any]:
    """JSON request with bounded retries on transient failures only (network, 5xx, 429). 4xx other than 429 is
    returned to the caller immediately: retrying an invalid credential never helps."""
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Accept": "application/json", "User-Agent": "agentoffice-bootstrap", **headers}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    backoff = float(os.environ.get("AO_HTTP_BACKOFF", "1.0"))
    last = ""
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)  # noqa: S310 - https or loopback
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                raw = resp.read(1 << 20)
                return resp.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as e:
            raw = e.read(1 << 16)
            try:
                parsed = json.loads(raw) if raw.strip() else None
            except ValueError:
                parsed = None
            if e.code == 429 or e.code >= 500:
                last = f"HTTP {e.code}"
                wait = backoff * (2**attempt)
                if e.code == 429 and (ra := e.headers.get("Retry-After", "")).isdigit():
                    wait = min(int(ra), 30)
                if attempt < retries - 1:
                    time.sleep(wait)
                continue
            return e.code, parsed
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as e:
            reason = getattr(e, "reason", e)
            last = f"{type(e).__name__}: {reason}"
            if attempt < retries - 1:
                time.sleep(backoff * (2**attempt))
        except ValueError:
            raise DeployError(f"{name}: the response is not JSON") from None
    raise DeployError(f"{name} is unavailable after {retries} attempts ({redact(last)})")


def local_api(paths: Paths, port: int, method: str, path: str, token: str | None, body: Any = None) -> tuple[int, Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return http_json(method, f"http://127.0.0.1:{port}{path}", headers=headers, body=body, name="AgentOffice API")


def health(port: int, deep: bool = True, timeout: float = 5) -> dict[str, Any] | None:
    url = f"http://127.0.0.1:{port}/api/health" + ("?deep=1" if deep else "")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # loopback only
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        with contextlib.suppress(ValueError):
            return json.loads(e.read())
        return None
    except (OSError, ValueError):
        return None


def wait_healthy(port: int, secs: float = 40) -> bool:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        h = health(port)
        if h and h.get("ok"):
            return True
        time.sleep(1)
    return False


# -- state ----------------------------------------------------------------------------------------------------------


class State:
    """What finished, when (no secrets). Every stage re-detects reality anyway; this is for resumption reports."""

    def __init__(self, path: Path, enabled: bool) -> None:
        self.path, self.enabled = path, enabled
        self.data: dict[str, Any] = {"stages": {}}
        with contextlib.suppress(OSError, ValueError):
            self.data = json.loads(path.read_text("utf-8"))
        self.data.setdefault("stages", {})

    def mark(self, stage: str, status: str, detail: str = "") -> None:
        self.data["stages"][stage] = {"status": status, "at": now_iso(), "detail": redact(detail)[:500]}
        self.save()

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    def save(self) -> None:
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- context --------------------------------------------------------------------------------------------------------


@dataclass
class Ctx:
    cfg: dict[str, dict[str, Any]]
    cfg_path: Path | None
    secrets: dict[str, str]
    paths: Paths
    platform: Platform
    state: State
    dry_run: bool = False
    interactive: bool = False
    tailscale_script: bool = False  # --install-tailscale: run the official install script without asking
    python: str | None = None
    plan: list[str] = field(default_factory=list)
    ts: TailscaleState | None = None

    @property
    def port(self) -> int:
        return int(self.cfg["application"]["port"])

    @property
    def production(self) -> bool:
        return self.cfg["deployment"]["mode"] == "production"

    @property
    def tailscale(self) -> bool:
        return self.cfg["network"]["provider"] == "tailscale"

    @property
    def healthchecks(self) -> bool:
        return self.cfg["monitoring"]["provider"] == "healthchecks"

    @property
    def webhook(self) -> bool:
        return bool(self.cfg["github_webhook"]["enabled"])

    def change(self, description: str, fn: Callable[[], Any] | None = None) -> Any:
        """Every mutation goes through here: in --dry-run it is recorded in the plan and not executed."""
        if self.dry_run:
            self.plan.append(description)
            log(f"PLAN  {description}")
            return None
        log(f"DO    {description}")
        return fn() if fn else None

    def secret(self, key: str, prompt: str) -> str | None:
        if key in self.secrets:
            return self.secrets[key]
        if self.interactive and sys.stdin.isatty() and not self.dry_run:
            v = getpass.getpass(f"{prompt} (input hidden, Enter to skip): ").strip()
            register_secret(v)
            if v:
                self.secrets[key] = v
                return v
        return None

    @property
    def can_prompt(self) -> bool:
        return self.interactive and sys.stdin.isatty()

    def admin_token(self) -> str:
        t = env_get(self.paths.env_file, "AGENTLINK_ADMIN_TOKEN")
        register_secret(t)
        return t


# -- stage: precheck ------------------------------------------------------------------------------------------------


def reach(host: str, port: int = 443, timeout: float = 5) -> str | None:
    """None if host:port accepts a TCP connection, else a short reason (DNS vs. connectivity)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        return f"DNS resolution failed ({e.strerror})"
    for fam, typ, proto, _, addr in infos:
        with contextlib.suppress(OSError), socket.socket(fam, typ, proto) as s:
            s.settimeout(timeout)
            s.connect(addr)
            return None
    return "TCP connection failed"


def free_bytes(path: Path) -> int:
    p = path
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).free


def mem_total() -> int:
    with contextlib.suppress(OSError, ValueError, IndexError):
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    return 0


def precheck(ctx: Ctx) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    p = ctx.platform
    status = FAIL if not p.supported else PASS if p.tested else "WARN"
    rows.append(("Platform", status, f"{p.pretty} {p.arch}" + (f": {p.reason}" if p.reason else "")))
    if p.supported and not has_apt():
        missing = missing_commands()
        rows.append(
            (
                "Packages",
                FAIL if missing else PASS,
                f"no apt-get; missing: {', '.join(missing)} (install with the package manager)"
                if missing
                else "no apt-get; required commands present",
            )
        )
    ts_method = tailscale_install_method(ctx) if ctx.tailscale and not shutil.which("tailscale") else None
    if ts_method is not None:
        rows.append(
            {
                "script": ("Tailscale install", PASS, f"not installed: will run `{TAILSCALE_INSTALL_CMD}`"),
                "ask": (
                    "Tailscale install",
                    "WARN",
                    f"not installed: you will be asked to run `{TAILSCALE_INSTALL_CMD}`",
                ),
                "apt": ("Tailscale install", PASS, "not installed: will be installed from the official apt repository"),
                "": (
                    "Tailscale install",
                    FAIL,
                    "not installed and no official apt repository for this OS: re-run in a terminal to be asked, "
                    f"add --install-tailscale to run `{TAILSCALE_INSTALL_CMD}`, or see {TAILSCALE_LINUX_DOC}",
                ),
            }[ts_method]
        )
    if find_python(ctx) is None and not p.system_python_ok and p.libc != "glibc":
        rows.append(("Python", FAIL, f"no Python >= 3.12 and {p.libc} libc (uv needs glibc): install Python 3.12+"))
    if not ctx.paths.user_mode:
        rows.append(
            ("Root", PASS if os.geteuid() == 0 else FAIL, "running as root" if os.geteuid() == 0 else "run with sudo")
        )
        has_systemd = Path("/run/systemd/system").is_dir()
        rows.append(
            ("systemd", PASS if has_systemd else FAIL, "systemd is PID 1" if has_systemd else "systemd is required")
        )
    gib = 1 << 30
    for label, path in (("Disk (code)", ctx.paths.home), ("Disk (data)", ctx.paths.data)):
        free = free_bytes(path)
        rows.append(
            (label, PASS if free >= gib else FAIL, f"{free // (1 << 20)} MiB free near {path} (1024 MiB required)")
        )
    mem = mem_total()
    rows.append(("Memory", PASS if mem >= 512 << 20 else "WARN", f"{mem >> 20} MiB (512 MiB recommended)"))
    cfg_detail = f"{ctx.cfg_path}" if ctx.cfg_path else "built-in defaults"
    rows.append(("Configuration", PASS, f"valid ({cfg_detail}, mode {ctx.cfg['deployment']['mode']})"))

    urls: list[tuple[str, str]] = []
    if ts_method in ("script", "ask"):
        urls.append((os.environ.get("AO_TAILSCALE_INSTALL_URL", TAILSCALE_INSTALL_URL), "Tailscale install script"))
    if ts_method == "apt":
        urls.append((os.environ.get("AO_TAILSCALE_PKGS_URL", "https://pkgs.tailscale.com"), "Tailscale packages"))
    if find_python(ctx) is None and not p.system_python_ok and p.libc == "glibc":
        urls.append((os.environ.get("AO_UV_DOWNLOAD_URL", "https://github.com"), "uv / managed Python download"))
    if ctx.healthchecks:
        urls.append((ctx.cfg["monitoring"]["api_url"], "Healthchecks.io API"))
    if ctx.webhook and "github_token" in ctx.secrets:
        urls.append((github_api(), "GitHub API"))
    for url, why in urls:
        u = urllib.parse.urlsplit(url)
        host = u.hostname or ""
        err = reach(host, u.port or (80 if u.scheme == "http" else 443))
        rows.append((f"Network: {why}", FAIL if err else PASS, f"{host}: {err or 'reachable'}"))

    rows.extend(credential_prechecks(ctx))
    return rows


def credential_prechecks(ctx: Ctx) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    if ctx.tailscale:
        st = tailscale_state(ctx) if shutil.which("tailscale") else None
        if st and st.kind == "running":
            rows.append(("Tailscale credential", PASS, "device already registered: no auth key needed"))
        elif "tailscale_auth_key" in ctx.secrets:
            rows.append(
                ("Tailscale credential", NOT_VERIFIED, "auth key present; validity is only known at registration")
            )
        elif ctx.interactive and sys.stdin.isatty():
            rows.append(("Tailscale credential", NOT_VERIFIED, "auth key will be asked for"))
        else:
            rows.append(("Tailscale credential", FAIL, "tailscale_auth_key is required to register this device"))
    if ctx.healthchecks:
        configured = bool(env_get(ctx.paths.env_file, "HC_PING_URL"))
        key = ctx.secrets.get("healthchecks_api_key")
        if key:
            try:
                chans = Healthchecks(ctx.cfg["monitoring"]["api_url"], key).channels()
                rows.append(
                    (
                        "Healthchecks.io credential",
                        PASS if chans else FAIL,
                        f"API key accepted; {len(chans)} integration(s)"
                        + ("" if chans else ": add and verify one first"),
                    )
                )
            except DeployError as e:
                rows.append(("Healthchecks.io credential", FAIL, str(e)))
        elif configured:
            rows.append(("Healthchecks.io credential", PASS, "HC_PING_URL already configured: no API key needed"))
        elif ctx.interactive and sys.stdin.isatty():
            rows.append(("Healthchecks.io credential", NOT_VERIFIED, "API key will be asked for"))
        else:
            rows.append(("Healthchecks.io credential", FAIL, "healthchecks_api_key is required to create the check"))
    return rows


# -- stage: dependencies --------------------------------------------------------------------------------------------

BASE_PACKAGES = ["git", "curl", "ca-certificates", "gnupg", "iproute2", "python3"]


def missing_packages(pkgs: list[str]) -> list[str]:
    missing = []
    for pkg in pkgs:
        r = run(["dpkg-query", "-W", "-f=${Status}", pkg], timeout=30)
        if r.code != 0 or "install ok installed" not in r.out:
            missing.append(pkg)
    return missing


def apt_install(ctx: Ctx, pkgs: list[str], *, update: bool = True) -> None:
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    if update:
        must(["apt-get", "update", "-q"], "apt-get update", timeout=600, env=env)
    must(
        ["apt-get", "install", "-y", "-q", "--no-install-recommends", *pkgs],
        f"apt-get install {' '.join(pkgs)}",
        timeout=1200,
        env=env,
    )


# command that proves each package is there, for systems without apt/dpkg (gnupg is only needed by the Tailscale apt
# repository, ca-certificates is checked by the TLS connections themselves)
PACKAGE_COMMANDS = {"git": "git", "curl": "curl", "iproute2": "ip", "python3": "python3"}
TAILSCALE_LINUX_DOC = "https://tailscale.com/kb/1031/install-linux"
TAILSCALE_INSTALL_URL = "https://tailscale.com/install.sh"
TAILSCALE_INSTALL_CMD = f"curl -fsSL {TAILSCALE_INSTALL_URL} | sh"


def has_apt() -> bool:
    return bool(shutil.which("apt-get") and shutil.which("dpkg-query"))


def missing_commands() -> list[str]:
    return [cmd for cmd in PACKAGE_COMMANDS.values() if not shutil.which(cmd)]


def stage_dependencies(ctx: Ctx) -> None:
    if not has_apt():
        missing = missing_commands()
        if missing:
            raise DeployError(
                f"required commands not found: {', '.join(missing)} (no apt-get on this system to install them)",
                hint="install them with the distribution's package manager (e.g. dnf, zypper, pacman), then re-run",
            )
        log("required commands present (no apt-get: packages are managed by the distribution): "
            + ", ".join(PACKAGE_COMMANDS.values()))  # fmt: skip
        return
    missing = missing_packages(BASE_PACKAGES)
    if not missing:
        log("system packages present: " + ", ".join(BASE_PACKAGES))
        return
    ctx.change(f"apt-get install {' '.join(missing)}", lambda: apt_install(ctx, missing))


# -- stage: python --------------------------------------------------------------------------------------------------

PROBE = (
    "import json, sqlite3, sys\n"
    "try:\n    import ensurepip, venv  # noqa: F401\n    ok = True\nexcept Exception:\n    ok = False\n"
    "print(json.dumps({'version': list(sys.version_info[:3]), 'sqlite': list(sqlite3.sqlite_version_info),"
    " 'venv': ok, 'executable': sys.executable}))\n"
)


@dataclass
class PyInfo:
    path: str
    version: tuple[int, ...]
    sqlite: tuple[int, ...]
    venv: bool

    @property
    def compatible(self) -> bool:
        return self.version >= (3, 12) and self.sqlite >= (3, 35) and self.venv

    def describe(self) -> str:
        v = ".".join(map(str, self.version))
        s = ".".join(map(str, self.sqlite))
        problems = []
        if self.version < (3, 12):
            problems.append("Python < 3.12")
        if self.sqlite < (3, 35):
            problems.append("SQLite < 3.35")
        if not self.venv:
            problems.append("venv/ensurepip missing")
        return f"{self.path}: Python {v}, SQLite {s}" + (f" ({', '.join(problems)})" if problems else "")


def probe_python(path: str) -> PyInfo | None:
    r = run([path, "-I", "-c", PROBE], timeout=30)
    if r.code != 0:
        return None
    try:
        d = json.loads(r.out.strip().splitlines()[-1])
        return PyInfo(str(d["executable"]), tuple(d["version"]), tuple(d["sqlite"]), bool(d["venv"]))
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def python_candidates(ctx: Ctx) -> list[str]:
    out: list[str] = []
    rec = ""
    with contextlib.suppress(OSError):
        for line in ctx.paths.deploy_conf.read_text("utf-8").splitlines():
            if line.startswith("AO_PYTHON="):
                rec = line.split("=", 1)[1].strip()
    if rec:
        out.append(rec)
    managed = sorted(ctx.paths.home.glob("python/cpython-3.*/bin/python3"), reverse=True)
    out.extend(str(p) for p in managed)
    for name in ("python3.14", "python3.13", "python3.12", "python3"):
        w = shutil.which(name)
        if w:
            out.append(w)
    seen: set[str] = set()
    return [c for c in out if not (c in seen or seen.add(c))]  # type: ignore[func-returns-value]


def find_python(ctx: Ctx) -> PyInfo | None:
    explicit = ctx.cfg["python"]["interpreter"]
    for c in [explicit] if explicit else python_candidates(ctx):
        info = probe_python(c)
        if info and info.compatible:
            return info
    return None


def install_uv(ctx: Ctx) -> Path:
    arch = ctx.platform.arch
    dest = ctx.paths.home / "uv" / UV_VERSION / "uv"
    if dest.is_file() and run([str(dest), "--version"], timeout=30).out.split()[1:2] == [UV_VERSION]:
        return dest
    base = os.environ.get("AO_UV_DOWNLOAD_URL", "https://github.com/astral-sh/uv/releases/download")  # tests override
    name = f"uv-{arch}-unknown-linux-gnu"
    url = f"{check_api_url(base, 'uv download URL')}/{UV_VERSION}/{name}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="ao-uv-") as td:
        tgz = Path(td) / f"{name}.tar.gz"
        download(url, tgz)
        digest = hashlib.sha256(tgz.read_bytes()).hexdigest()
        expected = os.environ.get("AO_UV_SHA256") or UV_SHA256[arch]  # tests override
        if digest != expected:
            raise DeployError(f"uv download checksum mismatch ({digest} != pinned {expected}); nothing was installed")
        with tarfile.open(tgz) as tf:
            member = tf.getmember(f"{name}/uv")
            if not member.isfile():
                raise DeployError("unexpected uv archive layout")
            src = tf.extractfile(member)
            assert src is not None
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".tmp")
            with open(tmp, "wb") as f:
                shutil.copyfileobj(src, f)
            os.chmod(tmp, 0o755)
            os.replace(tmp, dest)
    return dest


def download(url: str, dest: Path, timeout: float = 120) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "agentoffice-bootstrap"})  # noqa: S310 - https/loopback
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:  # noqa: S310
            shutil.copyfileobj(r, f)
    except (urllib.error.URLError, OSError) as e:
        raise DeployError(f"download of {url} failed: {getattr(e, 'reason', e)}") from None


def uv_python(ctx: Ctx) -> str:
    uv = install_uv(ctx)
    version = ctx.cfg["python"]["version"]
    with tempfile.TemporaryDirectory(prefix="ao-uv-cache-") as cache:
        env = {
            **os.environ,
            "UV_PYTHON_INSTALL_DIR": str(ctx.paths.home / "python"),
            "UV_CACHE_DIR": cache,
            "UV_NO_CONFIG": "1",
        }
        must([str(uv), "python", "install", "--no-bin", version], f"uv python install {version}", env=env, timeout=900)
        r = must([str(uv), "python", "find", "--managed-python", version], "uv python find", env=env, timeout=60)
    path = r.out.strip().splitlines()[-1] if r.out.strip() else ""
    if not path.startswith(str(ctx.paths.home / "python")):
        raise DeployError(f"uv returned an interpreter outside {ctx.paths.home}/python: {path!r}")
    return path


def stage_python(ctx: Ctx) -> None:
    explicit = ctx.cfg["python"]["interpreter"]
    found = find_python(ctx)
    if found:
        ctx.python = found.path
        log(f"Python: {found.describe()}")
        return
    if explicit:
        info = probe_python(explicit)
        raise DeployError(
            f"python.interpreter is not usable: {info.describe() if info else explicit + ' does not run'}"
        )
    if ctx.platform.libc != "glibc":
        raise DeployError(
            f"no Python >= 3.12 found and this system uses {ctx.platform.libc} libc: the pinned uv build needs glibc",
            hint="install Python 3.12+ with venv and SQLite >= 3.35 from the package manager (or set "
            "python.interpreter), then re-run",
        )
    if ctx.platform.system_python_ok:
        ctx.change(
            "apt-get install python3 python3-venv (distribution Python >= 3.12)",
            lambda: apt_install(ctx, ["python3", "python3-venv"]),
        )
    else:
        ctx.change(
            f"install uv {UV_VERSION} (pinned SHA-256) and a managed CPython {ctx.cfg['python']['version']} under "
            f"{ctx.paths.home}/python (the system Python is not touched)",
            lambda: uv_python(ctx),
        )
    if ctx.dry_run:
        return
    found = find_python(ctx)
    if not found:
        raise DeployError("no compatible Python after provisioning (Python >= 3.12 with venv and SQLite >= 3.35)")
    ctx.python = found.path
    log(f"Python: {found.describe()}")


# -- stage: install (deployment/install.sh, unchanged logic) --------------------------------------------------------


def installed_port(paths: Paths) -> int | None:
    url = env_get(paths.env_file, "AO_URL")
    m = re.match(r"^http://127\.0\.0\.1:(\d+)$", url)
    return int(m.group(1)) if m else None


def units_present(paths: Paths) -> bool:
    return all((paths.units / u).is_file() for u in UNITS)


UNITS = [
    "agent-office.service",
    "agent-office-healthcheck.service",
    "agent-office-healthcheck.timer",
    "agent-office-backup.service",
    "agent-office-backup.timer",
]


def stage_install(ctx: Ctx) -> None:
    p = ctx.paths
    current = p.home / "current"
    have = current.exists() and p.env_file.is_file()
    if have:
        port = installed_port(p)
        if port is not None and port != ctx.port:
            raise DeployError(
                f"AgentOffice is installed on port {port} but the configuration says {ctx.port}",
                hint="set application.port to the installed port (changing the port of an install is not automated)",
            )
        if units_present(p) and wait_healthy(ctx.port, 5):
            log(
                f"AgentOffice already installed and healthy ({current.resolve().name}); kept as is (updates: update.sh)"
            )
            persist_config(ctx)
            return
        log("an existing install is incomplete or unhealthy: re-running install.sh (keeps the database and secrets)")
    py = ctx.python or "<interpreter from the python stage>"
    ref = ctx.cfg["application"]["git_ref"]

    def do() -> None:
        env = {
            **os.environ,
            "PYTHON": py,
            "AO_PORT": str(ctx.port),
            "AO_URL": f"http://127.0.0.1:{ctx.port}",
            "AO_SHOW_TOKEN": "0",
        }
        r = run(["bash", str(HERE / "install.sh"), ref], env=env, timeout=1800)
        ctx.admin_token()  # register the (possibly new) token for redaction before logging any output
        for line in (r.out + r.err).splitlines():
            log(f"install.sh: {line}")
        if r.code != 0:
            raise DeployError(f"install.sh failed (exit {r.code})", hint="journalctl -u agent-office -n 50")

    ctx.change(f"run deployment/install.sh {ref} with PYTHON={py}, port {ctx.port}", do)
    if not ctx.dry_run:
        persist_config(ctx)


def persist_config(ctx: Ctx) -> None:
    """Keep the configuration used next to the env file so `--verify` and re-runs find it from anywhere."""
    if ctx.cfg_path is None:
        return
    dest = ctx.paths.etc / "deployment.toml"
    text = ctx.cfg_path.read_text("utf-8")
    if dest.is_file() and dest.read_text("utf-8") == text:
        return
    with contextlib.suppress(OSError):
        if dest.resolve() == ctx.cfg_path.resolve():
            return
    tmp = dest.with_name(".deployment.toml.tmp")
    tmp.write_text(text, "utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, dest)
    log(f"configuration saved to {dest}")


# -- Tailscale ------------------------------------------------------------------------------------------------------


@dataclass
class TailscaleState:
    kind: str  # running | needs_login | needs_approval | stopped | starting | no_daemon | unknown
    backend: str = ""
    dns_name: str = ""
    ips: list[str] = field(default_factory=list)
    tailnet: str = ""
    magic_suffix: str = ""
    magicdns: bool = False
    cert_domains: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    hostname: str = ""
    online: bool = False
    health: list[str] = field(default_factory=list)
    detail: str = ""

    def in_tailnet(self, expected: str) -> bool:
        return not expected or expected.lower() in (self.tailnet.lower(), self.magic_suffix.lower())

    @property
    def https_enabled(self) -> bool:
        return bool(self.cert_domains) and any(self.dns_name == d.rstrip(".") for d in self.cert_domains)


def classify_tailscale(status: dict[str, Any]) -> TailscaleState:
    """Interpret `tailscale status --json` (the format is documented as subject to change: read defensively)."""
    backend = str(status.get("BackendState", ""))
    me = status.get("Self") or {}
    tn = status.get("CurrentTailnet") or {}
    st = TailscaleState(
        kind={
            "Running": "running",
            "NeedsLogin": "needs_login",
            "NeedsMachineAuth": "needs_approval",
            "Stopped": "stopped",
            "Starting": "starting",
            "NoState": "starting",
        }.get(backend, "unknown"),
        backend=backend,
        dns_name=str(me.get("DNSName", "")).rstrip("."),
        ips=[str(x) for x in (me.get("TailscaleIPs") or status.get("TailscaleIPs") or [])],
        tailnet=str(tn.get("Name", "")),
        magic_suffix=str(tn.get("MagicDNSSuffix", status.get("MagicDNSSuffix", ""))),
        magicdns=bool(tn.get("MagicDNSEnabled", False)),
        cert_domains=[str(x).rstrip(".") for x in (status.get("CertDomains") or [])],
        tags=[str(x) for x in (me.get("Tags") or [])],
        hostname=str(me.get("HostName", "")),
        online=bool(me.get("Online", False)),
        health=[str(x) for x in (status.get("Health") or [])],
    )
    if st.kind == "running" and st.dns_name == "":
        st.kind, st.detail = "starting", "running but without a DNS name yet"
    return st


def tailscale_state(ctx: Ctx) -> TailscaleState:
    r = run(["tailscale", "status", "--json"], timeout=30)
    if r.code == 127:
        return TailscaleState("absent", detail="tailscale is not installed")
    try:
        data = json.loads(r.out)
    except ValueError:
        low = (r.err + r.out).lower()
        if "doesn't appear to be running" in low or "connect" in low or "tailscaled" in low:
            return TailscaleState("no_daemon", detail=tail(r.err or r.out))
        return TailscaleState("unknown", detail=tail(r.err or r.out))
    return classify_tailscale(data)


def diagnose_up(err: str, tag: str) -> DeployError:
    low = err.lower()
    if "tag" in low and ("not permitted" in low or "invalid" in low or "owner" in low):
        return DeployError(
            f"Tailscale refused the tag {tag}: {tail(err, 3)}",
            hint=f"declare {tag} in tagOwners of the tailnet policy and create the auth key with that tag ({DOC['tags']})",
        )
    if "invalid key" in low or "expired" in low or "not valid" in low or "authkey" in low or "auth key" in low:
        return DeployError(
            f"Tailscale rejected the auth key (invalid, expired, revoked or a one-off key already used): {tail(err, 3)}",
            hint=f"create a new pre-approved, tagged auth key ({DOC['auth_keys']}) and put it in the secrets file",
        )
    if "no such host" in low or "dns" in low or "lookup" in low:
        return DeployError(
            f"DNS problem while contacting Tailscale: {tail(err, 3)}", hint="check /etc/resolv.conf and outbound DNS"
        )
    if "timeout" in low or "deadline" in low or "timed out" in low:
        return DeployError(
            f"timed out contacting the Tailscale control plane: {tail(err, 3)}",
            hint="check outbound HTTPS (443) and UDP 41641, then re-run",
        )
    if "permission denied" in low or "access denied" in low or "operation not permitted" in low:
        return DeployError(f"permission problem running tailscale: {tail(err, 3)}", hint="run the bootstrap with sudo")
    return DeployError(f"tailscale up failed: {tail(err, 4)}")


def tailscale_install_method(ctx: Ctx) -> str:
    """How a missing Tailscale gets installed: "script" (--install-tailscale), "ask" (a terminal: the operator
    confirms the official script), "apt" (unattended, pinned apt repository) or "" (no way: fail)."""
    if ctx.tailscale_script:
        return "script"
    if ctx.can_prompt:
        return "ask"
    if ctx.platform.apt_distro and has_apt():
        return "apt"
    return ""


def install_tailscale_script(ctx: Ctx) -> None:
    """Run the official installer exactly as documented: curl -fsSL https://tailscale.com/install.sh | sh."""
    url = check_api_url(os.environ.get("AO_TAILSCALE_INSTALL_URL", TAILSCALE_INSTALL_URL), "Tailscale install URL")
    # pipefail: a failed download must not run as an empty (successful) script; the URL is an argument, not code
    must(["bash", "-c", 'set -o pipefail; curl -fsSL "$1" | sh', "tailscale-install", url],
         "the Tailscale install script", timeout=900)  # fmt: skip
    if not shutil.which("tailscale"):
        raise DeployError(
            "the Tailscale install script finished but the tailscale command is not on PATH",
            hint=f"install it by hand ({TAILSCALE_LINUX_DOC}) and re-run",
        )


def install_tailscale(ctx: Ctx) -> None:
    plat = ctx.platform
    if not plat.apt_distro or not has_apt():
        raise DeployError(
            f"Tailscale is not installed and {plat.pretty} has no official Tailscale apt repository the bootstrap "
            "can use",
            hint=f"install Tailscale from the official instructions ({TAILSCALE_LINUX_DOC}), then re-run",
        )
    base = check_api_url(
        os.environ.get("AO_TAILSCALE_PKGS_URL", "https://pkgs.tailscale.com/stable"), "Tailscale packages URL"
    )
    keyring = Path(os.environ.get("AO_APT_KEYRING", "/usr/share/keyrings/tailscale-archive-keyring.gpg"))
    sources = Path(os.environ.get("AO_APT_SOURCES", "/etc/apt/sources.list.d/tailscale.list"))
    expected_fpr = os.environ.get("AO_TAILSCALE_KEY_FPR", TAILSCALE_KEY_FPR)  # tests override
    with tempfile.TemporaryDirectory(prefix="ao-ts-") as td:
        key = Path(td) / "key.gpg"
        download(f"{base}/{plat.apt_distro}/{plat.codename}.noarmor.gpg", key)
        home = Path(td) / "gnupg"
        home.mkdir(mode=0o700)
        r = must(
            ["gpg", "--homedir", str(home), "--batch", "--show-keys", "--with-colons", str(key)], "gpg --show-keys"
        )
        fprs = [line.split(":")[9] for line in r.out.splitlines() if line.startswith("fpr:")]
        if not fprs or fprs[0] != expected_fpr:
            raise DeployError(
                f"the Tailscale repository key has an unexpected fingerprint ({fprs[0] if fprs else 'none'}); "
                "nothing was installed",
                hint=f"compare with {base}/ and update TAILSCALE_KEY_FPR only after checking it",
            )
        keyring.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(key, keyring)
        os.chmod(keyring, 0o644)
    line = f"deb [signed-by={keyring}] {base}/{plat.apt_distro} {plat.codename} main\n"
    if not sources.exists():
        sources.parent.mkdir(parents=True, exist_ok=True)
        sources.write_text(f"# Tailscale packages (added by AgentOffice bootstrap)\n{line}", "utf-8")
        os.chmod(sources, 0o644)
    apt_install(ctx, ["tailscale"])
    if not shutil.which("tailscale"):
        raise DeployError("the tailscale package was installed but the tailscale command is not on PATH")


def ensure_tailscaled(ctx: Ctx) -> None:
    if ctx.paths.user_mode:
        return  # the sandbox has no system daemon (tests use a fake tailscale)
    r = sctl(ctx.paths, "is-active", "tailscaled")
    if r.out.strip() != "active":
        ctx.change(
            "systemctl enable --now tailscaled",
            lambda: must(["systemctl", "enable", "--now", "tailscaled"], "start tailscaled"),
        )


def wait_tailscale(ctx: Ctx, secs: float = 60) -> TailscaleState:
    end = time.monotonic() + secs
    st = tailscale_state(ctx)
    while st.kind in ("starting", "no_daemon") and time.monotonic() < end:
        time.sleep(2)
        st = tailscale_state(ctx)
    return st


def stage_tailscale(ctx: Ctx) -> None:
    if not ctx.tailscale:
        log("network.provider = none: Tailscale skipped (development mode)")
        return
    n = ctx.cfg["network"]
    if not shutil.which("tailscale"):
        method = tailscale_install_method(ctx)
        if method == "ask" and not ctx.dry_run:
            if not yes(f"Tailscale is not installed and this deployment needs it. Install it now with "
                       f"`{TAILSCALE_INSTALL_CMD}`?", True):  # fmt: skip
                raise DeployError(
                    "Tailscale is required (network.provider = 'tailscale') and was not installed",
                    hint=f"install it (`{TAILSCALE_INSTALL_CMD}`) and re-run, or use network.provider = 'none' "
                    "(development only)",
                )
            method = "script"
        if method in ("script", "ask"):
            ctx.change(
                f"install Tailscale with the official script (`{TAILSCALE_INSTALL_CMD}`)"
                + (", after confirmation" if method == "ask" else ""),
                lambda: install_tailscale_script(ctx),
            )
        elif method == "apt":
            ctx.change(
                f"install Tailscale from the official apt repository ({ctx.platform.apt_distro} "
                f"{ctx.platform.codename}, signing key fingerprint pinned)",
                lambda: install_tailscale(ctx),
            )
        else:
            raise DeployError(
                f"Tailscale is not installed and {ctx.platform.pretty} has no official Tailscale apt repository",
                hint=f"re-run in a terminal to be asked, add --install-tailscale to run `{TAILSCALE_INSTALL_CMD}`, "
                f"or install it by hand ({TAILSCALE_LINUX_DOC})",
            )
        if ctx.dry_run:
            ctx.plan.append(
                f"register this device with the auth key (tag {n['tag']}, hostname {ctx.cfg['deployment']['hostname']})"
            )
            return
    ensure_tailscaled(ctx)
    st = wait_tailscale(ctx, 5 if ctx.dry_run else 60)
    if st.kind == "running":
        keep_identity(ctx, st)
        return
    if st.kind == "needs_approval":
        raise DeployError(
            "this device is registered but waiting for approval in the Tailscale admin console",
            hint=f"approve it (Machines page) or use a pre-approved auth key ({DOC['device_approval']}), then re-run",
        )
    if st.kind == "stopped":
        raise DeployError(
            "Tailscale is logged in but stopped (someone ran `tailscale down`); the bootstrap does not override that",
            hint="bring it up with `sudo tailscale up` (with your usual flags) and re-run",
        )
    if st.kind != "needs_login":
        raise DeployError(
            f"unexpected Tailscale state {st.backend or st.kind}: {st.detail}", hint="sudo systemctl status tailscaled"
        )
    key = ctx.secret("tailscale_auth_key", "Tailscale auth key")
    hostname = ctx.cfg["deployment"]["hostname"]
    if ctx.dry_run:
        ctx.plan.append(
            f"tailscale up --auth-key=file:<private temp file> --advertise-tags={n['tag']} --hostname={hostname}"
        )
        return
    if not key:
        raise DeployError(
            "this device is not in a tailnet and no tailscale_auth_key was provided",
            hint=f"create a pre-approved auth key with tag {n['tag']} ({DOC['auth_keys']}) and add it to the secrets file",
        )

    def up() -> None:
        with secret_file(key) as kf:
            r = run(
                [
                    "tailscale",
                    "up",
                    f"--auth-key=file:{kf}",
                    f"--advertise-tags={n['tag']}",
                    f"--hostname={hostname}",
                    "--timeout=90s",
                ],
                timeout=120,
            )
        if r.code != 0:
            raise diagnose_up(r.err + r.out, n["tag"])

    ctx.change(
        f"register with Tailscale (tag {n['tag']}, hostname {hostname}; key read from a private temporary file)", up
    )
    st = wait_tailscale(ctx, 60)
    if st.kind == "needs_approval":
        raise DeployError(
            "registered, but the device must be approved in the Tailscale admin console",
            hint=f"approve it, or use a pre-approved key next time ({DOC['device_approval']}); then re-run",
        )
    if st.kind != "running":
        raise DeployError(f"Tailscale did not reach Running after registration (state {st.backend or st.kind})")
    keep_identity(ctx, st)


def keep_identity(ctx: Ctx, st: TailscaleState) -> None:
    n = ctx.cfg["network"]
    if not st.in_tailnet(n["tailnet"]):
        raise DeployError(
            f"this device belongs to tailnet {st.tailnet or st.magic_suffix!r}, not the configured {n['tailnet']!r}; "
            "nothing was changed",
            hint="fix network.tailnet, or (if the device really must move) `sudo tailscale logout` and re-run with a key "
            "of the right tailnet",
        )
    if n["tag"] not in st.tags:
        log(f"WARNING: the device is not tagged {n['tag']} (tags: {st.tags or 'none'}); its identity is kept as is")
    ctx.ts = st
    ctx.state.set("tailscale", {"dns_name": st.dns_name, "tailnet": st.tailnet or st.magic_suffix, "tags": st.tags})
    log(f"Tailscale: {st.dns_name} in {st.tailnet or st.magic_suffix} ({', '.join(st.ips)})")


# -- Serve / Funnel -------------------------------------------------------------------------------------------------


def serve_status() -> dict[str, Any]:
    r = run(["tailscale", "serve", "status", "--json"], timeout=30)
    if r.code != 0:
        raise DeployError(f"reading the Tailscale Serve status failed: {tail(r.err or r.out)}")
    out = r.out.strip()
    if not out or out.startswith("No serve config"):
        return {}
    try:
        data = json.loads(out)
    except ValueError:
        raise DeployError("the Tailscale Serve status (JSON) could not be parsed") from None
    return data if isinstance(data, dict) else {}


def classify_port(serve: dict[str, Any], dns_name: str, port: int, target: str, *, funnel: bool) -> tuple[str, str]:
    """('absent'|'ours'|'conflict'|'exposed', detail) for one HTTPS port of the Serve/Funnel config.

    'ours' means exactly: HTTPS on `port`, a single "/" handler proxying to `target`, and Funnel on iff `funnel`.
    'exposed' is the dangerous case of the API being on Funnel."""
    tcp = (serve.get("TCP") or {}).get(str(port))
    hostport = f"{dns_name}:{port}"
    web = (serve.get("Web") or {}).get(hostport)
    allow_funnel = bool((serve.get("AllowFunnel") or {}).get(hostport))
    if tcp is None and web is None:
        return ("exposed", "Funnel enabled without a handler") if allow_funnel and not funnel else ("absent", "")
    if tcp and not tcp.get("HTTPS"):
        return "conflict", f"port {port} is used for a raw TCP forward ({tcp})"
    handlers = (web or {}).get("Handlers") or {}
    proxies = {path: (h or {}).get("Proxy") for path, h in handlers.items()}
    accepted = {target, target.replace("127.0.0.1", "localhost"), target + "/"}
    if allow_funnel and not funnel:
        return "exposed", f"port {port} is published on Tailscale Funnel"
    if set(proxies) != {"/"} or proxies.get("/") not in accepted:
        return "conflict", f"port {port} already serves {proxies or handlers}"
    if funnel and not allow_funnel:
        return "conflict", f"port {port} serves {target} on the tailnet only (Funnel off)"
    return "ours", f"https://{dns_name}{'' if port == 443 else f':{port}'}/ -> {target}"


def funnel_ports(serve: dict[str, Any]) -> list[str]:
    return sorted(hp for hp, on in (serve.get("AllowFunnel") or {}).items() if on)


def stage_https(ctx: Ctx) -> None:
    if not ctx.tailscale:
        return
    st = ctx.ts
    if st is None:
        if ctx.dry_run:
            ctx.plan.append(
                " ".join(
                    [
                        "tailscale",
                        "serve",
                        "--bg",
                        f"--https={ctx.cfg['network']['https_port']}",
                        f"http://127.0.0.1:{ctx.port}",
                    ]
                )
            )
            return
        raise DeployError("Tailscale is not running: HTTPS cannot be configured")
    if not st.magicdns:
        raise DeployError(
            "MagicDNS is disabled in this tailnet",
            hint=f"enable MagicDNS on the DNS page of the admin console ({DOC['https']})",
        )
    if not st.https_enabled:
        raise DeployError(
            "HTTPS certificates are not enabled in this tailnet",
            hint=f"admin console → DNS → HTTPS Certificates → Enable HTTPS ({DOC['https']}), then re-run",
        )
    https_port = int(ctx.cfg["network"]["https_port"])
    target = f"http://127.0.0.1:{ctx.port}"
    kind, detail = classify_port(serve_status(), st.dns_name, https_port, target, funnel=False)
    if kind == "ours":
        log(f"Tailscale Serve already configured: {detail}")
        return
    if kind == "exposed":
        raise DeployError(
            f"SECURITY: {detail}; the AgentOffice API must never be public",
            hint="run: sudo "
            + " ".join(["tailscale", "funnel", f"--https={https_port}", "off"])
            + "  (not changed for you)",
        )
    if kind == "conflict":
        raise DeployError(
            f"Tailscale Serve conflict: {detail}; not overwritten",
            hint="choose another network.https_port or remove that mapping yourself, then re-run",
        )
    argv = ["tailscale", "serve", "--bg", f"--https={https_port}", target]

    def apply() -> None:
        r = run(argv, timeout=90)
        if r.code != 0:
            low = (r.err + r.out).lower()
            hint = f"enable HTTPS certificates ({DOC['https']})" if "https" in low or "cert" in low else ""
            raise DeployError(f"{' '.join(argv)} failed: {tail(r.err or r.out)}", hint=hint)

    ctx.change(" ".join(argv) + "  (private to the tailnet)", apply)
    if not ctx.dry_run:
        kind, detail = classify_port(serve_status(), st.dns_name, https_port, target, funnel=False)
        if kind != "ours":
            raise DeployError(f"Tailscale Serve is not in the expected state after configuring it: {detail or kind}")
        log(f"Tailscale Serve: {detail}")


# -- GitHub webhook (optional; reuses the webhook-only listener of agentlink/serve.py) ------------------------------


def github_api() -> str:
    return check_api_url(os.environ.get("AO_GITHUB_API_URL", "https://api.github.com"), "GitHub API URL")


def payload_url(ctx: Ctx) -> str:
    dns = ctx.ts.dns_name if ctx.ts else "<device>.<tailnet>.ts.net"
    return f"https://{dns}:{ctx.cfg['github_webhook']['funnel_port']}/api/github/webhook"


def stage_github_webhook(ctx: Ctx) -> None:
    if not ctx.webhook:
        return
    g = ctx.cfg["github_webhook"]
    envf = ctx.paths.env_file
    changed = False
    secret = env_get(envf, "GITHUB_WEBHOOK_SECRET")
    register_secret(secret)
    new_secret = False
    if not secret:
        secret = ctx.secrets.get("github_webhook_secret") or pysecrets.token_hex(32)
        register_secret(secret)
        new_secret = True
        changed |= bool(
            ctx.change(
                f"store a GitHub webhook secret in {envf}", lambda: env_set(envf, "GITHUB_WEBHOOK_SECRET", secret)
            )
        )
    if env_get(envf, "AGENTLINK_WEBHOOK_PORT") != str(g["port"]):
        changed |= bool(
            ctx.change(
                f"set AGENTLINK_WEBHOOK_PORT={g['port']} in {envf}",
                lambda: env_set(envf, "AGENTLINK_WEBHOOK_PORT", str(g["port"])),
            )
        )
    if changed or (not ctx.dry_run and not listener_filters_webhook(g["port"])):

        def restart() -> None:
            sctl(ctx.paths, "restart", "agent-office.service")
            if not wait_healthy(ctx.port, 40):
                raise DeployError(
                    "agent-office did not become healthy after enabling the webhook listener",
                    hint="journalctl -u agent-office -n 50",
                )

        ctx.change("restart agent-office.service (opens the webhook-only loopback listener)", restart)
    if not ctx.dry_run and not listener_filters_webhook(g["port"]):
        raise DeployError(f"the webhook-only listener on 127.0.0.1:{g['port']} is not answering as expected")
    if ctx.ts is None:
        ctx.plan.append(f"publish ONLY the webhook listener {g['port']} on Funnel port {g['funnel_port']}")
        return
    target = f"http://127.0.0.1:{g['port']}"
    kind, detail = classify_port(serve_status(), ctx.ts.dns_name, g["funnel_port"], target, funnel=True)
    if kind == "conflict":
        raise DeployError(
            f"Funnel port {g['funnel_port']}: {detail}; not overwritten", hint="pick another github_webhook.funnel_port"
        )
    if kind != "ours":
        argv = ["tailscale", "funnel", "--bg", f"--https={g['funnel_port']}", target]

        def apply() -> None:
            r = run(argv, timeout=90)
            if r.code != 0:
                raise DeployError(
                    f"{' '.join(argv)} failed: {tail(r.err or r.out)}",
                    hint=f"the tailnet policy needs the `funnel` node attribute for this device ({DOC['funnel']})",
                )

        ctx.change(" ".join(argv) + "  (webhook-only listener; the API stays private)", apply)
    ensure_repositories(ctx, secret, new_secret)


def listener_filters_webhook(port: int) -> bool:
    """The public listener must 404 everything but the webhook and refuse unsigned deliveries."""
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/api/health")
        other = c.getresponse().status
        c.close()
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/api/github/webhook", body=b"{}", headers={"Content-Type": "application/json"})
        unsigned = c.getresponse().status
        c.close()
    except OSError:
        return False
    return other == 404 and unsigned == 401


def ensure_repositories(ctx: Ctx, secret: str, new_secret: bool) -> None:
    repos = ctx.cfg["github_webhook"]["repositories"]
    if ctx.dry_run and not health(ctx.port, deep=False):
        for r in repos:
            ctx.plan.append(f"link channel {r.get('channel') or default_channel(r['repo'])} to {r['repo']} (+ webhook)")
        return
    token = ctx.admin_token()
    gh = ctx.secrets.get("github_token")
    results = []
    for r in repos:
        repo, channel = r["repo"], r.get("channel") or default_channel(r["repo"])
        ensure_channel(ctx, token, channel, repo)
        if gh:
            results.append((repo, ensure_hook(ctx, gh, repo, secret, new_secret)))
    ctx.state.set("github_webhook", {"payload_url": payload_url(ctx), "hooks": dict(results)})
    if not gh and repos:
        log(
            "GitHub webhooks were not registered (no github_token): add them in each repository → Settings → Webhooks, "
            f"payload URL {payload_url(ctx)}, content type application/json, secret = GITHUB_WEBHOOK_SECRET in "
            f"{ctx.paths.env_file}"
        )


def ensure_channel(ctx: Ctx, token: str, channel: str, repo: str) -> None:
    code, body = local_api(ctx.paths, ctx.port, "GET", f"/api/channels/{channel}", token)
    if code == 200:
        have = (body or {}).get("repo") or ""
        if have.lower() != repo.lower():
            raise DeployError(
                f"channel {channel} exists and is linked to {have or 'no repository'}, not {repo}; not changed"
            )
        return
    if code != 404:
        raise DeployError(f"AgentOffice API: GET /api/channels/{channel} returned {code}")

    def create() -> None:
        c, b = local_api(ctx.paths, ctx.port, "POST", "/api/channels", token, {"name": channel, "repo": repo})
        if c not in (200, 201):
            raise DeployError(
                f"could not create channel {channel}: HTTP {c} {((b or {}).get('error') or {}).get('message', '')}"
            )

    ctx.change(f"create channel {channel} linked to {repo}", create)


def ensure_hook(ctx: Ctx, token: str, repo: str, secret: str, new_secret: bool) -> str:
    api = github_api()
    h = {
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "Accept": "application/vnd.github+json",
    }
    url = payload_url(ctx)
    code, hooks = http_json("GET", f"{api}/repos/{repo}/hooks?per_page=100", headers=h, name="GitHub API")
    if code in (401, 403, 404):
        raise DeployError(
            f"GitHub API refused to list the webhooks of {repo} (HTTP {code})",
            hint="the github_token needs repository 'Webhooks: read and write' (fine-grained) or admin:repo_hook",
        )
    if code != 200 or not isinstance(hooks, list):
        raise DeployError(f"GitHub API: unexpected answer listing hooks of {repo} (HTTP {code})")
    mine = [x for x in hooks if isinstance(x, dict) and (x.get("config") or {}).get("url") == url]
    config = {"url": url, "content_type": "json", "secret": secret, "insecure_ssl": "0"}
    if mine:
        hook = mine[0]
        want_events = set(WEBHOOK_EVENTS)
        if new_secret or not hook.get("active") or set(hook.get("events") or []) != want_events:

            def patch() -> None:
                c, _ = http_json(
                    "PATCH",
                    f"{api}/repos/{repo}/hooks/{hook['id']}",
                    headers=h,
                    body={"active": True, "events": WEBHOOK_EVENTS, "config": config},
                    name="GitHub API",
                )
                if c != 200:
                    raise DeployError(f"GitHub API: could not update the webhook of {repo} (HTTP {c})")

            ctx.change(f"update the GitHub webhook of {repo}", patch)
        hook_id = hook["id"]
    else:
        res = ctx.change(
            f"create the GitHub webhook of {repo} -> {url}",
            lambda: http_json(
                "POST",
                f"{api}/repos/{repo}/hooks",
                headers=h,
                body={"name": "web", "active": True, "events": WEBHOOK_EVENTS, "config": config},
                name="GitHub API",
            ),
        )
        if res is None:
            return "planned"
        c, b = res
        if c != 201 or not isinstance(b, dict) or "id" not in b:
            raise DeployError(f"GitHub API: could not create the webhook of {repo} (HTTP {c})")
        hook_id = b["id"]
    return str(hook_id)


def hook_last_response(token: str, repo: str, hook_id: str) -> tuple[int | None, str]:
    h = {
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "Accept": "application/vnd.github+json",
    }
    code, b = http_json("GET", f"{github_api()}/repos/{repo}/hooks/{hook_id}", headers=h, name="GitHub API")
    if code != 200 or not isinstance(b, dict):
        return None, f"HTTP {code}"
    lr = b.get("last_response") or {}
    return lr.get("code"), str(lr.get("status") or lr.get("message") or "")


# -- Healthchecks.io ------------------------------------------------------------------------------------------------


class Healthchecks:
    def __init__(self, api_url: str, key: str) -> None:
        self.base = check_api_url(api_url, "monitoring.api_url") + "/api/v3"
        self.h = {"X-Api-Key": key}

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        code, data = http_json(method, self.base + path, headers=self.h, body=body, name="Healthchecks.io API")
        if code in (401, 403):
            if method == "POST" and path == "/checks/" and code == 403:
                raise DeployError("Healthchecks.io refused to create the check: the account reached its check limit")
            raise DeployError(
                f"Healthchecks.io rejected the API key (HTTP {code})",
                hint="use a read-write API key of the project that should own the check (Project Settings → API Access)",
            )
        if code == 400:
            msg = (data or {}).get("error", "") if isinstance(data, dict) else ""
            raise DeployError(f"Healthchecks.io rejected the request: {redact(str(msg))[:200]}")
        if code not in (200, 201):
            raise DeployError(f"Healthchecks.io API returned HTTP {code} for {method} {path.split('?')[0]}")
        if not isinstance(data, dict):
            raise DeployError(f"Healthchecks.io API: incomplete response for {method} {path.split('?')[0]}")
        return data

    def channels(self) -> list[dict[str, Any]]:
        d = self._call("GET", "/channels/")
        ch = d.get("channels")
        if not isinstance(ch, list) or not all(isinstance(c, dict) and c.get("id") for c in ch):
            raise DeployError("Healthchecks.io API: incomplete response listing integrations")
        return ch

    def checks(self) -> list[dict[str, Any]]:
        d = self._call("GET", "/checks/")
        ch = d.get("checks")
        if not isinstance(ch, list):
            raise DeployError("Healthchecks.io API: incomplete response listing checks")
        return [c for c in ch if isinstance(c, dict)]

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        return valid_check(self._call("POST", "/checks/", {**body, "unique": ["name"]}))

    def update(self, uuid: str, body: dict[str, Any]) -> dict[str, Any]:
        if not re.match(r"^[0-9a-f-]{36}$", uuid):
            raise DeployError("Healthchecks.io API: invalid check identifier")
        return valid_check(self._call("POST", f"/checks/{uuid}", body))


def valid_check(c: dict[str, Any]) -> dict[str, Any]:
    ping = c.get("ping_url")
    if not isinstance(ping, str) or not re.match(r"^https?://\S+$", ping):
        raise DeployError(
            "Healthchecks.io API: the check has no usable ping_url (read-only key, or incomplete response)"
        )
    register_secret(ping)
    return c


def check_uuid(c: dict[str, Any]) -> str:
    u = c.get("uuid") or ""
    if not u and isinstance(c.get("update_url"), str):
        u = c["update_url"].rstrip("/").rsplit("/", 1)[-1]
    return str(u)


def desired_check(m: dict[str, Any], channel_ids: list[str]) -> dict[str, Any]:
    return {
        "name": m["check_name"],
        "slug": re.sub(r"[^a-z0-9_-]+", "-", m["check_name"].lower()).strip("-")[:100],
        "tags": m["tags"],
        "desc": f"AgentOffice deep health heartbeat (deployment/healthcheck.sh). {MANAGED_MARKER}",
        "timeout": m["interval_seconds"],
        "grace": m["grace_seconds"],
        "channels": ",".join(channel_ids),
    }


def check_drift(existing: dict[str, Any], want: dict[str, Any]) -> list[str]:
    diffs = []
    for k in ("timeout", "grace"):
        if existing.get(k) != want[k]:
            diffs.append(f"{k} {existing.get(k)} -> {want[k]}")
    if set(str(existing.get("tags", "")).split()) != set(want["tags"].split()):
        diffs.append("tags")
    have = {x for x in str(existing.get("channels", "")).split(",") if x}
    if have != set(want["channels"].split(",")):
        diffs.append(f"integrations {len(have)} -> {len(want['channels'].split(','))}")
    if MANAGED_MARKER not in str(existing.get("desc", "")):
        diffs.append("description")
    return diffs


def select_channels(available: list[dict[str, Any]], wanted: str) -> list[str]:
    if not available:
        raise DeployError(
            "the Healthchecks.io project has no integrations: an alert would reach nobody",
            hint=f"add an integration (e.g. e-mail) and verify it, then re-run ({DOC['hc_integrations']})",
        )
    if wanted == "*":
        return [str(c["id"]) for c in available]
    by_name = {str(c.get("name", "")): str(c["id"]) for c in available}
    names = [x.strip() for x in wanted.split(",")]
    missing = [n for n in names if n not in by_name]
    if missing:
        raise DeployError(f"Healthchecks.io integrations not found: {missing} (available: {sorted(by_name)})")
    return [by_name[n] for n in names]


def provision_monitor(ctx: Ctx) -> None:
    m = ctx.cfg["monitoring"]
    envf = ctx.paths.env_file
    current = env_get(envf, "HC_PING_URL")
    register_secret(current)
    key = ctx.secret("healthchecks_api_key", "Healthchecks.io API key (read-write)")
    if not key:
        if current:
            log("no Healthchecks.io API key: keeping the configured HC_PING_URL (check settings not re-verified)")
            return
        if ctx.dry_run:
            ctx.plan.append(f"create the Healthchecks.io check {m['check_name']!r} (needs healthchecks_api_key)")
            return
        raise DeployError(
            "HC_PING_URL is not configured and no healthchecks_api_key was provided",
            hint=f"create a read-write API key in the Healthchecks.io project ({DOC['hc_api']}) and add it to the secrets file",
        )
    if ctx.dry_run:
        ctx.plan.append(
            f"Healthchecks.io: look up check {m['check_name']!r}; create it if missing (period {m['interval_seconds']} s, "
            f"grace {m['grace_seconds']} s, integrations {m['channels']!r}); store HC_PING_URL in {envf}"
        )
        return
    hc = Healthchecks(m["api_url"], key)
    channel_ids = select_channels(hc.channels(), m["channels"])
    want = desired_check(m, channel_ids)
    same = [c for c in hc.checks() if c.get("name") == m["check_name"]]
    if len(same) > 1:
        raise DeployError(
            f"{len(same)} Healthchecks.io checks are named {m['check_name']!r}: refusing to guess",
            hint="delete or rename the extra ones",
        )
    if same:
        existing = valid_check(same[0])
        if MANAGED_MARKER not in str(existing.get("desc", "")) and not m["adopt_existing"]:
            raise DeployError(
                f"a check named {m['check_name']!r} exists but was not created by this bootstrap; not modified",
                hint="choose another monitoring.check_name, or set monitoring.adopt_existing = true to manage it",
            )
        diffs = check_drift(existing, want)
        if diffs:
            check = ctx.change(
                f"update Healthchecks.io check {m['check_name']!r} ({', '.join(diffs)})",
                lambda: hc.update(check_uuid(existing), want),
            )
        else:
            log(f"Healthchecks.io check {m['check_name']!r} exists with the expected settings: reused")
            check = existing
    else:
        check = ctx.change(f"create Healthchecks.io check {m['check_name']!r}", lambda: hc.create(want))
    if not str(check.get("channels", "")).strip(","):
        raise DeployError("the Healthchecks.io check has no integration assigned: alerts would reach nobody")
    ping = check["ping_url"]
    if ping != current:
        ctx.change(f"store HC_PING_URL in {envf} (protected, 0640)", lambda: env_set(envf, "HC_PING_URL", ping))
    ctx.state.set(
        "monitoring",
        {
            "check_name": m["check_name"],
            "integrations": len(channel_ids),
            "verified_at": now_iso(),
            "api_url": m["api_url"],
        },
    )


def stage_monitoring(ctx: Ctx) -> None:
    if not ctx.healthchecks:
        log("monitoring.provider = none: no external monitor (development mode)")
        return
    provision_monitor(ctx)


# -- stage: systemd units and local backups -------------------------------------------------------------------------


def latest_backup(paths: Paths) -> Path | None:
    files = sorted(paths.backups.glob("agent-office-*.db.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def backup_ok(b: Path) -> bool:
    s = b.with_name(b.name + ".sha256")
    if not s.is_file():
        return False
    want = s.read_text().split()[0] if s.read_text().split() else ""
    return hashlib.sha256(b.read_bytes()).hexdigest() == want


def stage_services(ctx: Ctx) -> None:
    p = ctx.paths
    scope = ["--user"] if p.user_mode else []

    def enable(u: str) -> Callable[[], Result]:
        return lambda: must(["systemctl", *scope, "enable", "--now", u], f"enable {u}")

    for unit in ("agent-office.service", "agent-office-healthcheck.timer", "agent-office-backup.timer"):
        if sctl(p, "is-enabled", unit).out.strip() != "enabled":
            ctx.change(f"systemctl enable --now {unit}", enable(unit))
    if ctx.dry_run:
        return
    if not wait_healthy(ctx.port, 40):
        raise DeployError("agent-office.service is not healthy", hint="journalctl -u agent-office -n 50")
    if latest_backup(p) is None:
        ctx.change(
            "run the first local backup (agent-office-backup.service)",
            lambda: must(
                ["systemctl", *(["--user"] if p.user_mode else []), "start", "agent-office-backup.service"],
                "first backup",
                timeout=600,
            ),
        )
    b = latest_backup(p)
    if b is None or not backup_ok(b):
        raise DeployError("no verifiable local backup was produced", hint="journalctl -u agent-office-backup -n 50")
    if ctx.healthchecks and env_get(p.env_file, "HC_PING_URL"):
        r = sctl(p, "start", "agent-office-healthcheck.service", timeout=120)
        if r.code != 0:
            raise DeployError(
                "the health check unit failed: the heartbeat was not sent",
                hint="journalctl -u agent-office-healthcheck -n 20",
            )
        log("health check ran: heartbeat sent to Healthchecks.io")


# -- live verification ----------------------------------------------------------------------------------------------


@dataclass
class Row:
    name: str
    status: str
    detail: str
    required: bool = True


class _PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS to the Tailscale IP with SNI/verification for the MagicDNS name (works without MagicDNS resolution on
    the server itself). Certificate verification is the system default: never disabled."""

    def __init__(self, ip: str, host: str, port: int, timeout: float = 10) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]


def https_call(
    ip: str, host: str, port: int, method: str, path: str, token: str | None = None, body: bytes | None = None
) -> tuple[int, bytes]:
    c = _PinnedHTTPS(ip, host, port)
    try:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=body, headers=headers)
        r = c.getresponse()
        return r.status, r.read(1 << 20)
    finally:
        c.close()


def tls_info(ip: str, host: str, port: int) -> tuple[str, str]:
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((ip, port), 10) as s, ctx.wrap_socket(s, server_hostname=host) as t:
            cert: dict[str, Any] = dict(t.getpeercert() or {})
    except ssl.SSLCertVerificationError as e:
        return FAIL, f"certificate NOT valid for {host}: {e.verify_message}"
    except (OSError, ssl.SSLError) as e:
        return FAIL, f"TLS connection to {host}:{port} failed: {e}"
    exp = ssl.cert_time_to_seconds(str(cert["notAfter"])) if cert.get("notAfter") else 0
    days = int((exp - time.time()) // 86400)
    issuer = next((v for rdn in cert.get("issuer", ()) for k, v in rdn if k == "organizationName"), "?")
    if days < 0:
        return FAIL, f"certificate expired {-days} days ago"
    return PASS, f"valid for {host}, issuer {issuer}, expires in {days} days (renewed by tailscaled)"


WS_CHECK = r"""
import json, socket, ssl, sys
from websockets.sync.client import connect
from websockets.exceptions import InvalidStatus
a = json.load(sys.stdin)
def open_ws(ticket):
    sock = socket.create_connection((a["ip"], a["port"]), 10)
    url = "wss://%s:%d/ws/events?ticket=%s" % (a["host"], a["port"], ticket)
    return connect(url, sock=sock, ssl=ssl.create_default_context(), server_hostname=a["host"], open_timeout=10)
with open_ws(a["ticket"]) as ws:
    hello = json.loads(ws.recv(timeout=10))
try:
    with open_ws("invalid-ticket") as ws:
        ws.recv(timeout=5)
    bad = "accepted"
except InvalidStatus as e:
    bad = "rejected %d" % e.response.status_code
except Exception as e:
    bad = "rejected (%s)" % type(e).__name__
print(json.dumps({"hello": hello.get("type"), "agent": hello.get("agent"), "bad": bad}))
"""


def ws_check(ctx: Ctx, ip: str, host: str, port: int, token: str) -> tuple[str, str]:
    py = ctx.paths.venv_python
    if not py.exists():
        return NOT_VERIFIED, f"{py} not found (no release installed)"
    code, body = https_call(ip, host, port, "POST", "/api/ws-ticket", token, b"{}")
    if code != 200:
        return FAIL, f"POST /api/ws-ticket through Serve returned {code}"
    try:
        ticket = json.loads(body)["ticket"]
    except (ValueError, KeyError):
        return FAIL, "the ticket response is not valid"
    r = run(
        [str(py), "-I", "-c", WS_CHECK],
        input=json.dumps({"ip": ip, "host": host, "port": port, "ticket": ticket}),
        timeout=60,
    )
    if r.code != 0:
        return FAIL, f"WebSocket through Serve failed: {tail(r.err, 2)}"
    res = json.loads(r.out.strip().splitlines()[-1])
    if res.get("hello") != "hello":
        return FAIL, f"unexpected first frame {res.get('hello')!r}"
    if not str(res.get("bad", "")).startswith("rejected"):
        return FAIL, "a WebSocket with an invalid ticket was ACCEPTED"
    return PASS, f"wss handshake with a single-use ticket ok (hello as {res.get('agent')}); invalid ticket {res['bad']}"


def listeners(port: int) -> list[str] | None:
    r = run(["ss", "-ltnH"], timeout=10)
    if r.code != 0:
        return None
    out = []
    for line in r.out.splitlines():
        cols = line.split()
        if len(cols) >= 4:
            host, _, p = cols[3].rpartition(":")
            if p == str(port):
                out.append(host.strip("[]").split("%")[0])
    return out


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def unit_result(paths: Paths, unit: str) -> dict[str, str]:
    r = sctl(paths, "show", "-p", "Result,ExecMainStatus,ExecMainExitTimestamp,ActiveState", unit, timeout=20)
    return dict(line.split("=", 1) for line in r.out.splitlines() if "=" in line)


def leak_scan(ctx: Ctx) -> tuple[str, str]:
    """No secret value in the files the bootstrap writes that are not meant to hold secrets."""
    values = [v for v in _SECRETS if len(v) >= 8]
    files = [
        ctx.paths.state_file,
        ctx.paths.report_file,
        ctx.paths.etc / "deploy.conf",
        ctx.paths.etc / "deployment.toml",
    ]
    files += sorted(ctx.paths.log_dir.glob("bootstrap*.log"))
    hits = []
    for f in files:
        with contextlib.suppress(OSError):
            text = f.read_text("utf-8", "replace")
            if any(v in text for v in values):
                hits.append(str(f))
    if hits:
        return FAIL, f"secret values found in {hits}"
    return (
        PASS,
        f"no secret values in {len([f for f in files if f.exists()])} bootstrap files (state, report, logs, config)",
    )


def verify_live(ctx: Ctx) -> list[Row]:
    p, rows = ctx.paths, list[Row]()
    add = rows.append
    plat = ctx.platform
    add(
        Row(
            "Linux",
            PASS if plat.supported else FAIL,
            f"{plat.pretty} {plat.arch}" + (f" ({plat.reason})" if plat.reason else ""),
        )
    )

    vpy = probe_python(str(p.venv_python)) if p.venv_python.exists() else None
    add(Row("Python", PASS if vpy and vpy.compatible else FAIL, vpy.describe() if vpy else f"{p.venv_python} missing"))

    active = sctl(p, "is-active", "agent-office.service").out.strip()
    shallow = health(ctx.port, deep=False)
    add(
        Row(
            "Backend",
            PASS if active == "active" and shallow and shallow.get("ok") else FAIL,
            f"agent-office.service {active or 'unknown'}; /api/health {'ok' if shallow and shallow.get('ok') else 'unreachable'}",
        )
    )
    deep = health(ctx.port, deep=True) or {}
    add(
        Row(
            "SQLite",
            PASS if deep.get("db") else FAIL,
            "deep health: read/write ok"
            if deep.get("db")
            else f"deep health: {deep.get('db_error') or deep or 'unreachable'}",
        )
    )
    add(
        Row(
            "Recovery",
            PASS if deep.get("reconciler") else FAIL,
            "reconciler loop alive" if deep.get("reconciler") else "reconciler not reporting",
        )
    )

    bad = [
        u
        for u in ("agent-office.service", "agent-office-healthcheck.timer", "agent-office-backup.timer")
        if sctl(p, "is-enabled", u).out.strip() != "enabled" or sctl(p, "is-active", u).out.strip() != "active"
    ]
    add(
        Row(
            "Systemd",
            FAIL if bad else PASS,
            f"not enabled+active: {bad}" if bad else "service and both timers enabled and active",
        )
    )

    ports = [ctx.port] + ([ctx.cfg["github_webhook"]["port"]] if ctx.webhook else [])
    exposed: list[str] = []
    unknown = False
    for port in ports:
        ls = listeners(port)
        if ls is None:
            unknown = True
        else:
            exposed += [f"{h}:{port}" for h in ls if not is_loopback(h)]
    if unknown:
        add(Row("Loopback only", NOT_VERIFIED, "`ss` is not available"))
    else:
        add(
            Row(
                "Loopback only",
                FAIL if exposed else PASS,
                f"listening on non-loopback {exposed}" if exposed else f"ports {ports} bound to loopback only",
            )
        )

    token = ctx.admin_token()
    if ctx.tailscale:
        rows.extend(verify_tailscale(ctx, token))
    else:
        for name in ("Tailscale", "Private HTTPS", "TLS Certificate", "WebSocket"):
            add(Row(name, SKIPPED, "network.provider = none (development)", required=False))
        code, _ = local_api(p, ctx.port, "GET", "/api/me", token)
        add(Row("API", PASS if code == 200 else FAIL, f"admin token on loopback: HTTP {code}"))

    if ctx.healthchecks:
        add(verify_monitor(ctx))
    else:
        add(Row("Healthchecks.io", SKIPPED, "monitoring.provider = none (development)", required=False))

    b = latest_backup(p)
    timer = sctl(p, "is-active", "agent-office-backup.timer").out.strip()
    if b is None:
        add(Row("Local backups", FAIL, f"timer {timer}; no backup in {p.backups}"))
    else:
        age_h = (time.time() - b.stat().st_mtime) / 3600
        ok = backup_ok(b) and timer == "active"
        add(
            Row(
                "Local backups",
                PASS if ok else FAIL,
                f"timer {timer}; latest {b.name} ({age_h:.0f} h old, sha256 {'ok' if backup_ok(b) else 'MISMATCH'})",
            )
        )

    add(verify_credentials(ctx))
    if ctx.tailscale:
        add(
            Row(
                "Other devices",
                NOT_VERIFIED,
                "access is only checked from this server; test from a participant laptop",
                required=False,
            )
        )
    return rows


def verify_tailscale(ctx: Ctx, token: str) -> list[Row]:
    rows: list[Row] = []
    st = tailscale_state(ctx)
    n = ctx.cfg["network"]
    if st.kind != "running":
        rows.append(Row("Tailscale", FAIL, f"state {st.backend or st.kind} {st.detail}".strip()))
        for name in ("Private HTTPS", "TLS Certificate", "API", "WebSocket"):
            rows.append(Row(name, NOT_VERIFIED, "Tailscale is not running"))
        return rows
    ctx.ts = st
    tag_note = "" if n["tag"] in st.tags else f"; WARNING not tagged {n['tag']}"
    ok_tn = st.in_tailnet(n["tailnet"])
    rows.append(
        Row(
            "Tailscale",
            PASS if ok_tn else FAIL,
            f"{st.dns_name} in {st.tailnet or st.magic_suffix}{tag_note}"
            + ("" if ok_tn else f" (expected {n['tailnet']})"),
        )
    )
    https_port = int(n["https_port"])
    try:
        serve = serve_status()
    except DeployError as e:
        serve = None
        rows.append(Row("Private HTTPS", FAIL, str(e)))
    if serve is not None:
        kind, detail = classify_port(serve, st.dns_name, https_port, f"http://127.0.0.1:{ctx.port}", funnel=False)
        public = funnel_ports(serve)
        allowed = [f"{st.dns_name}:{ctx.cfg['github_webhook']['funnel_port']}"] if ctx.webhook else []
        extra = [x for x in public if x not in allowed]
        if kind == "ours" and not extra:
            rows.append(
                Row("Private HTTPS", PASS, detail + ("; Funnel only on the webhook port" if public else "; Funnel off"))
            )
        else:
            rows.append(
                Row("Private HTTPS", FAIL, f"{kind}: {detail}" + (f"; unexpected Funnel on {extra}" if extra else ""))
            )
    ip = next((x for x in st.ips if ":" not in x), st.ips[0] if st.ips else "")
    if not ip:
        rows.append(Row("TLS Certificate", NOT_VERIFIED, "no Tailscale IP"))
        return rows
    status, detail = tls_info(ip, st.dns_name, https_port)
    rows.append(Row("TLS Certificate", status, detail))
    if status != PASS:
        rows.append(Row("API", NOT_VERIFIED, "TLS failed"))
        rows.append(Row("WebSocket", NOT_VERIFIED, "TLS failed"))
        return rows
    try:
        c1, _ = https_call(ip, st.dns_name, https_port, "GET", "/api/health")
        c2, _ = https_call(ip, st.dns_name, https_port, "GET", "/api/me")
        c3, b3 = https_call(ip, st.dns_name, https_port, "GET", "/api/me", token)
        role = json.loads(b3).get("role") if c3 == 200 else None
        ok = c1 == 200 and c2 == 401 and c3 == 200
        rows.append(
            Row(
                "API",
                PASS if ok else FAIL,
                f"via Serve: /api/health {c1}, /api/me without token {c2}, with admin token {c3} (role {role})",
            )
        )
        rows.append(Row("WebSocket", *ws_check(ctx, ip, st.dns_name, https_port, token)))
    except (OSError, ValueError, http.client.HTTPException) as e:
        rows.append(Row("API", FAIL, f"request through Serve failed: {e}"))
        rows.append(Row("WebSocket", NOT_VERIFIED, "API unreachable"))
    try:
        socket.getaddrinfo(st.dns_name, https_port)
        rows.append(Row("MagicDNS (server)", PASS, f"{st.dns_name} resolves on this server", required=False))
    except OSError:
        rows.append(
            Row(
                "MagicDNS (server)",
                NOT_VERIFIED,
                f"{st.dns_name} does not resolve on this server (checked by IP)",
                required=False,
            )
        )
    if ctx.webhook:
        rows.append(verify_webhook(ctx, serve or {}))
    return rows


def verify_webhook(ctx: Ctx, serve: dict[str, Any]) -> Row:
    g = ctx.cfg["github_webhook"]
    assert ctx.ts is not None
    if not listener_filters_webhook(g["port"]):
        return Row("GitHub webhook", FAIL, f"127.0.0.1:{g['port']} does not filter to the webhook only")
    kind, detail = classify_port(serve, ctx.ts.dns_name, g["funnel_port"], f"http://127.0.0.1:{g['port']}", funnel=True)
    if kind != "ours":
        return Row("GitHub webhook", FAIL, f"Funnel {kind}: {detail}")
    token = ctx.secrets.get("github_token")
    hooks = (ctx.state.data.get("github_webhook") or {}).get("hooks") or {}
    if not token or not hooks:
        return Row(
            "GitHub webhook",
            NOT_VERIFIED,
            f"listener filtered + Funnel ok ({payload_url(ctx)}); GitHub delivery not checked (no github_token)",
        )
    states = []
    for repo, hid in hooks.items():
        code, msg = hook_last_response(token, repo, hid)
        states.append((repo, code, msg))
    bad = [s for s in states if s[1] not in (200, 202)]
    status = PASS if not bad else (NOT_VERIFIED if all(s[1] is None for s in bad) else FAIL)
    return Row("GitHub webhook", status, "; ".join(f"{r}: last delivery {c} {m}".strip() for r, c, m in states))


def verify_monitor(ctx: Ctx) -> Row:
    p = ctx.paths
    url = env_get(p.env_file, "HC_PING_URL")
    register_secret(url)
    if not url:
        return Row("Healthchecks.io", FAIL, "HC_PING_URL is not set")
    res = unit_result(p, "agent-office-healthcheck.service")
    ran = res.get("ExecMainExitTimestamp", "") not in ("", "n/a", "0")
    hb_ok = ran and res.get("Result") == "success" and res.get("ExecMainStatus") == "0"
    key = ctx.secrets.get("healthchecks_api_key")
    m = ctx.cfg["monitoring"]
    if key:
        try:
            hc = Healthchecks(m["api_url"], key)
            same = [c for c in hc.checks() if c.get("name") == m["check_name"]]
            if len(same) != 1:
                return Row("Healthchecks.io", FAIL, f"{len(same)} checks named {m['check_name']!r}")
            c = same[0]
            if c.get("ping_url") != url:
                return Row("Healthchecks.io", FAIL, "HC_PING_URL does not belong to the configured check")
            if not str(c.get("channels", "")).strip(","):
                return Row("Healthchecks.io", FAIL, "the check has no integration (nobody would be alerted)")
            detail = f"check {m['check_name']!r} status {c.get('status')}, integrations assigned"
        except DeployError as e:
            return Row("Healthchecks.io", FAIL if "rejected" in str(e) else NOT_VERIFIED, str(e))
    else:
        mon = ctx.state.data.get("monitoring") or {}
        if not mon.get("verified_at"):
            return Row("Healthchecks.io", NOT_VERIFIED, "no API key: check and integrations not verified")
        detail = (
            f"check {mon.get('check_name')!r} with {mon.get('integrations')} integration(s) "
            f"verified at {mon['verified_at']} (no API key now)"
        )
    if not hb_ok:
        return Row(
            "Healthchecks.io",
            NOT_VERIFIED if not ran else FAIL,
            detail
            + (
                "; health check unit has not run yet"
                if not ran
                else f"; last health check run failed ({res.get('Result')})"
            ),
        )
    return Row("Healthchecks.io", PASS, detail + "; last heartbeat sent")


def verify_credentials(ctx: Ctx) -> Row:
    p = ctx.paths
    problems = []
    if p.env_file.is_file():
        st = p.env_file.stat()
        if st.st_mode & 0o777 != 0o640:
            problems.append(f"{p.env_file} mode {oct(st.st_mode & 0o777)[2:]} (expected 640)")
        if not p.user_mode:
            import grp

            group = grp.getgrgid(st.st_gid).gr_name if st.st_gid else "root"
            if st.st_uid != 0 or group != p.user:
                problems.append(f"{p.env_file} owner {st.st_uid}:{group} (expected root:{p.user})")
    else:
        problems.append(f"{p.env_file} missing")
    status, detail = leak_scan(ctx)
    if status != PASS:
        problems.append(detail)
    if problems:
        return Row("Credentials", FAIL, "; ".join(problems))
    return Row("Credentials", PASS, f"{p.env_file} 0640; {detail}")


# -- report ---------------------------------------------------------------------------------------------------------

REPORT_ORDER = [
    "Linux", "Python", "Backend", "SQLite", "Recovery", "Systemd", "Loopback only", "Tailscale", "Private HTTPS",
    "TLS Certificate", "API", "WebSocket", "GitHub webhook", "Healthchecks.io", "Local backups", "Credentials",
    "MagicDNS (server)", "Other devices",
]  # fmt: skip


def overall(rows: list[Row]) -> str:
    req = [r for r in rows if r.required and r.status != SKIPPED]
    if any(r.status == FAIL for r in req):
        return "FAILED"
    if any(r.status != PASS for r in req):
        return "INCOMPLETE"
    return "SUCCESS"


def render_report(ctx: Ctx, rows: list[Row]) -> str:
    rows = sorted(rows, key=lambda r: REPORT_ORDER.index(r.name) if r.name in REPORT_ORDER else 99)
    w = max(len(r.name) for r in rows) + 2
    out = ["", "AGENTOFFICE DEPLOYMENT REPORT", f"({now_iso()}, mode {ctx.cfg['deployment']['mode']})", ""]
    for r in rows:
        out.append(
            f"{(r.name + ':').ljust(w)}{r.status.ljust(13)}{r.detail}" + ("" if r.required else "  [informational]")
        )
    out.append("")
    if ctx.ts and ctx.tailscale:
        port = int(ctx.cfg["network"]["https_port"])
        out.append(f"{'Private URL:'.ljust(w)}https://{ctx.ts.dns_name}{'' if port == 443 else f':{port}'}")
    else:
        out.append(f"{'Local URL:'.ljust(w)}http://127.0.0.1:{ctx.port}")
    if ctx.webhook:
        out.append(
            f"{'Webhook URL:'.ljust(w)}{payload_url(ctx)}  (secret: GITHUB_WEBHOOK_SECRET in {ctx.paths.env_file})"
        )
    out.append(
        f"{'Administration:'.ljust(w)}admin token in {ctx.paths.env_file} (sudo sed -n 's/^AGENTLINK_ADMIN_TOKEN=//p' {ctx.paths.env_file})"
    )
    unit = "journalctl --user -u" if ctx.paths.user_mode else "journalctl -u"
    out.append(f"{'Logs:'.ljust(w)}{unit} agent-office; bootstrap log in {ctx.paths.log_dir}/bootstrap.log")
    out.append("")
    result = overall(rows)
    out.append(f"{'Deployment:'.ljust(w)}{result}")
    if result == "SUCCESS":
        out.append(
            "Next: create one credential per participant agent (docs/AGENT_INSTALLATION.md §0) and invite them to the tailnet."
        )
    elif result == "INCOMPLETE":
        out.append("Some required checks could not be verified: see NOT_VERIFIED above.")
    else:
        out.append("Fix the FAIL rows and re-run: completed steps are detected, not redone.")
    return redact("\n".join(out)) + "\n"


def write_report(ctx: Ctx, text: str) -> None:
    f = ctx.paths.report_file
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f".{f.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, f)


# -- orchestration --------------------------------------------------------------------------------------------------

STAGES: list[tuple[str, Callable[[Ctx], None]]] = [
    ("dependencies", stage_dependencies),
    ("python", stage_python),
    ("install", stage_install),
    ("tailscale", stage_tailscale),
    ("https", stage_https),
    ("github_webhook", stage_github_webhook),
    ("monitoring", stage_monitoring),
    ("services", stage_services),
]


def print_rows(rows: list[tuple[str, str, str]]) -> None:
    w = max(len(r[0]) for r in rows) + 2
    for name, status, detail in rows:
        print(f"{(name + ':').ljust(w)}{status.ljust(13)}{redact(detail)}")


def make_ctx(a: argparse.Namespace, *, config_required: bool, dry_run: bool = False) -> Ctx:
    paths = Paths.from_env()
    cfg, cfg_path = load_config(a.config, paths, required=config_required)
    secrets = load_secrets(a.secrets_file)
    state = State(paths.state_file, enabled=not dry_run and paths.etc.is_dir())
    return Ctx(
        cfg,
        cfg_path,
        secrets,
        paths,
        current_platform(),
        state,
        dry_run=dry_run,
        interactive=not a.non_interactive,
        tailscale_script=bool(getattr(a, "install_tailscale", False)),
    )


def cmd_bootstrap(a: argparse.Namespace) -> int:
    mode = "check" if a.check else "dry-run" if a.dry_run else "verify" if a.verify else "apply"
    ctx = make_ctx(a, config_required=mode != "verify", dry_run=mode in ("check", "dry-run"))
    if mode == "verify":
        return cmd_verify_ctx(ctx)
    if mode == "apply":
        if not ctx.paths.user_mode and os.geteuid() != 0:
            raise DeployError("run as root (sudo)")
        open_log(ctx.paths.log_dir / "bootstrap.log")
        ctx.state.enabled = True
    log(
        f"AgentOffice bootstrap ({mode}) on {ctx.platform.pretty} {ctx.platform.arch}; config {ctx.cfg_path or 'defaults'}"
    )
    rows = precheck(ctx)
    if mode == "check" or any(r[1] == FAIL for r in rows):
        print_rows(rows)
        failed = [r for r in rows if r[1] == FAIL]
        if failed and mode != "check":
            raise DeployError("precheck failed: " + "; ".join(f"{r[0]}: {r[2]}" for r in failed))
        if mode == "check":
            print("\nCHECK: " + ("FAILED" if failed else "OK (nothing was changed)"))
        return 1 if failed else 0
    ctx.state.mark("precheck", "done") if mode == "apply" else None
    for i, (name, fn) in enumerate(STAGES, 1):
        log(f"== [{i}/{len(STAGES)}] {name}")
        try:
            fn(ctx)
        except DeployError as e:
            if mode == "apply":
                ctx.state.mark(name, "failed", str(e))
            raise DeployError(f"stage {name}: {e}", e.hint) from None
        if mode == "apply":
            ctx.state.mark(name, "done")
    if mode == "dry-run":
        print("\nDRY-RUN PLAN (nothing was changed):")
        for i, step in enumerate(ctx.plan, 1):
            print(f"  {i:2}. {redact(step)}")
        if not ctx.plan:
            print("  nothing to do: the deployment already matches the configuration")
        return 0
    return cmd_verify_ctx(ctx, show_access=True)


_STATUS_STYLE = {PASS: ("✔", "green"), FAIL: ("✖", "red"), NOT_VERIFIED: ("?", "yellow"), SKIPPED: ("-", "dim"),
                 "SUCCESS": ("✔", "green"), "FAILED": ("✖", "red"), "INCOMPLETE": ("?", "yellow")}  # fmt: skip


_REPORT_ROW = re.compile(r"^(\S[^:]*:\s+)(PASS|FAIL|NOT_VERIFIED|SKIPPED|SUCCESS|FAILED|INCOMPLETE)\b(.*)$")


def paint_report(text: str) -> str:
    """The report on a terminal: status words with a symbol and colour. The report file keeps the plain text."""
    if not _tty(sys.stdout):
        return text
    out = []
    for line in text.splitlines():
        m = _REPORT_ROW.match(line)
        if line == "AGENTOFFICE DEPLOYMENT REPORT":
            line = paint(line, "bold")
        elif m:
            label, status, rest = m.groups()
            mark, color = _STATUS_STYLE[status]
            final = status in ("SUCCESS", "FAILED", "INCOMPLETE")
            line = label + paint(f"{mark} {status}", color, *(("bold",) if final else ())) + rest
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def access_url(ctx: Ctx) -> str:
    if ctx.ts and ctx.tailscale and ctx.ts.dns_name:
        port = int(ctx.cfg["network"]["https_port"])
        return f"https://{ctx.ts.dns_name}{'' if port == 443 else f':{port}'}"
    return f"http://127.0.0.1:{ctx.port}"


def project_env_path(paths: Paths) -> Path | None:
    """The .env of the checkout that ran deploy.sh, the same file `make dev` writes (AO_PROJECT_ENV overrides it).
    The test sandbox (AO_USER_MODE=1) never touches the developer's real .env unless AO_PROJECT_ENV says so."""
    explicit = os.environ.get("AO_PROJECT_ENV", "")
    if explicit:
        return Path(explicit)
    if paths.user_mode:
        return None
    return REPO / ".env"


def save_project_token(paths: Paths, token: str) -> Path | None:
    """AGENTLINK_ADMIN_TOKEN=... in the checkout's .env (mode 0600, owned by the sudo user); other lines are kept."""
    f = project_env_path(paths)
    if f is None or not token:
        return None
    try:
        if not f.exists():
            fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write("AGENTLINK_DB_PATH=data/agentlink.db\n")
        env_set(f, "AGENTLINK_ADMIN_TOKEN", token)
        os.chmod(f, 0o600)
        uid, gid = os.environ.get("SUDO_UID", ""), os.environ.get("SUDO_GID", "")
        if os.geteuid() == 0 and uid.isdigit() and gid.isdigit():
            os.chown(f, int(uid), int(gid))
    except OSError as e:
        log(f"WARN: could not save the admin token in {f}: {e}")
        return None
    return f


def access_box(url: str, token: str, saved: Path | None, env_file: Path) -> str:
    """Printed on stdout only, outside redact(): never written to the log or the report file."""
    rows = [
        ("URL", url, ("bold",)),
        ("Admin token", token or f"(not found in {env_file})", ("bold", "green") if token else ("red",)),
        ("Saved in", f"{saved} (like make dev)" if saved else str(env_file), ()),
        ("Admin page", f"{url}/admin", ()),
    ]
    w = max(len(k) for k, _, _ in rows) + 2
    inner = max(w + len(v) for _, v, _ in rows) + 2
    title = " AgentOffice access "
    lines = ["", paint("╭─" + title + "─" * (inner - len(title) - 1) + "╮", "cyan")]
    for k, v, st in rows:
        pad = " " * (inner - 1 - w - len(v))
        lines.append(paint("│", "cyan") + " " + (k + ":").ljust(w) + paint(v, *st) + pad + paint("│", "cyan"))
    lines.append(paint("╰" + "─" * inner + "╯", "cyan"))
    lines.append(paint("Keep the token private: it can do everything. Rotate it from /admin if it leaks.", "dim"))
    return "\n".join(lines) + "\n"


def print_access(ctx: Ctx) -> None:
    token = env_get(ctx.paths.env_file, "AGENTLINK_ADMIN_TOKEN")
    saved = save_project_token(ctx.paths, token)
    if saved:
        log(f"admin token saved in {saved}")
    sys.stdout.write(access_box(access_url(ctx), token, saved, ctx.paths.env_file))
    sys.stdout.flush()


def cmd_verify_ctx(ctx: Ctx, *, show_access: bool = False) -> int:
    rows = verify_live(ctx)
    text = render_report(ctx, rows)
    print(paint_report(text))
    with contextlib.suppress(OSError):
        write_report(ctx, text)
    if ctx.state.enabled and not ctx.dry_run:
        ctx.state.set("last_verify", {"at": now_iso(), "result": overall(rows)})
    if show_access:
        print_access(ctx)
    return 0 if overall(rows) == "SUCCESS" else 1


def cmd_python(a: argparse.Namespace) -> int:
    ctx = make_ctx(a, config_required=False, dry_run=a.dry_run)
    stage_python(ctx)
    if ctx.python:
        print(ctx.python)
    return 0


def cmd_tailscale(a: argparse.Namespace) -> int:
    ctx = make_ctx(a, config_required=False, dry_run=a.dry_run)
    stage_tailscale(ctx)
    stage_https(ctx)
    if ctx.ts:
        print(f"https://{ctx.ts.dns_name}")
    return 0


def cmd_monitor(a: argparse.Namespace) -> int:
    ctx = make_ctx(a, config_required=False, dry_run=a.dry_run)
    stage_monitoring(ctx)
    if not ctx.dry_run:
        print(verify_monitor(ctx).detail)
    return 0


def cmd_detect(a: argparse.Namespace) -> int:
    p = current_platform()
    print(json.dumps(p.__dict__ | {"system_python_ok": p.system_python_ok}, indent=2))
    return 0 if p.supported else 1


# -- interactive wizard (deployment/install.sh --wizard) ------------------------------------------------------------


def toml_str(v: str) -> str:
    """A TOML basic string. Values are validated by parse_config afterwards; this only guarantees correct quoting."""
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_config(a: dict[str, Any]) -> str:
    lines = [
        "# Generated by deployment/install.sh --wizard. Edit and re-run deployment/deploy.sh to apply changes.",
        "[deployment]",
        f"mode = {toml_str(a['mode'])}",
        f"hostname = {toml_str(a['hostname'])}",
        "",
        "[application]",
        'bind_host = "127.0.0.1"',
        f"port = {int(a['port'])}",
        "",
        "[network]",
        f"provider = {toml_str(a['network'])}",
    ]
    if a["network"] == "tailscale":
        lines += [f"tag = {toml_str(a['tag'])}", f"tailnet = {toml_str(a['tailnet'])}", "https = true"]
        lines += ["https_port = 443", "public_access = false"]
    lines += ["", "[monitoring]", f"provider = {toml_str(a['monitoring'])}"]
    if a["monitoring"] == "healthchecks":
        lines += [f"check_name = {toml_str(a['check_name'])}", f"channels = {toml_str(a['channels'])}"]
        lines += ["interval_seconds = 300", "grace_seconds = 600"]
    lines += ["", "[backup]", "local_enabled = true", "", "[github_webhook]", f"enabled = {str(a['webhook']).lower()}"]
    if a["webhook"]:
        lines += [f"funnel_port = {int(a['funnel_port'])}"]
        for repo in a["repos"]:
            lines += ["", "[[github_webhook.repositories]]", f"repo = {toml_str(repo)}"]
    return "\n".join(lines) + "\n"


def ask(prompt: str, default: str = "", valid: Callable[[str], bool] = lambda v: True, error: str = "") -> str:
    while True:
        v = input(f"{prompt}" + (f" [{default}]" if default else "") + ": ").strip() or default
        if valid(v):
            return v
        print(f"  {error or 'invalid value'}")


def ask_secret(prompt: str, valid: Callable[[str], bool] = lambda v: True, error: str = "") -> str:
    while True:
        v = getpass.getpass(f"{prompt} (hidden, Enter to skip): ").strip()
        register_secret(v)
        if not v or valid(v):
            return v
        print(f"  {error or 'invalid value'}")


def yes(prompt: str, default: bool) -> bool:
    v = ask(prompt + " (y/n)", "y" if default else "n", lambda x: x.lower() in ("y", "n", "yes", "no", "s", "si", "sí"))
    return v.lower()[0] in "ys"


def cmd_wizard(a: argparse.Namespace) -> int:
    if not sys.stdin.isatty():
        raise DeployError("the wizard is interactive: run it from a terminal, or use deployment/bootstrap.sh --config")
    paths = Paths.from_env()
    out = Path(a.config) if a.config else paths.etc / "deployment.toml"
    print("AgentOffice installation wizard. Secrets are typed hidden, never echoed or stored in the configuration.\n")
    ans: dict[str, Any] = {}
    prod = ask("Mode: 1) Production (Tailscale HTTPS + Healthchecks.io)  2) Development (local only)", "1",
               lambda v: v in ("1", "2"), "answer 1 or 2") == "1"  # fmt: skip
    ans["mode"] = "production" if prod else "development"
    ans["hostname"] = ask("Server name in the tailnet", "agentoffice", lambda v: bool(HOSTNAME_RE.match(v)),
                          "lowercase letters, digits and '-' (max 63)")  # fmt: skip
    ans["port"] = ask("Local backend port (loopback only)", "8080", lambda v: v.isdigit() and 1024 <= int(v) <= 65535)
    sec: dict[str, str] = {}
    ans["network"] = "tailscale" if prod else "none"
    ans["monitoring"] = "healthchecks" if prod else "none"
    ans["webhook"] = False
    deploy_flags: list[str] = []
    if prod:
        print("\n-- Tailscale (docs/DEPLOYMENT_SETUP_GUIDE.md §C)")
        if not shutil.which("tailscale"):
            if not yes(f"Tailscale is not installed and production needs it. Install it during the deployment with "
                       f"`{TAILSCALE_INSTALL_CMD}`?", True):  # fmt: skip
                print(
                    "Production cannot work without Tailscale: install it, or choose 2) Development. Nothing written."
                )
                return 1
            deploy_flags.append("--install-tailscale")
        ans["tag"] = ask("Device tag", "tag:agentoffice", lambda v: bool(TAG_RE.match(v)), "like tag:agentoffice")
        ans["tailnet"] = ask(
            "Expected tailnet name (Enter: do not check)", "", lambda v: not v or bool(TAILNET_RE.match(v))
        )
        st = tailscale_state_quiet()
        if st == "running":
            print("  this server is already connected to Tailscale: no auth key needed")
        else:
            k = ask_secret(
                "Tailscale auth key (tskey-auth-...)", lambda v: v.startswith("tskey-"), "must start with tskey-"
            )
            if k:
                sec["tailscale_auth_key"] = k
        print("\n-- Healthchecks.io (docs/DEPLOYMENT_SETUP_GUIDE.md §D)")
        ans["check_name"] = ask("Check name", "agentoffice", lambda v: bool(CHECK_NAME_RE.match(v)))
        ans["channels"] = "*"
        if not env_get(paths.env_file, "HC_PING_URL") or yes(
            "HC_PING_URL is already configured. Re-check it with an API key?", False
        ):
            k = ask_secret("Healthchecks.io API key (read-write)")
            if k:
                sec["healthchecks_api_key"] = k
                api = Healthchecks("https://healthchecks.io", k)
                chans = api.channels()
                if not chans:
                    print("  WARNING: the project has no integrations yet; add one (e-mail) before deploying")
                else:
                    print(
                        "  integrations: "
                        + ", ".join(f"{c.get('name') or c.get('kind')} ({c.get('kind')})" for c in chans)
                    )
                    names = [str(c.get("name", "")) for c in chans]
                    ans["channels"] = ask("Integrations to alert (* = all, or comma-separated names)", "*",
                                          lambda v: v == "*" or all(x.strip() in names for x in v.split(",")),
                                          f"choose among {names}")  # fmt: skip
        print("\n-- GitHub webhook (optional; publishes ONLY the webhook endpoint with Tailscale Funnel)")
        if yes("Receive GitHub events (push, PRs, issues, CI) in channels?", True):
            ans["webhook"] = True
            ans["funnel_port"] = ask("Funnel port", "8443", lambda v: v in ("8443", "10000"), "8443 or 10000")
            repos = ask("Repositories (owner/name, comma-separated)", DEFAULT_WEBHOOK_REPO,
                        lambda v: all(REPO_RE.match(x.strip()) for x in v.split(",")), "like owner/repo")  # fmt: skip
            ans["repos"] = [x.strip() for x in repos.split(",")]
            k = ask_secret("GitHub token to create the webhooks (admin:repo_hook / Webhooks read-write)")
            if k:
                sec["github_token"] = k
    text = render_config(ans)
    parse_config(text, "wizard answers")  # same validation as a hand-written file
    print("\nConfiguration:\n\n" + text)
    if not yes(f"Write it to {out} and deploy now?", True):
        print("Nothing written.")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.tmp")
    tmp.write_text(text, "utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, out)
    print(f"written: {out}")
    if os.environ.get("AO_WIZARD_RUN", "1") == "0":  # tests: stop after writing the configuration
        return 0
    with secret_file(json.dumps(sec)) as sf:
        r = subprocess.run(
            [
                "bash",
                str(HERE / "deploy.sh"),
                "--config",
                str(out),
                "--secrets-file",
                str(sf),
                "--non-interactive",
                *deploy_flags,
            ],
            check=False,
        )
    print("\nThe temporary secrets file was removed. Re-running deploy.sh later needs no secrets unless something "
          "must be re-registered.")  # fmt: skip
    return r.returncode


def tailscale_state_quiet() -> str:
    if not shutil.which("tailscale"):
        return "absent"
    r = run(["tailscale", "status", "--json"], timeout=15)
    try:
        return classify_tailscale(json.loads(r.out)).kind
    except ValueError:
        return "unknown"


class _Parser(argparse.ArgumentParser):
    """argparse echoes unknown arguments; a secret pasted on the command line by mistake must not reach the
    terminal or a log, so only option names are shown."""

    def error(self, message: str) -> NoReturn:
        if message.startswith("unrecognized arguments:"):
            opts = [t.split("=", 1)[0] for t in message.split(":", 1)[1].split() if t.startswith("-")]
            message = (
                f"unrecognized arguments: {' '.join(opts) or '(values not shown)'}; secrets are never accepted as "
                "arguments (use --secrets-file or the AO_* environment variables)"
            )
        super().error(redact(message))


def parser() -> argparse.ArgumentParser:
    common = _Parser(add_help=False)
    common.add_argument(
        "--config", help="deployment TOML (default: ./deployment.toml, then /etc/agent-office/deployment.toml)"
    )
    common.add_argument(
        "--secrets-file",
        help=f"JSON secrets, mode 0600 (default: {default_secrets_path()} if present). "
        "Each value may instead come from AO_TAILSCALE_AUTH_KEY / AO_HEALTHCHECKS_API_KEY / AO_GITHUB_WEBHOOK_SECRET / AO_GITHUB_TOKEN",
    )
    common.add_argument("--non-interactive", action="store_true", help="never prompt; fail naming what is missing")
    common.add_argument(
        "--install-tailscale",
        action="store_true",
        help=f"if Tailscale is missing, install it with `{TAILSCALE_INSTALL_CMD}` without asking",
    )
    ap = _Parser(prog="provision.py", description="AgentOffice deployment automation")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser(
        "bootstrap",
        parents=[common],
        help="full deployment",
        description="Deploy AgentOffice on this server: precheck → packages → Python → install.sh → Tailscale → "
        "HTTPS (Serve) → [GitHub webhook] → Healthchecks.io → systemd/backups → live verification report. "
        "Idempotent: re-running detects what is done and does only what is missing.",
    )
    g = b.add_mutually_exclusive_group()
    g.add_argument(
        "--check", action="store_true", help="validate requirements, configuration and credentials; change nothing"
    )
    g.add_argument(
        "--dry-run", action="store_true", help="print the plan of changes; change nothing, call no creation API"
    )
    g.add_argument(
        "--verify", action="store_true", help="verify the existing deployment (PASS/FAIL/NOT_VERIFIED report)"
    )
    b.set_defaults(fn=cmd_bootstrap)
    for name, fn, desc in (
        ("python", cmd_python, "find or provision a Python >= 3.12 and print its path"),
        ("tailscale", cmd_tailscale, "install/register Tailscale and configure private HTTPS (Serve)"),
        ("monitor", cmd_monitor, "create or reuse the Healthchecks.io check and store HC_PING_URL"),
    ):
        s = sub.add_parser(name, parents=[common], help=desc, description=desc)
        s.add_argument("--dry-run", action="store_true", help="print what would change")
        s.set_defaults(fn=fn)
    v = sub.add_parser(
        "verify", parents=[common], help="live verification report", description="live verification report"
    )
    v.set_defaults(
        fn=lambda a: cmd_bootstrap(argparse.Namespace(**{**vars(a), "check": False, "dry_run": False, "verify": True}))
    )
    d = sub.add_parser("detect-os", help="print the detected platform")
    d.set_defaults(fn=cmd_detect)
    w = sub.add_parser("wizard", help="interactive questions, then deploy", description="interactive installation")
    w.add_argument("--config", help="where to write the configuration (default: /etc/agent-office/deployment.toml)")
    w.set_defaults(fn=cmd_wizard)
    return ap


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    try:
        return int(a.fn(a))
    except DeployError as e:
        log(f"ERROR: {e}")
        if e.hint:
            log(f"HINT:  {e.hint}")
        return 1
    except KeyboardInterrupt:
        log("interrupted: re-run the same command to continue (completed steps are detected, not redone)")
        return 130


if __name__ == "__main__":
    sys.exit(main())
