# Automated deployment — validation (phases 1–7)

Results of the automated deployment work planned in [automated-deployment-audit.md](automated-deployment-audit.md).
Runs on 2026-11-23, Linux x86_64 (Kali Rolling), Python 3.14.8, systemd 262. The code tested is the
`feat/automated-deployment` working tree on top of `5987a92`, i.e. the commit of this document. Raw outputs:

- [`automated-deployment-tests-20261123.log`](automated-deployment-tests-20261123.log): every deploy test with its
  result, the end-to-end run and the whole-suite summary.
- [`deployment-verify-20261123T114215Z.log`](deployment-verify-20261123T114215Z.log): `deployment/verify.sh`, 20/20,
  re-run after the `install.sh`/`lib.sh` changes (regression check of the existing deployment).

Nothing below was executed against the real Tailscale, Healthchecks.io or GitHub services, nor on a Raspberry Pi.
Those levels are listed as **NOT_VERIFIED**, not as passed.

## Results by level

| Level | What | Result |
|---|---|---|
| 1. Unit | `tests/test_deploy_unit.py` | 119 passed |
| 2. Simulated services | `tests/test_deploy_integration.py` (fake `tailscale` with real TLS through a test CA, fake Healthchecks.io v3, fake GitHub, fake apt, uv checksum, non-apt / no Tailscale repository / non-glibc, Tailscale install prompt and script) | 60 passed |
| 3. Local integration | `AO_DEPLOY_E2E=1 tests/test_deploy_e2e.py`: real `install.sh`, real units under `systemd --user`, fakes for external services | 1 passed (~10 s) |
| — | Contract `tests/test_webhook_ingress.py` (no doc or script puts the API on Funnel) | passed, docs included |
| — | Whole suite | 346 passed, 2 skipped (node not installed; e2e without opt-in) |
| — | `deployment/verify.sh` | 20/20 |
| — | `ruff check`, `ruff format --check` (repo incl. `deployment/`), `mypy`, `mypy --python-version 3.11 deployment/provision.py` | clean |
| 4. Real Tailscale / Healthchecks.io / GitHub | — | **NOT_VERIFIED** |
| 5. Real Raspberry Pi (and reboot) | — | **NOT_VERIFIED** |

What the end-to-end test proves (level 3), in three runs of the same `deploy.sh` command:

1. **Interrupted:** Healthchecks.io answers 500. The run fails at `monitoring` with `install` and `tailscale` recorded
   as done, one `tailscale up`, no check created.
2. **Resumed:** `SUCCESS`. `install.sh` is not re-run ("already installed and healthy"; same release, same admin
   token), there is no second `tailscale up`, exactly one check is created and pinged, the GitHub hook is created
   and its signed ping is delivered over verified TLS (HTTP 200), the service is active and a backup exists.
3. **Repeated:** `SUCCESS` with zero remote writes: no check created or updated, no `tailscale up`, no Serve or
   Funnel change, no GitHub POST/PATCH. Same release, same token, same database file (inode), and a message written
   between runs is still there.

Then `verify-live.sh` exits 0. Across all outputs, logs, the state file and the report, none of the six secrets
appears (Tailscale key, Healthchecks key, GitHub token, admin token, ping URL, webhook secret). The secrets file is
unchanged and still `0600`, and the env file holds no external API key.

## Not verified (needs real services or hardware)

| Item | How to verify |
|---|---|
| `tailscale up` with a real tagged, pre-approved auth key; tag refused / approval pending messages from the real control plane | Guide §C and §G on a fresh server |
| Real Let's Encrypt certificate through Tailscale Serve, validated against system CAs | Report row `TLS Certificate` on a real run |
| Healthchecks.io v3: check creation, channel assignment, `up` after the first heartbeat, alert e-mail on `down` | Guide §D/§H; stop the service > 15 min |
| GitHub webhook created through the real API, delivery through Funnel | Only with `github_webhook.enabled` |
| Debian 12 path with uv-managed CPython on real Debian 12; Ubuntu 24.04; Raspberry Pi OS 64-bit on aarch64 | Run the guide on each |
| Untested distributions (non-apt, derivatives, musl) beyond the precheck and stage logic | Only unit/simulated coverage |
| The real `https://tailscale.com/install.sh` run by the bootstrap | Only a local script is piped to `sh` in tests; Tailscale is already installed on the dev machine |
| Reboot: service, timers and Tailscale Serve come back | `sudo reboot`, then `verify-live.sh` |
| Reachability from a participant device; no access from outside the tailnet | Manual (guide §L); **excluded** from automation by scope |
| Off-machine backups | **Excluded** by scope (local backups only) |

## Phase contracts

```yaml
phase:
  id: 0
  name: "Audit of the existing deployment"
  status: PASS
deliverables: ["docs/evidence/automated-deployment-audit.md"]
verification:
  passed: ["existing deployment re-verified before changes (verify.sh 20/20 at 5987a92)"]
  failed: []
  not_verified: []
regressions: {detected: 0, unresolved: 0}
security: ["gaps listed: no unattended path, manual Tailscale/Healthchecks steps, no live verification"]
limitations: []
evidence: ["docs/evidence/automated-deployment-audit.md"]
decision: {accepted: true, reason: "Plan, risks and file list written before any change"}
---
phase:
  id: 1
  name: "Bootstrap and declarative configuration"
  status: PASS
deliverables:
  - "deployment/bootstrap.sh, deploy.sh, preflight.sh, verify-live.sh, provision.py"
  - "deployment/deployment.example.toml, deployment/bootstrap-secrets.example.json"
verification:
  passed:
    - "--help, --check, --dry-run, --verify, --non-interactive, --config, --secrets-file (test_help_of_every_entry_point and unit tests)"
    - "strict TOML schema: unknown keys/types/forbidden values rejected, all errors listed at once"
    - "secrets file: refused if not 0600/0400, a symlink, foreign-owned, or holding REPLACE_ME; never modified"
    - "secrets refused as CLI arguments; no set -x in entry points"
    - "dry-run makes no change and no remote request"
  failed: []
  not_verified: []
regressions: {detected: 0, unresolved: 0}
security: ["secrets only from a protected file or AO_* env vars", "redaction of every known secret in all output"]
limitations: []
evidence: ["docs/evidence/automated-deployment-tests-20261123.log"]
decision: {accepted: true, reason: "All entry points and validation covered by tests"}
---
phase:
  id: 2
  name: "Linux preparation"
  status: PASS
deliverables: ["dependencies and python stages", "deployment/provision-python.sh", "install.sh records AO_PYTHON in deploy.conf; lib.sh reuses it"]
verification:
  passed:
    - "platform matrix: any 64-bit Linux allowed; Ubuntu >= 24.04, Debian 12/13, Raspberry Pi OS 64-bit tested (PASS), others WARN; 32-bit, 32-bit userland, other architectures refused"
    - "missing apt packages installed, present ones not reinstalled, apt errors reported; without apt the required commands are checked"
    - "uv pinned and SHA-256 checked; wrong checksum never executed"
    - "verify.sh 20/20 after the install.sh/lib.sh changes"
  failed: []
  not_verified: ["real Debian 12 with uv-managed CPython", "real Ubuntu 24.04", "Raspberry Pi OS aarch64"]
regressions: {detected: 0, unresolved: 0}
security: ["system Python never replaced", "Tailscale apt key fingerprint pinned"]
limitations: ["validated on Kali with a simulated os-release"]
evidence: ["docs/evidence/automated-deployment-tests-20261123.log", "docs/evidence/deployment-verify-20261123T114215Z.log"]
decision: {accepted: true, reason: "Behaviour covered locally; distribution runs pending real machines"}
---
phase:
  id: 3
  name: "Tailscale and private HTTPS"
  status: PASS
deliverables: ["tailscale and https stages", "deployment/provision-tailscale.sh"]
verification:
  passed:
    - "auth key passed as --auth-key=file: from a private temp dir; never in argv"
    - "registered device kept (no second tailscale up); wrong tailnet refused without changes"
    - "invalid/expired/used key, tag refused, pending approval, stopped, MagicDNS/HTTPS disabled: diagnosed with a hint"
    - "Serve configured on https_port -> 127.0.0.1:<port>; foreign mapping not overwritten; API on Funnel = security FAIL"
    - "TLS verified against the CA bundle for the MagicDNS name (test_tls_verification_is_never_disabled); WebSocket handshake through Serve"
  failed: []
  not_verified: ["real tailnet, real auth key, real Let's Encrypt certificate"]
regressions: {detected: 0, unresolved: 0}
security: ["API never published with Funnel", "no curl -k anywhere"]
limitations: ["control-plane errors are matched on the CLI messages of the fake, modelled on documented behaviour"]
evidence: ["docs/evidence/automated-deployment-tests-20261123.log"]
decision: {accepted: true, reason: "All paths covered against a faithful fake; real tailnet pending"}
---
phase:
  id: 4
  name: "Healthchecks.io"
  status: PASS
deliverables: ["monitoring stage (Management API v3)", "deployment/provision-monitor.sh"]
verification:
  passed:
    - "check created when absent, reused without writes, updated in place when outdated"
    - "foreign or duplicate check refused (adopt_existing to take over)"
    - "project with no integrations refused; named channels resolved"
    - "401/403 not retried; 429/5xx retried with backoff"
    - "only HC_PING_URL stored; API key never written to disk"
  failed: []
  not_verified: ["real Healthchecks.io project, alert e-mail"]
regressions: {detected: 0, unresolved: 0}
security: ["API key sent only in the X-Api-Key header over HTTPS"]
limitations: []
evidence: ["docs/evidence/automated-deployment-tests-20261123.log"]
decision: {accepted: true, reason: "Create/reuse/update semantics verified against a fake v3 API"}
---
phase:
  id: 5
  name: "Integration, live verification and report"
  status: PASS
deliverables: ["verify-live.sh", "AGENTOFFICE DEPLOYMENT REPORT (SUCCESS/INCOMPLETE/FAILED)", "install.sh --wizard", "resumable state file"]
verification:
  passed:
    - "end-to-end: interrupted run resumed, then SUCCESS, then a repeat run with zero remote writes and the same DB, token and release"
    - "rows PASS/FAIL/NOT_VERIFIED; INCOMPLETE when a mandatory row cannot be verified"
    - "secrets absent from every output, log, state and report"
  failed: []
  not_verified: ["report against real services", "reboot"]
regressions: {detected: 0, unresolved: 0}
security: ["report and log redacted; admin token never printed unattended"]
limitations: ["no check from a second device (excluded by scope)"]
evidence: ["docs/evidence/automated-deployment-tests-20261123.log"]
decision: {accepted: true, reason: "install.sh + deploy.sh reach a verified SUCCESS locally"}
---
phase:
  id: 6
  name: "Documentation"
  status: PASS
deliverables:
  - "docs/DEPLOYMENT_SETUP_GUIDE.md (Spanish, sections A-L)"
  - "README, DEPLOYMENT, ARCHITECTURE, DEVELOPMENT, TESTING, TROUBLESHOOTING, PHASE_ACCEPTANCE updated"
verification:
  passed:
    - "official links checked (HTTP 200) on 2026-11-23; raspberrypi.com refuses automated clients (403), linked as official"
    - "test_webhook_ingress scans every doc: no API on Funnel, every Serve/Funnel command well-formed"
    - "commands in the guide match the entry points' flags and the real output strings"
  failed: []
  not_verified: ["guide followed end to end by a non-specialist on a clean server"]
regressions: {detected: 0, unresolved: 0}
security: ["only fictitious secret values in examples"]
limitations: ["both scope exclusions documented: external connectivity check and off-machine backups"]
evidence: ["docs/DEPLOYMENT_SETUP_GUIDE.md"]
decision: {accepted: true, reason: "Guide self-contained and consistent with the technical reference"}
---
phase:
  id: 7
  name: "Tests, CI and evidence"
  status: PASS
deliverables: ["tests/test_deploy_unit.py", "tests/test_deploy_integration.py", "tests/test_deploy_e2e.py", "tests/deploy_fakes.py", ".github/workflows/ci.yml", "this file"]
verification:
  passed: ["346 passed, 2 skipped", "e2e 1 passed", "verify.sh 20/20", "ruff, mypy (3.14 and 3.11 target) clean"]
  failed: []
  not_verified: ["level 4 (real external services)", "level 5 (real Raspberry Pi)", "the new CI job until the PR runs it"]
regressions: {detected: 0, unresolved: 0}
security: ["secret-leak assertions in unit, integration and e2e tests"]
limitations: ["levels 4 and 5 need real accounts and hardware"]
evidence: ["docs/evidence/automated-deployment-tests-20261123.log", "docs/evidence/deployment-verify-20261123T114215Z.log"]
decision: {accepted: true, reason: "Levels 1-3 green; levels 4-5 honestly NOT_VERIFIED"}
```
