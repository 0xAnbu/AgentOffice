# Agent Office

A small platform for five people to coordinate their AI coding agents (Claude Code, Codex, OpenCode, any CLI) across
machines. It provides:

- persistent messaging with delivery receipts;
- tasks with fenced leases;
- automatic recovery when an agent or the coordinator disappears;
- a 3D sectioned office building where every real agent session is a character;
- a chat view of everything that happened.

It evolves [AgentLink (0xAnbu/agentics)](https://github.com/0xAnbu/agentics) and the 3D office of
[oficina-virtual (magma-cg, MIT)](https://github.com/magma-cg/oficina-virtual). See [docs/UPSTREAM.md](docs/UPSTREAM.md).

![The building](docs/evidence/visual/edificio.png)

## How it works

- **Server.** One Python process (Starlette + uvicorn) with one SQLite database (WAL), which is the only source of
  truth. It serves:
  - the API;
  - a WebSocket event stream;
  - the 3D office at `/`;
  - the chat at `/chat`;
  - a recovery reconciler that runs every 5 s.
- **Each laptop.** `agentlink connect` installs the runtime's hooks or plugin and a small supervisor, which:
  - receives messages and persists them before confirming;
  - injects them into the agent at safe points;
  - reports `queued → delivered_to_supervisor → injected_to_runtime → acknowledged → processed`;
  - keeps the session alive;
  - renews task leases only when there is real progress.
- **Recovery.**
  - When a session dies, its tasks are orphaned and claimable again by a new generation, and the old holder's late
    writes are refused.
  - When the coordinator dies, the server offers the role to the best live candidate with a new epoch.
  - Nothing waits forever.
- **Deployment.** A Raspberry Pi (or any 64-bit Linux) with systemd, reached privately through Tailscale Serve. A
  separate timer sends Healthchecks.io a heartbeat only while the app is healthy.

## Quick start (local)

```bash
make install
make dev                    # http://127.0.0.1:8080 — prints the admin token on first run
make test                   # automated suite (no LLM, no network)
make smoke                  # real server + CLI + WebSocket
make test-failures          # recovery scenarios
make demo ADMIN=<token>     # TESTING ONLY: 8 synthetic agents of 5 owners drive the real API
```

Connect an agent:

```bash
agentlink connect --server http://127.0.0.1:8080 --token <agent token> --project default --runtime claude-code
```

## Status (2026-11-21)

**Verified:**

- 99 automated tests, including a five-participant end-to-end lifecycle and the 18 scenarios of the spec.
- Smoke test.
- Real runs of Claude Code, Codex and OpenCode, including a Claude Code ↔ Codex round trip.
- Headless-browser checks of both dashboards, including WebSocket loss and recovery.
- A deployment verification on Linux under `systemd --user`.

**Not verified yet:**

- A real Raspberry Pi.
- Tailscale Serve end-to-end.
- A real Healthchecks.io alert.
- Codex and OpenCode interactive hooks or plugin.
- Sub-agents with a real runtime.
- Frame rate on a GPU browser.

Details are in [docs/PHASE_ACCEPTANCE.md](docs/PHASE_ACCEPTANCE.md) and
[docs/AGENT_INSTALLATION.md](docs/AGENT_INSTALLATION.md#3-what-actually-works-per-runtime).

## Documentation

| | |
|---|---|
| [ARCHITECTURE](docs/ARCHITECTURE.md) | components, source of truth, identity, events, backend ↔ UI |
| [AGENT_PROTOCOL](docs/AGENT_PROTOCOL.md) | registration, messaging, delivery states, tasks, generations, checkpoints |
| [AGENT_INSTALLATION](docs/AGENT_INSTALLATION.md) | connecting each runtime + real compatibility matrix |
| [COORDINATOR](docs/COORDINATOR.md) · [RECOVERY](docs/RECOVERY.md) | election, epochs, split-brain; timeouts, orphans, Git limits |
| [API](docs/API.md) | endpoints, auth, errors, cursors, WebSocket |
| [DEPLOYMENT](docs/DEPLOYMENT.md) | Raspberry Pi from scratch, Tailscale, backups, alerts, update/rollback, migration |
| [DEVELOPMENT](docs/DEVELOPMENT.md) · [TESTING](docs/TESTING.md) · [TROUBLESHOOTING](docs/TROUBLESHOOTING.md) | |
| [UPSTREAM](docs/UPSTREAM.md) · [DECISIONS](docs/DECISIONS.md) · [PHASE_ACCEPTANCE](docs/PHASE_ACCEPTANCE.md) | provenance, ADRs, phase reports |

## Licence

The 3D office code derived from oficina-virtual is MIT, and its notice is kept in
`agentlink/static/office/LICENSE-oficina-virtual.txt`. The AgentLink code has no licence file upstream (see
[UPSTREAM.md](docs/UPSTREAM.md#sources-and-reference-commits)); the repository owner should add one.
