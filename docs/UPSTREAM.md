# Upstream

## Sources and reference commits

| Repository | Commit (pinned) | Date | Licence | Used for |
|---|---|---|---|---|
| [0xAnbu/agentics](https://github.com/0xAnbu/agentics) (AgentLink) | `eb4f77152d230d0772d6948fce073a00b9624149` | 2026-11-21 | **No licence file found** (see below) | backend, CLI, MCP, monitor UI, admin tool, tests |
| [magma-cg/oficina-virtual](https://github.com/magma-cg/oficina-virtual) | `9b838e87050a80438fd79d0cdb43cd2074f2150f` | 2026-11-17 | MIT © 2026 Magma Consulting Group S.A.S. (verified `LICENSE`) | 3D office: scene, camera, characters, poses, animations, textures, post-processing |

The first commit of this repository (`49bfeb8`) imports both trees unmodified at those commits, as the traceability
baseline. Their READMEs are kept in `upstream/`; the AgentLink documents superseded by Agent Office docs are in
`upstream/agentlink-docs/`.

**Licences.** oficina-virtual is MIT: its notice is kept verbatim in
`agentlink/static/office/LICENSE-oficina-virtual.txt`, and files derived from it say so in their header. agentics has
no `LICENSE` file at the pinned commit; it belongs to the same GitHub account as this repository. Until its owner adds
a licence, its reuse here relies on that ownership — **the owner should add an explicit licence to both repositories**
(open item).

## Reused components and what changed

### From AgentLink (`agentlink/`)

| Component | Status |
|---|---|
| Starlette app, auth (hashed bearer tokens, roles), rate limits, validation, audit | kept; CSP adjusted for the 3D UI (`font-src 'self'`, `img-src blob:`) |
| `db.py` (SQLite/Postgres SQL subset, transactions) | kept; schema v2 migration (projects, sessions, devices, task deps, checkpoints, coordinators, events, deliveries); refuses newer schemas |
| messages, channels, inbox, long-poll, KV, presence, archive, GitHub webhook, MCP | kept; messages emit events in-transaction, priority, search/author/before/project filters |
| tasks + path leases (`coordination.py`) | rewritten: session-bound task leases with generations (fencing), state machine, dependencies + cycle detection, checkpoints, renewal only with progress |
| CLI (`cli.py`) and `./admin` | kept; new client commands in `clientcli.py` (`connect`, `supervise`, `hook`, `claim`, `checkpoint`, `check-lease`, …) |
| monitor UI (`static/`) | moved to `/chat` and rewritten on the snapshot + events API (threads, search, pagination, deliveries, tasks, leases, coordinator, recovery) |
| Vercel deployment (`api/`, `vercel.json`, `deploy_url.py`), Termux scripts, old CI | removed (single-instance SQLite deployment instead); CI re-added for the new layout |
| New | `projects.py`, `sessions.py`, `coordinator.py`, `recovery.py`, `delivery.py`, `events.py`, `office.py`, `ws.py`, `local.py`, `supervisor.py`, `hooks.py`, `adapters.py`, `deployment/` |

### From oficina-virtual (`agentlink/static/office/`)

| File | Status |
|---|---|
| `personas.js` (procedural 3D characters, poses), `texturas.js`, `post.js`, `vendor/` (three.js r171, MIT, OrbitControls) | **unchanged** |
| `mundo.js` | parameterised: floor height offset (`y`), city/sign only on the ground floor |
| `plano3d.js` | `distribuir(…, minInterior)` so stacked floors share one footprint |
| `oficina.js` | render loop, camera, characters, poses and errand animations kept; data source replaced (Agent Office API + events, **no simulation**), several floors, elevator transit, labels with runtime/owner/coordinator/sub-agent, Activity Center hooks |
| `index.html`, `oficina.css` | Agent Office header, floor buttons, Activity Center panel, token login |
| `simulacion.js`, `server.mjs` (Node demo server, Claude Code session scanner), GitHub Pages demo | removed — Agent Office data comes only from the backend |
| `marca/` logos | replaced with neutral Agent Office marks (no Magma trademarks); fonts (Archivo, Space Grotesk; `marca/OFL.txt`) kept |
| New | `datos.js` (API/WS client, snapshot → building), `actividad.js` (Activity Center) |

## Dependencies

Runtime (`requirements.txt`, all pure-Python wheels): starlette, uvicorn, pg8000 (legacy Postgres path),
**websockets** (new: uvicorn's WebSocket protocol for `/ws/events`). Dev: pytest, httpx, ruff, mypy. Browser: three.js
vendored from oficina-virtual. The client side (supervisor, hooks, CLI) uses only the Python standard library.

## Original test results (baseline)

| Upstream | Command | Result |
|---|---|---|
| agentics @ `eb4f771` | `pytest` in its own checkout and venv (re-run 2026-11-21) | **55 passed** |
| oficina-virtual @ `9b838e8` | — | no automated tests in the repository (`package.json` has only `start`/`estado`); Node is not installed here, so its demo server was not run |

All of AgentLink's original tests still run in this repository (some adapted to the intended contract changes listed
below); total suite now 99 passed.

## Known regressions / intended contract changes

1. `GET /` serves the 3D office; the AgentLink monitor is at `/chat` (`tests/test_github_archive_app.py` updated).
2. Tasks created with an `assignee` stay `open` (reserved) until claimed.
3. `reply` mentions the original author.
4. Monitors can be scoped to projects; channels belong to a project (existing ones migrated to `default`).
5. Postgres/serverless mode: code kept but **not verified with schema v2**; not supported for Agent Office.
6. Vercel one-click deployment removed.

No other regression is known.
