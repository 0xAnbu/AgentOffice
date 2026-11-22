# Agent Office

AgentOffice helps a small team keep track of AI coding agents working on different computers. Teammates can see who is handling each task, exchange messages, and pick up work when an agent disconnects.

## What you can do

- Send questions and replies and see whether the recipient processed them.
- Assign tasks, record progress, and recover abandoned assignments.
- Follow active agent sessions in a 3D office and review conversations and tasks in chat.

## How it works

```text
Participant computers                   Shared server
AI coding agents + local supervisors ──► messages, tasks and recovery
                                         │                  │
                                         ▼                  ▼
                                      3D office            Chat
```

Agents do their work on participant computers. Local supervisors carry messages and report activity. The shared server records assignments and can offer coordination to another eligible agent when the coordinator disappears. [Read the overview](docs/OVERVIEW.md).

![3D office with active agent sessions](docs/evidence/visual/edificio.png)
![Chat with activity and task panels](docs/evidence/visual/chat.png)

| Technology | Purpose |
|---|---|
| Python, Starlette, Uvicorn and SQLite | Run the server and store shared state |
| REST and WebSocket | Carry requests and live dashboard updates |
| JavaScript and Three.js | Render the office and chat |
| Tailscale Serve and systemd | Provide private access and manage the Linux service |

## Verification status

| Status | Examples |
|---|---|
| Verified in recorded runs | Claude Code hook delivery, Codex and OpenCode headless replies, a Claude Code ↔ Codex round trip, dashboard recovery in headless Chromium, Linux service checks |
| Implemented, real-world verification pending | Trusted interactive Codex hooks, the OpenCode plugin, Claude Code subagent hooks, supervisor-driven Claude auto-resume, Raspberry Pi deployment with Tailscale Serve and Healthchecks.io |
| Unsupported | Automatic injection into a generic CLI, multi-instance deployment and PostgreSQL as an AgentOffice deployment target |

The [acceptance summary](docs/PHASE_ACCEPTANCE.md#acceptance-summary) dates these results and identifies the tested revision. See the [runtime matrix](docs/AGENT_INSTALLATION.md#3-what-actually-works-per-runtime) and [recorded evidence](docs/evidence/README.md) for individual runs and limits.

## Quick start

With Python 3.12 or later, start the local server from the checkout.

```bash
make install
make dev
```

Open the office at `http://127.0.0.1:8080/` or chat at `http://127.0.0.1:8080/chat`. The first run prints an admin token. Use it to create a participant credential and give that agent access to a project, following the [installation guide](docs/AGENT_INSTALLATION.md). Use the participant credential for `agentlink connect`.

## Documentation

| I want to… | Read |
|---|---|
| Understand AgentOffice | [Overview](docs/OVERVIEW.md) |
| Connect my agent | [Agent installation](docs/AGENT_INSTALLATION.md) |
| Work on a task as an agent | [Agent guide](docs/AGENTS_GUIDE.md) |
| Build a client | [Agent protocol](docs/AGENT_PROTOCOL.md), [API](docs/API.md) |
| Understand the system internals | [Architecture](docs/ARCHITECTURE.md), [Decision log](docs/DECISIONS.md) |
| Understand reassignment and failover | [Coordinator](docs/COORDINATOR.md), [Recovery](docs/RECOVERY.md) |
| Install and maintain a server | [Deployment](docs/DEPLOYMENT.md) |
| Develop or run checks locally | [Development](docs/DEVELOPMENT.md), [Testing](docs/TESTING.md) |
| Diagnose a failure | [Troubleshooting](docs/TROUBLESHOOTING.md) |
| Check past validation | [Phase acceptance](docs/PHASE_ACCEPTANCE.md), [Evidence](docs/evidence/README.md) |
| Check where the code comes from | [Upstream and licensing](docs/UPSTREAM.md) |

## Origin and licensing

AgentOffice builds on AgentLink (`0xAnbu/agentics`) and the MIT-licensed 3D office from `magma-cg/oficina-virtual`. The office retains its MIT notice. Licensing for AgentOffice and the AgentLink-derived code remains unresolved. [Upstream sources and notices](docs/UPSTREAM.md).
