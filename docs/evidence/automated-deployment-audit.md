# Automated deployment — audit (phase 0)

Audit of the deployment tooling on `main` at `5987a92` (after PR #8), written **before** any change, as the plan for
the automated deployment (`deployment/bootstrap.sh`). Results of each later phase are appended at the end.

## 1. Initial state

| Piece | What it does today | Reused as |
|---|---|---|
| `deployment/lib.sh` | Paths (`AO_*`, all overridable), `PYTHON` (default `python3`), `log`/`die`, `sctl` (system or `--user`), `db_path`, `schema_of`, `wait_healthy` (deep health), `render_unit`, `build_release` (git archive + venv + import check), `switch_to`, `prune_releases` | unchanged API; reads the interpreter recorded at install time (see §4) |
| `deployment/install.sh [ref]` | Checks Linux/arch/Python ≥ 3.12 + sqlite ≥ 3.35 + venv/curl, creates the `agent-office` system user, directories and modes, the env file **once** (random admin token), `deploy.conf` (`AO_SRC`), builds+switches a release, renders and enables the 5 units, waits for deep health | the INSTALL stage calls it as is |
| `deployment/update.sh [ref] / --rollback` | New release, pre-update backup, switch, restart, automatic rollback (+ restore if schema migrated) | unchanged; the bootstrap never rebuilds a healthy install (that is `update.sh`'s job) |
| `backup.sh`, `restore.sh` | SQLite online backup + integrity + gzip + sha256, keep 14; verified restore with automatic roll-back | unchanged; the bootstrap only checks the timer and takes a first backup if none exists |
| `healthcheck.sh` + timer (5 min) | Deep health → pings `HC_PING_URL` only when healthy | unchanged; the bootstrap provisions `HC_PING_URL` and triggers one run |
| `verify.sh` | 20 checks of the real scripts under `systemd --user` in a sandbox (CI `deployment` job) | unchanged; still the regression test of install/update/rollback/backup/restore |
| Units | `agent-office.service` (loopback, hardened, `Restart=on-failure`), healthcheck/backup services + timers | unchanged |
| `agentlink/serve.py` | `--host/--port`, env file loader, optional webhook-only listener (`AGENTLINK_WEBHOOK_PORT`, fails closed without `GITHUB_WEBHOOK_SECRET`) | unchanged; the bootstrap only sets the two env keys when the webhook is enabled |
| Docs | DEPLOYMENT.md: manual apt/uv, `tailscale up` interactive, Serve, Funnel for the webhook, manual Healthchecks.io check | rewritten around the bootstrap, manual steps kept as reference |

### Environment variables and defaults

`AO_HOME=/opt/agent-office`, `AO_DATA=/var/lib/agent-office`, `AO_ETC=/etc/agent-office`,
`AO_BACKUP_DIR=/var/backups/agent-office`, `AO_UNIT_DIR=/etc/systemd/system`, `AO_USER=agent-office`, `AO_PORT=8080`,
`AO_URL=http://127.0.0.1:$AO_PORT`, `AO_USER_MODE=0`, `AO_KEEP_RELEASES=5`, `AO_ENV_FILE=$AO_ETC/agent-office.env`,
`PYTHON=python3`, `AO_BACKUP_KEEP=14`. Env file keys: `AGENTLINK_ADMIN_TOKEN`, `AGENTLINK_DB_PATH`,
`AGENTLINK_MODE=production`, `GITHUB_WEBHOOK_SECRET`, `AGENTLINK_WEBHOOK_PORT`, `AO_URL`, `AO_BACKUP_DIR`,
`AO_BACKUP_KEEP`, `HC_PING_URL`.

### Dependency inventory

| Dependency | Minimum | Where it comes from |
|---|---|---|
| Linux + systemd | systemd with `systemctl` | the OS |
| Python | ≥ 3.12, `venv` + `ensurepip`, sqlite3 ≥ 3.35 | apt (Ubuntu 24.04, Debian 13) or a uv-managed interpreter (Debian 12) |
| Python for the bootstrap itself | ≥ 3.11 (`tomllib`) | the distribution `python3` (3.11 on Debian 12) |
| git, curl, ca-certificates | any current | apt |
| gnupg | to check the Tailscale repository key fingerprint | apt |
| iproute2 (`ss`) | listener checks | apt |
| Tailscale | Serve with `--bg` (≥ 1.52); Funnel ≥ 1.38.3 | the official `pkgs.tailscale.com` apt repository |
| Runtime wheels | `requirements.txt` (pinned, pure Python) | pip inside each release venv |

### Credentials today

The admin token is generated once by `install.sh` into the env file (`0640 root:agent-office`) and **printed to
stdout** — captured by any unattended log. Participant tokens are created by the admin through the API/`./admin`.
There is no handling of Tailscale or Healthchecks.io credentials: both were manual console steps.

## 2. Missing functionality

1. A single entry point; a declarative configuration; `--check`, `--dry-run`, `--verify`.
2. OS/arch detection against an explicit support list (today: only `uname`).
3. Automatic package installation; Python 3.12+ on Debian 12.
4. Tailscale install, unattended registration (auth key), Serve, HTTPS checks.
5. Healthchecks.io check creation (Management API v3), `HC_PING_URL` provisioning, alert-channel check.
6. A live verification of the installed host (TLS, Serve, WebSocket through the proxy) with PASS/FAIL/NOT_VERIFIED.
7. Persisting the chosen interpreter: `update.sh` falls back to the system `python3` unless `PYTHON=` is passed again.

## 3. Incompatibilities and findings

| # | Finding | Decision |
|---|---|---|
| F1 | `install.sh` prints the admin token on stdout; an unattended run writes it to a log | print it only when stdout is a terminal (classic interactive use unchanged); otherwise print where it is stored |
| F2 | Docs call the interpreter `AO_PYTHON` (a shell variable) and pass it as `PYTHON=`; the scripts read `PYTHON` only. Consistent, but `update.sh` without `PYTHON=` silently uses the system `python3` (3.11 on Debian 12) | `install.sh` records `AO_PYTHON=` in `deploy.conf`; `lib.sh` uses it when `PYTHON` is not set. `PYTHON` remains the interface |
| F3 | No circular dependencies: every script sources `lib.sh`; `update.sh` → `backup.sh`/`restore.sh`; nothing calls `install.sh` | the bootstrap calls `install.sh`, never the reverse (the wizard runs the bootstrap only after collecting answers) |
| F4 | The prompt asks for "no Funnel" **and** for the GitHub webhook to work (§7.6). GitHub cannot reach a tailnet-only service; the existing solution (PR #8) is a webhook-only loopback listener published with Funnel on 8443 | the API is **never** on Funnel. The webhook is **off by default**; only an explicit `[github_webhook] enabled = true` publishes the webhook-only listener, reusing PR #8 unchanged. `tests/test_webhook_ingress.py` keeps enforcing that only that port can be funnelled |
| F5 | The proposed `deployment/internal/` + `deployment/config/` layout would move files that `update.sh` (renders units from `$NEW/deployment/*.service`), the units' `ExecStart=` paths of installed releases, `verify.sh`, CI and the docs depend on | **not adopted**: new files live flat in `deployment/` next to the existing ones; a release-to-release move would break rollback to an older release |
| F6 | `install.sh` re-run rebuilds and switches the release with no pre-update backup | the bootstrap runs `install.sh` only when there is no healthy install (or the port changed is refused); code updates stay with `update.sh` |
| F7 | Healthchecks.io read-only keys cannot list integrations nor see `ping_url` | require a read-write key; 401/403 diagnosed as "invalid or read-only key" |
| F8 | `unique` on create updates an existing match (docs) — a write on every run | look up first (exact name); write only when missing or different; `unique: ["name"]` kept on create as a duplicate guard |
| F9 | Healthchecks timer period is fixed at 5 min in the unit | `interval_seconds` must be ≥ 300 (otherwise false alerts); units unchanged |

## 4. Files to modify / create

| File | Change |
|---|---|
| `deployment/bootstrap.sh` (new) | entry point: options, root check, makes sure a `python3` ≥ 3.11 exists, runs `provision.py bootstrap` |
| `deployment/deploy.sh` (new) | short alias of the bootstrap for the wizard flow (`install.sh --wizard` → `deploy.sh`) |
| `deployment/provision.py` (new, stdlib only, Python ≥ 3.11) | config/secrets validation, platform detection, stages, Tailscale/Healthchecks.io/GitHub clients, live verification, report |
| `deployment/preflight.sh`, `provision-python.sh`, `provision-tailscale.sh`, `provision-monitor.sh`, `verify-live.sh` (new) | thin entry points of the corresponding `provision.py` sub-commands, usable on their own |
| `deployment/deployment.example.toml`, `bootstrap-secrets.example.json` (new) | examples, fake values only |
| `deployment/install.sh` | F1, F2, `--wizard` |
| `deployment/lib.sh` | F2 |
| `.gitignore` | `deployment.toml`, `bootstrap-secrets.json` never committed |
| `tests/test_deploy_*.py` (new) | unit, simulated integration (fake `tailscale`, fake Healthchecks.io/GitHub APIs, fake apt), security |
| `.github/workflows/ci.yml` | ruff covers `deployment/`; the deployment job runs the end-to-end bootstrap under `systemd --user` |
| docs | new `DEPLOYMENT_SETUP_GUIDE.md`; DEPLOYMENT, ARCHITECTURE, DEVELOPMENT, TESTING, TROUBLESHOOTING, PHASE_ACCEPTANCE, README |

## 5. Regression risks

| Risk | Mitigation |
|---|---|
| `install.sh` behaviour change (token output, `deploy.conf`) | `verify.sh` (20 checks) re-run; the token is still in the env file; interactive output unchanged |
| `lib.sh` interpreter selection | `PYTHON` still wins; recorded value used only if executable |
| Overwriting someone's Tailscale config | the bootstrap never runs `tailscale up` on a registered node, never `serve reset`, refuses a port already serving something else |
| Touching a foreign Healthchecks.io check | exact-name match **and** a management marker in the description; otherwise refuse unless `adopt_existing = true` |
| Secrets in logs | secrets never in argv (Tailscale key via `--auth-key=file:` in a `0700` temp dir), never printed; tests grep every output for them |

## 6. Implementation decisions

- **Python for the logic, Bash for the entry points.** TOML parsing (`tomllib`), JSON APIs, retries and the report
  are far safer in Python than in Bash; the deployment scripts already embed Python. Stdlib only, so the bootstrap
  runs before any virtualenv exists. Config files are parsed as data, never sourced.
- **uv only where needed** (Debian 12): pinned `uv 0.13.9`, downloaded from the GitHub release and checked against
  a pinned SHA-256 before it runs; it installs a managed CPython under `/opt/agent-office/python` (uv verifies the
  interpreter archives against its embedded checksums). The system Python is never replaced.
- **Tailscale from the official apt repository**, with the signing key's primary fingerprint pinned
  (`2596A99EAAB33821893C0A79458CA832957F5868`), not `curl | sh`.
- **Stages re-detect state every run**; `bootstrap.state` only records what finished, for the report and resumption.
