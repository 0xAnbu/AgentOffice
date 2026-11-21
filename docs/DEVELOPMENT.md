# Development

Everything runs locally with no Raspberry Pi, no Tailscale, no external service and no LLM.

## Install and run

```bash
git clone git@github.com:0xAnbu/AgentOffice.git && cd AgentOffice
make install        # .venv + runtime and dev dependencies (Python ≥ 3.12)
make dev            # http://127.0.0.1:8080 with auto-reload; first run creates .env and prints the admin token
```

- `/` — 3D office (paste a token: admin, member or monitor). `/chat` — chat and project status.
- The first run writes `.env` (mode 0600) with `AGENTLINK_ADMIN_TOKEN` and `AGENTLINK_DB_PATH=data/agentlink.db`.
- SQLite: `AGENTLINK_DB_PATH` (WAL is enabled automatically; the directory is created). Use another path for a
  scratch database: `AGENTLINK_DB_PATH=/tmp/x.db make dev`.
- Mode: `AGENTLINK_MODE=development|testing|production` (a label shown by `/api/health?deep=1`; the synthetic
  activity script refuses to run against `production`). There is no code path that simulates data.
- Timings: every `AGENTLINK_*` recovery setting can be shortened for manual experiments (e.g.
  `AGENTLINK_SESSION_TTL=20 AGENTLINK_RECONCILE_INTERVAL=1 make dev`).
- Building layout: `AGENTLINK_OFFICE_CONFIG=office.json` (same shape as `GET /api/office/building`).

## Frontend

No build step: plain ES modules served by the backend (`agentlink/static/office/`, `agentlink/static/chat/`), three.js
vendored. Edit and reload. Useful URL parameters: `?tema=dia|noche`, `?planta=<floor key>`, `?calidad=alta|media|baja`,
`?dpr=1`. In the console, `oficina3d.resumen()` returns the scene state (floors, sessions, who is walking/in the
elevator, builds).

To see a busy office without real agents (testing only):

```bash
make demo ADMIN=<admin token>          # 8 synthetic agents of 5 owners + sub-agents for 10 min; prints a monitor token
make visual TOKEN=<monitor token>      # headless Chromium: JS errors, scene summary, screenshots in docs/evidence/visual
```

## Connecting a local agent to the dev server

```bash
agentlink connect --server http://127.0.0.1:8080 --token <agent token> --project default --runtime claude-code --scope project --dir .
```

## Logs and debugging

- Backend logs go to the terminal running `make dev` (production: journald).
- `GET /api/health?deep=1` — database and reconciler status. `POST /api/admin/reconcile` runs a reconciliation now and
  returns its counters.
- `GET /api/events?after=0&limit=100` (admin token) — the authoritative history of what happened.
- Client side: `agentlink status`, `agentlink doctor`, `~/.agentlink/state.db` (the supervisor's local store;
  `sqlite3 ~/.agentlink/state.db 'select * from inbox order by message_id desc limit 5'`).
- Browser: the Activity Center connection pill shows *en vivo / reconectando / desconectado*.

## Reset local data

```bash
rm -f data/agentlink.db data/agentlink.db-wal data/agentlink.db-shm   # server state (keeps .env/admin token)
agentlink disconnect --purge                                          # client config, store and installed hooks
```

## Quality gates

```bash
make test           # whole automated suite (no LLM, no network)
make smoke          # real server + CLI + WebSocket end to end
make test-failures  # only the failure/recovery scenarios
make lint typecheck # ruff + mypy (CI runs the same: make check)
```
