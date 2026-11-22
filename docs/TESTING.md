# Testing

| Command | What | Needs |
|---|---|---|
| `make test` | all automated tests (120 at the time of writing) | nothing |
| `make test-failures` | recovery and client failure scenarios (`tests/test_recovery.py`, `tests/test_client.py`) | nothing |
| `make smoke` | a real uvicorn server driven through the CLI, curl and a WebSocket client | nothing |
| `bash deployment/verify.sh` | install/service/backup/restore/healthcheck/update/rollback under `systemd --user` | Linux + systemd |
| `make visual TOKEN=…` | headless Chromium on `/` and `/chat` (errors, scene state, screenshots) | Chromium, a running server |
| `make test-runtimes [ONLY=claude,codex,opencode,cross]` | real Claude Code / Codex / OpenCode | the CLIs installed and logged in; **spends quota** |

## Layers

- **Unit / service** — validation, migrations (including refusing a newer schema), coordination rules, dependency
  cycles, delivery state monotonicity (`tests/test_*.py` inherited from AgentLink plus Agent Office tests).
- **Integration (in-process HTTP)** — Starlette `TestClient` against a temporary SQLite database with a controllable
  server clock (`security.advance_clock`); the reconciler is called explicitly so time-based behaviour is
  deterministic (`tests/office_helpers.py`: projects alpha/beta, five owners, several agents).
- **Integration (real HTTP)** — uvicorn on a free localhost port, real local supervisors with their own
  `AGENTLINK_HOME` and store (`tests/test_client.py`, `tests/test_e2e_office.py`). Hook payloads are the documented
  runtime hook inputs, produced deterministically; a fake `codex` binary tests headless delivery.
- **Concurrency** — 8 sessions claim one task in parallel threads (exactly one wins); 3 concurrent coordinator
  acceptances (exactly one wins).
- **Recovery** — every scenario of INSTRUCTIONS §15 below.
- **Synthetic agents** — `scripts/synthetic_office.py` (5 owners, 8 agents, sub-agents) drives the *real* API for
  dashboards and manual checks. Testing only; refuses `AGENTLINK_MODE=production`; sessions are labelled
  `synthetic`.
- **3D rendering** — `scripts/render_check.py` (`make render`, Linux, needs Chromium): starts its own temporary
  server, then (1) CPU% and memory of every Chromium process with the adaptive loop vs `?ahorro=0` at the same
  quality, and with default settings on an emulated 2x screen; (2) sessions appear one by one (a rebuild each) and the
  page must hold no more geometries/textures than a fresh page on the same layout, and six day/night switches must not
  add any; (3) the frame-rate tiers (camera drag 60, event 30, ambient 8, nothing for 60 s 2). `--repo <checkout>`
  runs the same procedure against another commit. WebGL is software (SwiftShader) here: it draws ~1 frame/s at
  1600×1000, below any target, so the loop comparison uses a tiny window where the requested rate is reachable.
- **Real runtimes** — `scripts/e2e_runtimes.py`, opt-in (`RUN_REAL_RUNTIMES=1`), writes
  `docs/evidence/runtime-e2e-<utc>.json` with every step and the runtime versions. OpenCode should be run with a
  model the account can use (`AGENTLINK_OPENCODE_MODEL`, e.g. `opencode-go/deepseek-v4.1-flash`).

## INSTRUCTIONS §15 scenarios → tests

| # | Scenario | Test / evidence |
|---|---|---|
| 1 | Concurrent claim | `test_eight_sessions_claim_concurrently_exactly_one_wins` |
| 2 | Holder dies | `test_dead_holder_task_is_orphaned_recorded_and_reclaimable`, `test_lease_expiry_without_renewal_orphans_then_recovers_to_open` |
| 3 | Old agent reappears | `test_old_holder_cannot_complete_with_stale_epoch`, `test_stale_supervisor_report_is_rejected_cleanly` |
| 4 | Coordinator disappears | `test_coordinator_death_triggers_single_successor_and_epoch_bump`, `test_stalled_coordinator_with_live_session_loses_authority` |
| 5 | Split-brain | `test_old_coordinator_returns_and_is_rejected` (+ concurrent acceptance in test 4) |
| 6 | All agents disconnected | `test_no_candidates_preserves_state_and_alerts_humans` |
| 7 | Dependency cycles | `test_dependency_cycle_rejected_graph_unchanged` |
| 8 | Token/quota exhaustion | `test_stalled_runtime_cannot_keep_lease_alive`, `test_supervisor_heartbeats_and_renewal_policy` |
| 9 | Backend restart | `test_backend_restart_recovers_state_and_reconciles`; real restart + SIGKILL in `deployment/verify.sh` |
| 10 | Agent reconnection | `test_reconnect_recovers_pending_messages_without_duplicates`, `test_supervisor_restart_and_store_loss_lose_nothing` |
| 11 | Visualisation | `test_snapshot_places_sessions_on_specialization_floors`, `test_floor_mapping_fallback`, `scripts/visual_check.py` (real Chromium, `docs/evidence/visual/`) |
| 12 | Chat | `test_message_search_pagination_and_project_filter`, `test_ws_*`, `visual_check.py` (`/chat`) |
| 13 | External supervision | `test_deep_health`; `deployment/verify.sh` (heartbeat only when healthy, none when down). A real Healthchecks.io check is **not** configured (manual, see DEPLOYMENT.md) |
| 14 | Cross-provider autonomous communication | `runtime-e2e-20261121T224306Z.json` (Claude Code ↔ Codex, real) |
| 15 | Received but not processed | `test_received_but_unprocessed_message_stays_pending_and_alerts` |
| 16 | Duplicate work and Git | `test_old_holder_cannot_complete_with_stale_epoch` + supervisor AUTHORITY LOST notice; Git limits in RECOVERY.md (no Git operation is blocked) |
| 17 | Transactional consistency | `test_crash_between_state_change_and_event_commits_nothing`, `test_every_message_has_its_event` |
| 18 | Project isolation | `test_project_isolation`, `test_ws_filters_unauthorized_projects`, `test_monitor_scoped_to_project` |
| — | External review 2026-11-21 (findings 1–8, 12–16, 20, 22) | `tests/test_review_fixes.py`: 15 behavioural tests, each verified to fail on `fbe3e7b` (before the fixes) and pass after. Findings 9–11, 17, 19 only apply to Postgres/multi-instance (unsupported); 21 (inbox starts from the oldest mention) is the intended contract |
| — | Second external review 2026-11-22 (findings 5, 6, 9, 10, 11) | `tests/test_review_fixes.py` (6 more tests, verified to fail on `f9b3922`); restore safety in `deployment/verify.sh`; anonymous view and `/chat` scroll behaviour in `visual_check.py`. Findings 1–4, 7, 8 and the server half of 10 are PostgreSQL/multi-instance only |
| — | Rendering audit 2026-11-22 (continuous redraw, duplicated textures, resources not freed on rebuild, DPR, shadow map) | `scripts/render_check.py`: run on `main` (392f361) it reports 1839 geometries / 636 textures leaked after 6 rebuilds and textures added by theme switches; on this branch 1 / 1 and 0 |
| — | Phase 8 integral scenario | `test_five_participants_full_lifecycle` (5 independent clients over real HTTP) |

## Credentials

The automated suite needs none. `make test-runtimes` uses the CLIs' own logins (`claude`, `codex`, `opencode`) on the
machine; nothing is stored by the tests. Evidence files contain no tokens.

## Evidence

`docs/evidence/` — see its README for what each file proves (including the failed and invalid runs, kept on purpose).
