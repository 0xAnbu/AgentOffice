# Deployment (Raspberry Pi or any 64-bit Linux)

One backend instance, SQLite on local disk, systemd, private HTTPS through Tailscale Serve, external alerting through
Healthchecks.io. No Docker, no public port, no domain.

> **Verification status.** The scripts and units below were verified on Linux x86_64 (Kali, systemd 262,
> Python 3.14) under `systemd --user` and by CI. See the [deployment evidence](evidence/README.md). They have **not** yet been run on a Raspberry Pi, and Tailscale
> Serve and a real Healthchecks.io check have not been configured end-to-end. See the checklist at the end.

## Automated deployment

`deployment/deploy.sh` (alias of `bootstrap.sh`) runs every step of this document unattended and verifies the
result. The [setup guide](DEPLOYMENT_SETUP_GUIDE.md) (Spanish, for non-specialists) covers the account preparation;
this section is the reference.

```bash
sudo deployment/preflight.sh  --config /etc/agent-office/deployment.toml --secrets-file /root/agentoffice-secrets.json  # read-only checks
sudo deployment/deploy.sh --dry-run --config … --secrets-file …   # plan, nothing changed, no remote writes
sudo deployment/deploy.sh           --config … --secrets-file …   # deploy (resumable, idempotent)
sudo deployment/verify-live.sh                                     # PASS / FAIL / NOT_VERIFIED report, read-only
sudo deployment/install.sh --wizard                                # interactive: writes the config, asks secrets hidden
```

- **Configuration:** TOML, validated strictly (unknown keys, wrong types and forbidden values such as
  `public_access = true` or `bind_host` other than loopback are rejected, all errors at once). Search order:
  `--config`, `./deployment.toml`, `/etc/agent-office/deployment.toml`. It is copied to
  `/etc/agent-office/deployment.toml` so re-runs and `verify-live.sh` need no flag. Template:
  `deployment/deployment.example.toml`.
- **Secrets:** a JSON file (`--secrets-file`, default `deployment/bootstrap-secrets.json`, gitignored) that must be a
  regular file, mode `600`/`400`, owned by root or the invoking user; or `AO_TAILSCALE_AUTH_KEY`,
  `AO_HEALTHCHECKS_API_KEY`, `AO_GITHUB_TOKEN`, `AO_GITHUB_WEBHOOK_SECRET`. Never on the command line. The file is
  never modified or deleted. Secrets reach `tailscale up` through a private temporary file, and the Healthchecks.io
  API through a request header. They are never written to logs, the state or the report. Only `HC_PING_URL` (and
  the webhook secret, if enabled) is stored, in the `640` env file.
- **Stages** (state in `/etc/agent-office/bootstrap-state.json`): `dependencies` (apt) → `python` (system ≥ 3.12,
  else uv-managed CPython, e.g. on Debian 12) → `install` (`install.sh`, skipped if already installed and healthy) →
  `tailscale` (official apt repository; `tailscale up` with the tag, only if not already connected; refuses a
  different tailnet) → `https` (Serve on `https_port` → `127.0.0.1:<port>`; never overwrites another mapping;
  refuses Funnel on that port) → `github_webhook` (optional, see below) → `monitoring` (Healthchecks.io Management
  API v3: create, reuse or update the check `check_name`; refuses projects with no integrations and checks it did
  not create unless `adopt_existing`) → `services` → verification. A completed stage is re-checked, not redone.
- **Report:** printed and saved to `/etc/agent-office/bootstrap-report.txt`; log in
  `/var/log/agent-office/bootstrap.log`. Each mandatory row is `PASS`, `FAIL` or `NOT_VERIFIED`. The result is
  `SUCCESS` only if all are `PASS`, `FAILED` if any is `FAIL`, otherwise `INCOMPLETE`. TLS is verified against the
  system CAs for the MagicDNS name, never skipped. The API is checked through Serve (health, 401 without a token,
  200 with the admin token), then a WebSocket handshake. Exit code 0 only for `SUCCESS`, otherwise 1 (2 for a usage
  error).
- **Supported:** Ubuntu ≥ 24.04, Debian 12/13, Raspberry Pi OS 64-bit on `x86_64`/`aarch64`; anything else is
  refused by the preflight.
- **Not covered by the verification:** reachability from other devices (see
  [Troubleshooting](TROUBLESHOOTING.md#reachability-from-other-devices)) and off-machine backups.

The sections below describe the same steps done by hand.

## Requirements

- 64-bit Linux (`aarch64` or `x86_64`): Raspberry Pi OS 64-bit (Bookworm or later), Debian 12+, Ubuntu 24.04+. Any
  Raspberry Pi able to run a 64-bit OS (Pi 3B+/4/5, Zero 2 W) should work; no Pi-specific API is used.
- Python **≥ 3.12** with `venv` and SQLite **≥ 3.35** (checked by `install.sh`). Debian 12 ships Python 3.11: use
  Raspberry Pi OS/Debian 13 (Python 3.13) or install 3.12+ (e.g. `uv python install 3.12` and run `install.sh` with
  `PYTHON=/path/to/python3.12`).
- `git`, `curl`. Disk: a few hundred MB (code + venvs + database + 14 backups). RAM: measured peak resident memory of the backend was ~43 MB (x86_64, 24 sessions, 550 messages, 880 events); not yet measured on a Pi.
- Storage: prefer an SSD/USB disk over the SD card for `/var/lib/agent-office` (WAL writes).

## Install

### Recommended: distribution with Python 3.12 or later

On Debian 13, Raspberry Pi OS based on Debian 13, or Ubuntu 24.04:

```bash
sudo apt update
sudo apt install -y git curl python3 python3-venv
python3 --version
AO_PYTHON=/usr/bin/python3
```

### Alternative: Bookworm with managed Python

Install `uv` using the [official installer instructions](https://docs.astral.sh/uv/getting-started/installation/). Store the managed interpreter under `/opt` so the service can read it when systemd protects home directories.

```bash
sudo apt update
sudo apt install -y git curl ca-certificates python3
AO_UV="$(command -v uv)"
sudo env UV_PYTHON_INSTALL_DIR=/opt/agent-office/python "$AO_UV" python install 3.12
AO_PYTHON="$(sudo env UV_PYTHON_INSTALL_DIR=/opt/agent-office/python "$AO_UV" python find --managed-python 3.12)"
"$AO_PYTHON" --version
```

### Install and check the service

Use the interpreter selected above. Keep the path available for updates because releases create a fresh virtual environment.

```bash
sudo git clone https://github.com/0xAnbu/AgentOffice /opt/agent-office/src
sudo env PYTHON="$AO_PYTHON" /opt/agent-office/src/deployment/install.sh
curl -fsS 'http://127.0.0.1:8080/api/health?deep=1'
```

Save the admin token printed on first installation in a secure location.

What it does (idempotent; re-running keeps the database and secrets):

| Path | Content |
|---|---|
| `/opt/agent-office/releases/<utc>-<sha>/` | code + its own `.venv` (one per release) |
| `/opt/agent-office/current` | symlink to the live release |
| `/var/lib/agent-office/agent-office.db` | the database (WAL) — **the only state** |
| `/etc/agent-office/agent-office.env` | secrets/config, `0640 root:agent-office` |
| `/var/backups/agent-office/` | backups (`0700`) |
| `agent-office.service` | the backend, user `agent-office`, `127.0.0.1:8080`, `Restart=on-failure`, hardened |
| `agent-office-healthcheck.timer` | every 5 min, independent of the backend |
| `agent-office-backup.timer` | daily 03:30 (+ random delay), `Persistent=true` |

Logs: `journalctl -u agent-office -f`. Status: `systemctl status agent-office`.

## Private network (Tailscale Serve)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up                                   # log in with the team's tailnet account
sudo tailscale serve --bg --https=443 http://127.0.0.1:8080
tailscale serve status                              # → https://<pi-name>.<tailnet>.ts.net
```

- Enable **MagicDNS** and **HTTPS certificates** in the Tailscale admin console (DNS page) first.
- Do **not** put port 443 / `127.0.0.1:8080` on `tailscale funnel`: that would publish the whole API to the Internet.
  The only thing that may go on Funnel is the webhook-only listener (next section).
- Onboarding each participant: invite them to the tailnet (or share the machine), they install Tailscale and log in;
  then `https://<pi-name>.<tailnet>.ts.net` works from their laptops. Use ACLs to limit access to that machine.
- The backend still requires a token on every API call; Tailscale is an additional layer, not the authentication.
- The backend listens on `127.0.0.1` only; `verify.sh` checks there is no other listener.

## GitHub webhook (Tailscale Funnel, webhook-only listener)

GitHub's servers are not in the tailnet, so a webhook pointed at `https://<pi-name>.<tailnet>.ts.net` (Serve) can
never be delivered. Publishing the API with Funnel would fix that by exposing everything, so instead the backend
opens a **second loopback listener that serves nothing but `POST /api/github/webhook`**, and only that one goes on
Funnel:

```bash
# /etc/agent-office/agent-office.env
GITHUB_WEBHOOK_SECRET=<long random string>        # e.g. python3 -c 'import secrets; print(secrets.token_hex(32))'
AGENTLINK_WEBHOOK_PORT=8081
```

```bash
sudo systemctl restart agent-office
sudo tailscale funnel --bg --https=8443 http://127.0.0.1:8081
tailscale funnel status                             # → https://<pi-name>.<tailnet>.ts.net:8443 (Funnel on)
```

In the GitHub repository: **Settings → Webhooks → Add webhook**, payload URL
`https://<pi-name>.<tailnet>.ts.net:8443/api/github/webhook`, content type `application/json`, the same secret.
Then link a channel to the repo (`--repo owner/repo`); the `ping` delivery should show a green tick.

- Funnel needs the `funnel` node attribute in the tailnet policy (the CLI prints the link to enable it) and only
  accepts ports 443, 8443 and 10000. Port 443 stays on Serve (tailnet only); the webhook uses 8443.
- On port 8081 every other path, method and WebSocket gets `404` / is closed **before routing**, even with a valid
  token; deliveries must carry a valid `X-Hub-Signature-256`. Both are covered by `tests/test_webhook_ingress.py`.
- The backend refuses to start if `AGENTLINK_WEBHOOK_PORT` is set without `GITHUB_WEBHOOK_SECRET`, or equals the
  API port. Without `AGENTLINK_WEBHOOK_PORT` nothing extra listens and nothing is on Funnel.
- To turn it off: `sudo tailscale funnel --https=8443 off`, then remove `AGENTLINK_WEBHOOK_PORT`.

## Tokens

The admin token is in `/etc/agent-office/agent-office.env`. Create one token per agent and one monitor token per
dashboard ([AGENT_INSTALLATION.md](AGENT_INSTALLATION.md#0-the-admin-creates-a-credential-per-agent)). Rotate:
`./admin tokens …` or `POST /api/admin/agents/{name}/tokens` then `DELETE /api/admin/tokens/{id}`. To rotate the
admin token, edit the env file and `systemctl restart agent-office`.

## Alerts (Healthchecks.io)

1. Create a free account at healthchecks.io, add a check "agent-office", period **5 minutes**, grace **10 minutes**.
2. In *Integrations*, set the e-mail address(es) that must be alerted.
3. Put the ping URL in the env file: `HC_PING_URL=https://hc-ping.com/<uuid>` (no restart needed).
4. `sudo systemctl start agent-office-healthcheck.service && journalctl -u agent-office-healthcheck -n 5`
   → "healthy; heartbeat sent"; the check turns green.

`healthcheck.sh` calls `/api/health?deep=1` (process up + SQLite read/write + reconciler loop alive) and pings **only
when all pass**. If the backend, SQLite or the reconciler fail — or the whole Pi is down — no ping is sent and
Healthchecks.io e-mails you after the grace period. The ping is an empty GET: no internal data leaves the machine.
Detection is not instantaneous (period + grace, ≈ 10–15 min). The endpoint is anonymous, so its write probe runs at
most once every 10 s regardless of how often it is called.

## Backup and restore

```bash
sudo -u agent-office /opt/agent-office/current/deployment/backup.sh       # manual backup, prints its path
ls -l /var/backups/agent-office/
sudo /opt/agent-office/current/deployment/restore.sh /var/backups/agent-office/agent-office-<ts>.db.gz
```

Backups use the SQLite online backup API (consistent while the service writes), are verified with
`PRAGMA integrity_check`, gzipped with a `.sha256`, and the newest `AO_BACKUP_KEEP` (14) are kept. Copy them off the
Pi too (e.g. `rsync` to a participant's machine over Tailscale): a backup on the same SD card does not survive the SD
card.

Restore verifies checksum, integrity and schema compatibility **before** stopping anything, refuses to continue if
the service cannot be stopped, moves the current database (and `-wal`/`-shm`) to
`/var/lib/agent-office/replaced-<ts>/`, starts the service and waits for the deep health check. If the restored
database leaves the service unhealthy, the previous one is put back automatically (the restored copy is kept in
`failed-restore-<ts>/`). To verify a backup without restoring:
`gzip -dc file.db.gz > /tmp/x.db && sqlite3 /tmp/x.db 'PRAGMA integrity_check; SELECT MAX(version) FROM schema_version;'`.

## Update and rollback

```bash
sudo env PYTHON="$AO_PYTHON" /opt/agent-office/current/deployment/update.sh            # fetch + deploy origin/main
sudo env PYTHON="$AO_PYTHON" /opt/agent-office/current/deployment/update.sh v0.2.0     # or a tag / commit
sudo /opt/agent-office/current/deployment/update.sh --rollback # back to the previous release
```

`update.sh` builds the new release in its own directory (import check before anything changes), takes a
`pre-update` backup, switches the symlink, restarts and waits 40 s for the deep health check. If it is not healthy
it switches back automatically and, if the new code had migrated the schema, restores the pre-update backup (the
backend refuses to run on a schema newer than its code). `--rollback` does the same on demand. The last 5 releases
are kept.

## Future migration to another machine

1. On the new machine: install as above (it creates a fresh empty database and a new env file).
2. `sudo systemctl stop agent-office` on the new one.
3. On the old one: `backup.sh` and copy the `.db.gz` + `.sha256` and `/etc/agent-office/agent-office.env`.
4. On the new one: put the old env file in place (same tokens) and `restore.sh <file> --yes`.
5. Move the Tailscale Serve config (`tailscale serve --bg --https=443 http://127.0.0.1:8080`), the webhook Funnel if
   used (`tailscale funnel --bg --https=8443 http://127.0.0.1:8081`, then update the GitHub payload URL) and, if the
   hostname changes, run `agentlink connect --server https://<new>…` on each laptop (tokens stay valid).
6. Point `HC_PING_URL` at the same check; stop the old service.

## Verifying a deployment

`bash deployment/verify.sh` (no root; uses `systemd --user` and a throw-away sandbox) checks: install, service
active, secrets mode, auth (no token / wrong token / admin), loopback-only binding, persistence across restart,
automatic restart after SIGKILL, no data loss after the crash, backup under concurrent writes, restore, the
health-check unit sending exactly one heartbeat when healthy and **none** when the app is down, timers installed,
restore refused when the service cannot be stopped, automatic rollback of a restore that leaves the service
unhealthy, update, manual rollback and automatic rollback of a broken release.

### Raspberry Pi acceptance checklist (pending — needs the hardware)

- [ ] `install.sh` on Raspberry Pi OS 64-bit; record `uname -a`, `python3 --version`, `sqlite3 --version`.
- [ ] `deployment/verify.sh` on the Pi (20/20).
- [ ] Reboot the Pi → service and timers come back; data persists.
- [ ] Tailscale Serve: HTTPS from two participant laptops; `curl` from outside the tailnet fails.
- [ ] Webhook Funnel: GitHub `ping` delivered; from outside the tailnet `:8443/api/health` → 404 and `:443` unreachable.
- [ ] Healthchecks.io: green when healthy; stop the service → alert e-mail received after the grace period.
- [ ] Restore a backup copied off the Pi.
