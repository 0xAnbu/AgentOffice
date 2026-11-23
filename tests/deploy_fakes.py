"""Test doubles for the automated deployment (deployment/provision.py).

Everything here imitates an external system closely enough to exercise the real code paths, and nothing more:

- FakeHealthchecks: an HTTP server speaking the subset of the Healthchecks.io Management API v3 the bootstrap uses
  (X-Api-Key auth, /channels/, /checks/ list/create with `unique`, /checks/<uuid> update, /ping/<uuid>), with failure
  modes (invalid key, unavailable, rate limit, incomplete responses, no integrations, creation failure).
- A fake `tailscale` CLI (a stateful script on PATH) for status/up/serve/funnel. `serve`/`funnel` start a real TLS
  byte-pipe proxy on the fake Tailscale IP with a certificate from a throw-away CA, so TLS verification, the HTTP API
  and WebSockets are tested through a proxy exactly like through Tailscale Serve (the CA is trusted via SSL_CERT_FILE).
- Fake `dpkg-query` / `apt-get` for package-stage tests.

They do NOT prove anything about the real Tailscale or Healthchecks.io services: those checks stay NOT_VERIFIED in
the evidence until they are run on a real server.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import textwrap
import threading
import uuid as uuidlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
PROVISION = REPO / "deployment" / "provision.py"


def load_provision() -> Any:
    spec = importlib.util.spec_from_file_location("ao_provision", PROVISION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ao_provision"] = mod
    spec.loader.exec_module(mod)
    return mod


# -- Healthchecks.io ------------------------------------------------------------------------------------------------


class FakeHealthchecks:
    def __init__(self, key: str = "hcw_fake_rw_key_0123456789", mode: str = "ok") -> None:
        self.key = key
        self.mode = mode
        self.channels: list[dict[str, Any]] = [
            {"id": "4ec5a071-2d08-4baa-898a-eb4eb3cd6941", "name": "ops-mail", "kind": "email"},
            {"id": "6a3b27c7-58d0-4e3d-b6a7-d3d0b0c0f1aa", "name": "ops-signal", "kind": "signal"},
        ]
        self.checks: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str, Any]] = []
        self.pings = 0
        self.rate_limited = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def creates(self) -> int:
        return sum(1 for m, p, _ in self.requests if m == "POST" and p == "/api/v3/checks/")

    def updates(self) -> int:
        return sum(1 for m, p, _ in self.requests if m == "POST" and re.match(r"^/api/v3/checks/[0-9a-f-]{36}$", p))

    def add_check(self, name: str, **kw: Any) -> dict[str, Any]:
        u = str(uuidlib.uuid4())
        c = {
            "name": name,
            "slug": name,
            "tags": "",
            "desc": "",
            "grace": 3600,
            "timeout": 86400,
            "channels": "",
            "status": "new",
            "uuid": u,
            "ping_url": f"{self.url}/ping/{u}",
            "update_url": f"{self.url}/api/v3/checks/{u}",
            **kw,
        }
        self.checks.append(c)
        return c

    def _view(self, c: dict[str, Any]) -> dict[str, Any]:
        if self.mode == "incomplete":
            return {k: v for k, v in c.items() if k not in ("ping_url", "uuid", "update_url")}
        return dict(c)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _send(self, code: int, body: Any = None, headers: dict[str, str] | None = None) -> None:
                raw = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> Any:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else None

            def do_GET(self) -> None:
                self._route("GET")

            def do_POST(self) -> None:
                self._route("POST")

            def _route(self, method: str) -> None:
                body = self._body() if method == "POST" else None
                path = self.path.split("?")[0]
                if path.startswith("/ping/"):
                    u = path.rsplit("/", 1)[-1]
                    for c in fake.checks:
                        if c["uuid"] == u:
                            c["status"] = "up"
                            fake.pings += 1
                            self.send_response(200)
                            self.send_header("Content-Length", "2")
                            self.end_headers()
                            self.wfile.write(b"OK")
                            return
                    self._send(404, {"error": "not found"})
                    return
                fake.requests.append((method, path, body))
                if fake.mode == "unavailable":
                    self._send(503, {"error": "maintenance"})
                    return
                if fake.mode == "rate_limit" and fake.rate_limited < 2:
                    fake.rate_limited += 1
                    self._send(429, {"error": "rate limited"}, {"Retry-After": "0"})
                    return
                if self.headers.get("X-Api-Key") != fake.key:
                    self._send(401, {"error": "wrong api key"})
                    return
                if method == "GET" and path == "/api/v3/channels/":
                    self._send(200, {"channels": [] if fake.mode == "no_channels" else fake.channels})
                elif method == "GET" and path == "/api/v3/checks/":
                    self._send(200, {"checks": [fake._view(c) for c in fake.checks]})
                elif method == "POST" and path == "/api/v3/checks/":
                    if fake.mode == "post_500":
                        self._send(500, {"error": "boom"})
                        return
                    unique = (body or {}).get("unique") or []
                    for c in fake.checks:
                        if unique and all(c.get(k) == body.get(k) for k in unique):
                            c.update({k: v for k, v in body.items() if k != "unique"})
                            self._send(200, fake._view(c))
                            return
                    c = fake.add_check(body["name"], **{k: v for k, v in body.items() if k not in ("unique", "name")})
                    self._send(201, fake._view(c))
                elif method == "POST" and path.startswith("/api/v3/checks/"):
                    u = path.rsplit("/", 1)[-1]
                    for c in fake.checks:
                        if c["uuid"] == u:
                            c.update(body or {})
                            self._send(200, fake._view(c))
                            return
                    self._send(404, {"error": "not found"})
                else:
                    self._send(404, {"error": "no such endpoint"})

        return H


# -- Tailscale ------------------------------------------------------------------------------------------------------

FAKE_TAILSCALE = r'''#!/usr/bin/env python3
"""Fake tailscale CLI for tests. State in $FAKE_TS_STATE (JSON). Records every argv in state["calls"]."""
import json, os, subprocess, sys
S = os.environ["FAKE_TS_STATE"]
st = json.load(open(S))
argv = sys.argv[1:]
st.setdefault("calls", []).append(argv)
def save():
    tmp = S + ".tmp"
    json.dump(st, open(tmp, "w"), indent=1)
    os.replace(tmp, S)
def out(obj):
    print(json.dumps(obj, indent=2))
def dns():
    return st["hostname"] + "." + st["suffix"]
if argv[:2] == ["status", "--json"]:
    save()
    if st.get("daemon") is False:
        print("failed to connect to local tailscaled; it doesn't appear to be running", file=sys.stderr); sys.exit(1)
    running = st["backend"] == "Running"
    out({
        "Version": "1.90.0-fake",
        "BackendState": st["backend"],
        "TailscaleIPs": [st["ip"]] if running else [],
        "Self": {"DNSName": dns() + "." if running else "", "HostName": st["hostname"], "Online": running,
                 "TailscaleIPs": [st["ip"]] if running else [], "Tags": st.get("tags", [])},
        "CurrentTailnet": {"Name": st["tailnet"], "MagicDNSSuffix": st["suffix"], "MagicDNSEnabled": st.get("magicdns", True)}
                          if running else None,
        "MagicDNSSuffix": st["suffix"],
        "CertDomains": [dns()] if running and st.get("https", True) else None,
        "Health": [],
    })
    sys.exit(0)
if argv and argv[0] == "up":
    opts = dict(a[2:].split("=", 1) for a in argv[1:] if a.startswith("--") and "=" in a)
    keyspec = opts.get("auth-key", "")
    key = open(keyspec[5:]).read().strip() if keyspec.startswith("file:") else keyspec
    st["key_was_file"] = keyspec.startswith("file:")
    st["key_file"] = keyspec[5:] if keyspec.startswith("file:") else ""
    behaviour = st.get("up", "ok")
    if key != st.get("valid_key"):
        save(); print("backend error: invalid key: API key does not exist", file=sys.stderr); sys.exit(1)
    if behaviour == "bad_tag":
        save(); print("backend error: requested tags [%s] are invalid or not permitted" % opts.get("advertise-tags"), file=sys.stderr); sys.exit(1)
    if behaviour == "timeout":
        save(); print("timeout waiting for Tailscale service to enter a Running state; check health with \"tailscale status\"", file=sys.stderr); sys.exit(1)
    st["hostname"] = opts.get("hostname", st["hostname"])
    st["tags"] = [t for t in opts.get("advertise-tags", "").split(",") if t]
    st["backend"] = "NeedsMachineAuth" if behaviour == "needs_approval" else "Running"
    save(); sys.exit(0)
if argv[:3] == ["serve", "status", "--json"]:
    save(); out(st.get("serve") or {}); sys.exit(0)
if argv and argv[0] in ("serve", "funnel"):
    args = [a for a in argv[1:] if a != "--bg"]
    port = next(a.split("=", 1)[1] for a in args if a.startswith("--https="))
    target = [a for a in args if not a.startswith("--")][-1]
    if st.get("serve_fails"):
        save(); print("error: " + st["serve_fails"], file=sys.stderr); sys.exit(1)
    sv = st.setdefault("serve", {})
    sv.setdefault("TCP", {})[port] = {"HTTPS": True}
    sv.setdefault("Web", {})[dns() + ":" + port] = {"Handlers": {"/": {"Proxy": target}}}
    if argv[0] == "funnel":
        sv.setdefault("AllowFunnel", {})[dns() + ":" + port] = True
    tport = target.rsplit(":", 1)[1].strip("/")
    p = subprocess.Popen([sys.executable, os.environ["FAKE_TS_PROXY"], st["ip"], port, tport, os.environ["FAKE_TS_CERT"],
                          os.environ["FAKE_TS_KEY"]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    st.setdefault("proxies", []).append(p.pid)
    save()
    import socket, time
    for _ in range(100):  # like the real CLI, return once the listener is up
        try:
            socket.create_connection((st["ip"], int(port)), 0.2).close(); break
        except OSError:
            time.sleep(0.05)
    sys.exit(0)
save()
print("fake tailscale: unsupported " + " ".join(argv), file=sys.stderr)
sys.exit(2)
'''

TLS_PROXY = r'''#!/usr/bin/env python3
"""TLS-terminating byte pipe: listen_ip listen_port target_port cert key (like Tailscale Serve, minus the tailnet)."""
import asyncio, ssl, sys
ip, port, tport, cert, key = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5]
ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
ctx.load_cert_chain(cert, key)
async def pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except Exception:
        pass
    finally:
        w.close()
async def handle(r, w):
    try:
        ur, uw = await asyncio.open_connection("127.0.0.1", tport)
    except OSError:
        w.close(); return
    await asyncio.gather(pipe(r, uw), pipe(ur, w))
async def main():
    srv = await asyncio.start_server(handle, ip, port, ssl=ctx)
    async with srv:
        await srv.serve_forever()
asyncio.run(main())
'''


def make_ca(d: Path, dns_name: str) -> dict[str, Path]:
    """Throw-away CA + leaf certificate for `dns_name` (openssl CLI)."""
    d.mkdir(parents=True, exist_ok=True)
    ca_key, ca_crt, key, csr, crt = (d / n for n in ("ca.key", "ca.crt", "leaf.key", "leaf.csr", "leaf.crt"))
    ext = d / "ext.cnf"
    ext.write_text(f"subjectAltName=DNS:{dns_name}\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\n"
                   "extendedKeyUsage=serverAuth\n")  # fmt: skip
    q = {"check": True, "capture_output": True}
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(ca_key), "-out",
                    str(ca_crt), "-days", "2", "-subj", "/O=AgentOffice Test CA/CN=test-ca",
                    "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign"], **q)  # fmt: skip
    subprocess.run(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(csr), "-subj",
                    f"/CN={dns_name}"], **q)  # fmt: skip
    subprocess.run(["openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca_crt), "-CAkey", str(ca_key),
                    "-CAcreateserial", "-out", str(crt), "-days", "2", "-extfile", str(ext)], **q)  # fmt: skip
    return {"ca": ca_crt, "cert": crt, "key": key}


class FakeTailscale:
    """Install the fake CLI in `bin_dir` and expose its state."""

    def __init__(self, root: Path, *, backend: str = "NeedsLogin", valid_key: str = "tskey-auth-kFAKEvalid0CNTRL-abcdef",
                 tailnet: str = "example.com", ip: str = "127.0.0.2", **extra: Any) -> None:  # fmt: skip
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.state_file = root / "ts-state.json"
        (self.bin / "tailscale").write_text(FAKE_TAILSCALE)
        (self.bin / "tailscale").chmod(0o755)
        self.proxy = root / "tls_proxy.py"
        self.proxy.write_text(TLS_PROXY)
        self.valid_key = valid_key
        self.dns = "agentoffice.tail1234.ts.net"
        self.certs = make_ca(root / "pki", self.dns)
        self.write({"backend": backend, "valid_key": valid_key, "tailnet": tailnet, "suffix": "tail1234.ts.net",
                    "hostname": "agentoffice", "ip": ip, "tags": [], **extra})  # fmt: skip

    def env(self) -> dict[str, str]:
        return {
            "FAKE_TS_STATE": str(self.state_file),
            "FAKE_TS_PROXY": str(self.proxy),
            "FAKE_TS_CERT": str(self.certs["cert"]),
            "FAKE_TS_KEY": str(self.certs["key"]),
            "SSL_CERT_FILE": str(self.certs["ca"]),
        }

    def read(self) -> dict[str, Any]:
        return json.loads(self.state_file.read_text())

    def write(self, st: dict[str, Any]) -> None:
        self.state_file.write_text(json.dumps(st))

    def update(self, **kw: Any) -> None:
        st = self.read()
        st.update(kw)
        self.write(st)

    def calls(self, verb: str) -> list[list[str]]:
        return [c for c in self.read().get("calls", []) if c and c[0] == verb]

    def stop_proxies(self) -> None:
        import os
        import signal

        for pid in self.read().get("proxies", []):
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass


# -- apt ------------------------------------------------------------------------------------------------------------


def fake_apt(bin_dir: Path, *, missing: tuple[str, ...] = (), apt_fails: str = "") -> Path:
    """dpkg-query reporting `missing` as not installed; apt-get logging calls and optionally failing."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    log = bin_dir / "apt.log"
    miss = " ".join(missing)
    (bin_dir / "dpkg-query").write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        pkg="${{@: -1}}"
        for m in {miss}; do [ "$pkg" = "$m" ] && {{ echo "dpkg-query: no packages found matching $pkg" >&2; exit 1; }}; done
        printf 'install ok installed'
        """))  # fmt: skip
    (bin_dir / "apt-get").write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        echo "$*" >> {log}
        {"echo 'E: " + apt_fails + "' >&2; exit 100" if apt_fails else "exit 0"}
        """))  # fmt: skip
    for n in ("dpkg-query", "apt-get"):
        (bin_dir / n).chmod(0o755)
    return log


# -- GitHub (repository webhooks API) -------------------------------------------------------------------------------


class FakeGitHub:
    """GET/POST /repos/<o>/<r>/hooks, PATCH/GET /repos/<o>/<r>/hooks/<id> with bearer auth."""

    def __init__(
        self,
        token: str = "github_pat_FAKE0123456789abcdefghij",
        resolve: dict[str, str] | None = None,
        cafile: str | None = None,
    ) -> None:
        self.token = token
        self.cafile = cafile
        self.resolve = resolve  # when set, each created/updated hook gets a real signed ping delivery
        self.hooks: dict[str, list[dict[str, Any]]] = {}
        self.requests: list[tuple[str, str, Any]] = []
        self.next_id = 1000
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _send(self, code: int, body: Any) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _do(self, method: str) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                path = self.path.split("?")[0]
                fake.requests.append((method, path, body))
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    self._send(401, {"message": "Bad credentials"})
                    return
                m = re.match(r"^/repos/([^/]+/[^/]+)/hooks(?:/(\d+))?$", path)
                if not m:
                    self._send(404, {"message": "Not Found"})
                    return
                repo, hid = m.group(1), m.group(2)
                hooks = fake.hooks.setdefault(repo, [])
                if hid is None and method == "GET":
                    self._send(200, hooks)
                elif hid is None and method == "POST":
                    fake.next_id += 1
                    h = {"id": fake.next_id, "active": body["active"], "events": body["events"], "config": body["config"],
                         "last_response": {"code": None, "status": "unused", "message": None}}  # fmt: skip
                    hooks.append(h)
                    fake._ping(repo, h)
                    self._send(201, h)
                else:
                    h = next((x for x in hooks if str(x["id"]) == hid), None)
                    if h is None:
                        self._send(404, {"message": "Not Found"})
                    elif method == "PATCH":
                        h.update({k: v for k, v in body.items() if k != "config"})
                        h["config"].update(body.get("config") or {})
                        fake._ping(repo, h)
                        self._send(200, h)
                    else:
                        self._send(200, h)

            def do_GET(self) -> None:
                self._do("GET")

            def do_POST(self) -> None:
                self._do("POST")

            def do_PATCH(self) -> None:
                self._do("PATCH")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _ping(self, repo: str, h: dict[str, Any]) -> None:
        if self.resolve is None:
            return
        code, msg = deliver_ping(h["config"]["url"], h["config"]["secret"], repo, h["id"], self.resolve, self.cafile)
        h["last_response"] = {"code": code or None, "status": "active" if code == 200 else "failed", "message": msg}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def deliver_ping(
    url: str, secret: str, repo: str, hook_id: int, resolve: dict[str, str], cafile: str | None = None
) -> tuple[int, str]:
    """Deliver a signed `ping` like GitHub does after creating a hook, over verified TLS (resolve maps names to IPs)."""
    import hashlib
    import hmac
    import http.client
    import socket
    import ssl
    import urllib.parse

    u = urllib.parse.urlsplit(url)
    host, port = u.hostname or "", u.port or 443
    body = json.dumps(
        {"zen": "Keep it logically awesome.", "hook_id": hook_id, "repository": {"full_name": repo}}
    ).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    ctx = ssl.create_default_context(cafile=cafile)
    try:
        raw = socket.create_connection((resolve.get(host, host), port), 10)
        conn = http.client.HTTPSConnection(host, port, context=ctx)
        conn.sock = ctx.wrap_socket(raw, server_hostname=host)
        conn.request("POST", u.path, body=body, headers={"Content-Type": "application/json", "X-GitHub-Event": "ping",
                     "X-GitHub-Delivery": str(uuidlib.uuid4()), "X-Hub-Signature-256": sig})  # fmt: skip
        r = conn.getresponse()
        r.read()
        conn.close()
        return r.status, "OK" if r.status < 300 else r.reason
    except (OSError, ssl.SSLError) as e:
        return 0, f"delivery failed: {e}"
