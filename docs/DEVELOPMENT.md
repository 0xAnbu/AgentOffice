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
`?dpr=<n>` (automatic: the screen's ratio capped at 1.5), `?sombras=4096` (default 2048²), `?ahorro=0` (fixed 30 fps
instead of the adaptive rate). In the console, `oficina3d.resumen()` returns the scene state (floors, sessions, who is
walking/in the elevator, builds) and `oficinaMedida` the frame figures (`fps` = rate asked for now, `real`, `cuadros`,
`llamadas`/`triangulos` of the whole frame, `liberado` = what the last rebuild freed).

### Rendering cost

- **Adaptive frame rate.** The loop asks for 60 fps while the
  camera moves, 30 while someone walks / rides the elevator / something flies or right after an event, 15 while
  someone works at a desk, 8 with only ambient motion (traffic, screens) and 2 after 60 s with nothing at all; 0 in a
  hidden tab. Any input, event, changed snapshot, theme change or resize wakes it on the next display frame. The world
  advances with real elapsed time (people in steps of ≤ 0.1 s), so traffic keeps its speed at low rates. The automatic
  quality downgrade only judges seconds that asked for ≥ 15 fps.
- **GPU resources have an owner** (`static/office/recursos.js`). A rebuild (a desk appears/disappears) frees the
  previous building's geometries, materials, instance buffers and textures; shared resources (floor textures, cached
  geometries, contact shadow, halo, sky and environment per theme) are created once and never freed.
- `make render` (`scripts/render_check.py`) measures these behaviours. See [Testing](TESTING.md) for procedures and [Evidence](evidence/README.md) for past measurements.

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
