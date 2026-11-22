# Architecture

Agent Office coordinates AI agents (Claude Code, Codex, OpenCode, any CLI) that run on the machines of five
participants. One backend holds the state; local supervisors connect each runtime to it; a 3D building and a chat
view show what is really happening.

```text
 participant laptops (x5)                                      Raspberry Pi / any Linux box
 ┌──────────────────────────────────────┐                     ┌──────────────────────────────────────────────┐
 │ runtime (Claude Code / Codex /        │  hooks / plugin /   │ agent-office.service (ONE process)            │
 │ OpenCode / generic CLI)               │  headless exec      │  Starlette + uvicorn, 127.0.0.1:8080          │
 │        ▲            │                 │                     │  ├─ REST API  /api/*      (agents, humans)    │
 │        │ inject     │ hook events     │                     │  ├─ MCP       /mcp        (optional)          │
 │ agentlink supervisor (stdlib only)    │ HTTPS (Tailscale)   │  ├─ WS        /ws/events  (dashboards)        │
 │  local SQLite store: inbox, cursors,  │◄───────────────────►│  ├─ UI        /  (3D)   /chat                 │
 │  sessions, held tasks, activity       │  long-poll + REST   │  ├─ reconciler loop (every 5 s)               │
 └──────────────────────────────────────┘                     │  └─ SQLite (WAL) = single source of truth     │
                                                              └──────────────────────────────────────────────┘
                                                                 timers (separate processes): healthcheck → Healthchecks.io,
                                                                 daily backup (SQLite online backup API)
```

## Components and responsibilities

| Component | Where | Responsibility | Never does |
|---|---|---|---|
| `agentlink/` backend | server | Identity, projects, channels, messages, tasks + leases, coordinator election, sessions, deliveries, durable event log, recovery reconciler, API/WS/UI | Execute code, talk to an LLM |
| Reconciler (`recovery.reconcile`) | inside the backend | Expire sessions/leases, orphan tasks, stall detection, coordinator loss/offers, overdue deliveries. Idempotent. | Hold state outside SQLite |
| Supervisor (`agentlink supervise`) | each laptop | Long-polls the inbox, persists before acknowledging, reports delivery states, beats device + session heartbeats, renews task leases **only with real progress**, writes local "authority lost" notices | Decide coordination |
| Hooks / plugin (`agentlink hook ...`) | each runtime | Register sessions/sub-agents, inject pending messages at safe points, report progress, block `Stop` while urgent work is pending | Fail the runtime (they fail open) |
| Adapters (`agentlink/adapters.py`) | each laptop | Install/uninstall the runtime integration reversibly; declare an honest capability matrix | Bypass a runtime's trust/approval prompts |
| 3D office (`static/office/`) | browser | Visualise sessions as the original oficina-virtual characters on floors, animate real events | Write coordination state; simulate in production |
| Chat (`static/chat/`) | browser | Conversations, threads, search, deliveries, tasks, leases, coordinator, recovery history | Duplicate business logic |
| `deployment/` | server | systemd unit, install/update/rollback, backup/restore, health check timer | Run a second coordination server |

## Single source of truth

Everything that matters is a row in one SQLite database (WAL, `synchronous=NORMAL`, `foreign_keys=ON`):
`projects`, `project_members`, `agents`, `tokens`, `channels`, `messages`, `tasks`, `task_deps`, `checkpoints`,
`leases`, `sessions`, `devices`, `coordinators`, `deliveries`, `events`, `kv`, `audit`. All writes go through
`db.transaction()` (`BEGIN IMMEDIATE` + a process lock), so concurrent claims are serialised by the database and
every authority decision is a conditional `UPDATE` (fencing). The local supervisor store is a cache of what was
received; losing it loses nothing (the server re-delivers from cursors and delivery receipts — tested).

All timeouts are judged on the **server clock** (`security.now_ms()`); clients never decide that someone else
expired.

## Projects and identity

- **Project** — the isolation unit: channels, tasks, coordinator, sessions and events belong to one project.
  A member of `alpha` cannot read `beta` (tested); a `monitor` token can be scoped to projects.
- **Agent** — a credential (name + role `admin|member|monitor` + bearer tokens hashed with SHA-256). Agent Office
  adds `owner` (the human participant, e.g. `dev01`), `specialization` (decides the floor) and `coord_priority`.
- **Session** — one running runtime instance of an agent (`ses_…`), with `runtime`, `model`, `device`,
  `parent_id` (sub-agents), state and heartbeats. An agent can have many sessions; authority (task leases,
  coordinator role) is held by a *session*, not by the agent, so a dead session loses it even if the agent has
  others alive.
- **Device** — a participant's machine; its heartbeat proves the supervisor is alive, and **never** renews
  sessions, leases or the coordinator role.

## Coordination vs. execution

The backend coordinates; it never executes. Agents do the work in their own repositories and report through the
protocol (messages, task updates, checkpoints). Because the server cannot see or undo local Git operations, the
guarantees stop at the protocol boundary: a stale holder's *writes to Agent Office* are refused (fencing), and the
supervisor tells the runtime it lost authority, but a `git push` made outside Agent Office cannot be blocked
(`agentlink check-lease` exists for pre-push hooks). See [RECOVERY.md](RECOVERY.md#git).

## Events

Every state change emits an event **in the same transaction** (`events.emit(tx, …)`), so there is never a state
change without its event or vice versa (tested by injecting a crash between both). Events have a monotonic id; the
dashboards read them with a cursor over WebSocket (`/ws/events`) or long-poll (`/api/events`) and receive
`resync` when their cursor falls behind retention. 33 event types: `message.created`, `task.*` (created, claimed,
updated, renewed, released, orphaned, recovered, blocked, unblocked, checkpoint, stale_write, wait_timeout),
`lease.*`, `session.*` (registered, ended, expired, stalled, resumed), `agent.online/offline`, `coordinator.*`
(offered, accepted, declined, offer_expired, lost, unavailable, released), `delivery.overdue`, `project.created`,
`kv.set`.

## Backend ↔ frontend

The UI is served by the same process (`/` → 3D office, `/chat`), uses the same token-authenticated API as agents
and holds no state of its own:

1. `GET /api/office/snapshot` — building layout, sessions (with floor), tasks, leases, coordinators, devices and the
   current event cursor.
2. `POST /api/ws-ticket` → single-use, 30-second ticket (the bearer token never goes in a URL) →
   `WS /ws/events?ticket=…&after=<cursor>` replays missed events then streams live ones.
3. On reconnect: resume from the last cursor; on `resync`: reload the snapshot.

The 3D scene is rebuilt only when the *layout* changes (floors, rooms, desks); heartbeats only update labels and
poses. Animations are a visual queue derived from events (message → note flies, assignment → folder carried, via
the elevator across floors), independent of and never blocking the real delivery queue. There is **no simulation
fallback**: without a token or backend the page shows the login / a disconnected state.

## Building model

Floors come from `AGENTLINK_OFFICE_CONFIG` (JSON) or the built-in default (top to bottom): Coordinación,
Seguridad / QA, DevOps / Infraestructura, Backend / APIs (fallback floor), Frontend / UX. A session lands on the
first floor whose `match` list contains its specialization (session specialization, else agent specialization,
else the fallback). On each floor, each owner gets a room; each session gets a desk and a character.

## Persistence and operations

- Schema migrations are versioned (`schema_version`), forward-only, applied at start; the backend refuses to run on
  a schema newer than its code (safe rollback).
- Backups use the SQLite online backup API (consistent under WAL, verified with `integrity_check`).
- Logs: journald (`journalctl -u agent-office`). Health: `/api/health` (process) and `/api/health?deep=1`
  (SQLite read/write + reconciler heartbeat; 503 otherwise).
- Inherited from AgentLink and **not** part of the supported Agent Office deployment: the Postgres/serverless mode
  (not verified with schema v2) and the Vercel deployment (removed). The correctness of claims, fencing, elections,
  dependency checks, path leases, delivery states and cursors relies on SQLite's single serialised writer
  (`BEGIN IMMEDIATE`). Two reviews listed what would have to change before supporting PostgreSQL or several
  instances: conditional `UPDATE`s re-checking epoch/version, a consistent lock order between session and task
  updates, cursors that tolerate out-of-order commits, and serialised path-lease and dependency checks.
