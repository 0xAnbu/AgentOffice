# Phase acceptance reports

Format of INSTRUCTIONS §20.5. All results below were produced by commands run on 2026-11-21/22 on Linux x86_64
(Kali, Python 3.14.8, systemd 262); code verified at commit `183fec1`, documentation at `e995421`. Nothing here is
assumed: anything not executed is listed as pending. Raw outputs are in `docs/evidence/` (see its README).

Summary:

| Phase | Status |
|---|---|
| 1 — Audit, local environment, integration | **CONDITIONAL** |
| 2 — Minimal vertical communication | **PASSED** |
| 3 — Reliability and recovery | **PASSED** |
| 4 — Universal client and integrations | **CONDITIONAL** |
| 5 — 3D frontend | **CONDITIONAL** |
| 6 — Chat and realtime | **PASSED** |
| 7 — Raspberry Pi deployment | **CONDITIONAL** |
| 8 — Integral validation | **CONDITIONAL** |

The project is **not** declared finished (§20.7 requires every phase PASSED and a verified Raspberry Pi deployment).

```yaml
phase:
  id: 1
  name: "Audit, local environment and integration"
  status: CONDITIONAL
repository:
  commit: "183fec1"
implementation:
  completed:
    - "Both upstreams imported unmodified at pinned commits (49bfeb8): agentics eb4f771, oficina-virtual 9b838e8"
    - "Licences checked: oficina-virtual MIT (notice kept); agentics has no LICENSE file"
    - "AgentLink backend integrated; Three.js office served by Starlette at /; no Node server"
    - "SQLite WAL; reproducible venv; Makefile: install, dev, test, smoke, test-failures"
  pending:
    - "Repository owner to add an explicit licence to 0xAnbu/agentics (reuse currently relies on same-account ownership and the owner's instructions)"
verification:
  commands: ["make test", "make smoke", "scripts/visual_check.py"]
  passed: 120
  failed: 0
  skipped: 0
  evidence:
    - "Upstream agentics suite re-run at its pinned commit: 55 passed"
    - "oficina-virtual has no automated tests; its Node demo server was not run (Node not installed, not needed)"
    - "Headless Chromium: 3D scene initialised from API data, 0 JS errors (docs/evidence/visual/report.json)"
    - "Persistence across backend restart: test_backend_restart_recovers_state_and_reconciles, deployment/verify.sh"
regressions: {detected: 0, unresolved: 0}
limitations:
  - "agentics licence not explicit (see UPSTREAM.md)"
decision:
  advance: true
  next_phase: 2
  reason: "Everything works locally; the only open item is a licence file the owner must add"
---
phase:
  id: 2
  name: "Minimal vertical communication"
  status: PASSED
repository:
  commit: "183fec1"
implementation:
  completed:
    - "Sessions with own identity, persisted messages, cursors, delivery receipts, acknowledgements, reconnection"
    - "Dashboards show real events (WebSocket + snapshot)"
    - "Real runtime integration (Claude Code, Codex, OpenCode)"
  pending: []
verification:
  commands: ["make test", "make smoke", "RUN_REAL_RUNTIMES=1 scripts/e2e_runtimes.py"]
  passed: 120
  failed: 0
  skipped: 0
  evidence:
    - "test_vertical_flow_two_supervisors: A -> server -> supervisor B -> runtime B -> reply -> A, both directions"
    - "test_supervisor_restart_and_store_loss_lose_nothing, test_reconnect_recovers_pending_messages_without_duplicates"
    - "test_idempotent_retries (client_id), delivery states require evidence for processed"
    - "runtime-e2e-20261121T224306Z.json: real Claude Code <-> Codex round trip (communication between real models)"
regressions: {detected: 0, unresolved: 0}
limitations: []
decision: {advance: true, next_phase: 3, reason: "All criteria verified with real protocol clients"}
---
phase:
  id: 3
  name: "Reliability and recovery"
  status: PASSED
repository:
  commit: "183fec1"
implementation:
  completed:
    - "Projects and identities; task state machine; differentiated heartbeats (device/session/progress/lease)"
    - "Session-bound task leases with fencing generations; dependencies with cycle rejection"
    - "Coordinator election with epochs, offers, decline/propose, timeouts, succession; split-brain prevention"
    - "Transactional events; idempotent reconciler; orphan recovery; holder_required + safety net"
    - "Two external reviews: every applicable finding fixed with a test that fails before and passes after"
  pending: []
verification:
  commands: ["make test", "make test-failures"]
  passed: 120
  failed: 0
  skipped: 0
  evidence:
    - "I1 exclusive claim: test_eight_sessions_claim_concurrently_exactly_one_wins"
    - "I2 fencing: test_old_holder_cannot_complete_with_stale_epoch"
    - "I3 coordination: test_coordinator_death_triggers_single_successor_and_epoch_bump (3 concurrent accepts -> 1), test_old_coordinator_returns_and_is_rejected"
    - "I4 recovery: test_lease_expiry_without_renewal_orphans_then_recovers_to_open, test_active_task_cannot_lose_its_holder"
    - "I5 persistence: test_backend_restart_recovers_state_and_reconciles"
    - "I6 events: test_crash_between_state_change_and_event_commits_nothing, test_every_message_has_its_event"
    - "I7 dependencies: test_dependency_cycle_rejected_graph_unchanged"
    - "I8 idempotency: test_idempotent_retries, test_reconnect_recovers_pending_messages_without_duplicates"
    - "I9 authorisation: test_project_isolation, test_sessions_cannot_be_hijacked, test_removed_member_loses_sessions_tasks_and_coordinator"
    - "I10 progress: test_stalled_runtime_cannot_keep_lease_alive, test_device_heartbeat_does_not_renew_anything"
regressions: {detected: 0, unresolved: 0}
limitations:
  - "Guarantees rely on SQLite's single serialised writer; PostgreSQL / several instances are unsupported (prerequisites listed in ARCHITECTURE.md)"
decision: {advance: true, next_phase: 4, reason: "All invariants verified by automated tests"}
---
phase:
  id: 4
  name: "Universal client and integrations"
  status: CONDITIONAL
repository:
  commit: "183fec1"
implementation:
  completed:
    - "agentlink connect/status/doctor/supervise/hook/disconnect; reversible install; user service (systemd/launchd)"
    - "Adapters: Claude Code (hooks), Codex (hooks + opt-in headless), OpenCode (plugin + opt-in headless), generic CLI"
    - "Capability matrix with honest statuses (AGENT_INSTALLATION.md)"
    - "Headless turns: keep-alive heartbeats, bounded retries, timeout, only rendered messages count"
  pending:
    - "Codex interactive hooks: need the participant to trust them in Codex (never bypassed) — not exercised"
    - "OpenCode plugin observation — not exercised"
    - "Sub-agents with a real runtime (Claude: hook-level tests only; Codex/OpenCode: UNKNOWN)"
    - "Supervisor auto-resume of idle Claude/Codex/OpenCode sessions — not exercised"
verification:
  commands: ["make test", "RUN_REAL_RUNTIMES=1 scripts/e2e_runtimes.py --only claude,codex,codex-timeout,opencode,cross"]
  passed: 120
  failed: 0
  skipped: 0
  evidence:
    - "runtime-e2e-20261121T221923Z.json: Claude Code 2.1.330 8/8 (hooks, injection, reply, processed)"
    - "runtime-e2e-20261121T232513Z.json: Codex 0.170.0 4/4 and OpenCode 2.1.4 4/4 via the supervisor loop"
    - "runtime-e2e-20261122T024122Z.json: Codex 4/4 after the keep-alive/timeout changes"
    - "runtime-e2e-20261122T024142Z.json: real Codex timeout handled (exit 124, pending, no re-run) 4/4"
    - "runtime-e2e-20261121T224306Z.json: cross-provider 6/6"
    - "Failed/invalid runs kept: 20261121T221937Z (OpenCode half invalid), 20261121T223223Z (OpenCode hang)"
regressions: {detected: 0, unresolved: 0}
limitations:
  - "Capabilities marked IMPLEMENTED_NOT_VERIFIED / PARTIALLY_SUPPORTED / UNKNOWN in the matrix are not claimed as working"
  - "macOS/Windows clients not tested"
decision:
  advance: true
  next_phase: 5
  reason: "Every capability declared SUPPORTED_AND_TESTED has real evidence; the rest is declared as such"
---
phase:
  id: 5
  name: "3D frontend"
  status: CONDITIONAL
repository:
  commit: "183fec1"
implementation:
  completed:
    - "Sectioned building, configurable floors by specialization, rooms per owner, original characters unchanged"
    - "Runtime/owner/coordinator/sub-agent badges; elevator transit with cab; camera, floor buttons, keyboard"
    - "Event-driven animations through a visual queue; no simulation; Activity Center; hidden controls before login"
    - "Rendering cost: adaptive frame rate, GPU resources freed on rebuild, shared textures, DPR capped at 1.5, 2048² shadow map (no LOD)"
  pending:
    - "Frame rate on a real GPU browser (only measured with software rendering)"
    - "Human visual review on the team's screens"
verification:
  commands: ["python scripts/visual_check.py --admin-token ...", "python scripts/render_check.py"]
  passed: 2
  failed: 0
  skipped: 0
  evidence:
    - "docs/evidence/visual/: building, floor, agent close-up, anonymous view; report.json with 0 JS/HTTP errors"
    - "Scene built once from API data; heartbeats and a 300-event burst cause no rebuild (construcciones = 1)"
    - "Characters observed walking and inside the elevator; sub-agents and coordinator labelled"
    - "JS cost per frame ~0.3-5 ms; page round-trip 1-4 ms after the burst (rendering itself measured in SwiftShader only)"
    - "render_check: no GPU-resource growth across rebuilds/theme switches (main leaked ~330 geometries and ~110 textures per rebuild); idle building 8 fps instead of 30 (-69% CPU at equal quality, software GL); frame-rate tiers verified"
regressions: {detected: 0, unresolved: 0}
limitations:
  - "Floor labels can overlap the header/floor buttons at some zoom levels (cosmetic)"
decision: {advance: true, next_phase: 6, reason: "Works on API data with real-browser verification; GPU performance pending"}
---
phase:
  id: 6
  name: "Chat and realtime"
  status: PASSED
repository:
  commit: "183fec1"
implementation:
  completed:
    - "WS /ws/events with single-use tickets, cursor replay, resync; snapshot refresh on state events"
    - "/chat without 3D: projects, channels, agents, threads, search, pagination, filters, deliveries, tasks, leases, coordinator, recovery history"
    - "Scroll QoS: follow new messages only at the bottom; floating 'new messages' button otherwise"
  pending: []
verification:
  commands: ["make test", "make smoke", "python scripts/visual_check.py --admin-token ..."]
  passed: 120
  failed: 0
  skipped: 0
  evidence:
    - "Browser: WS dropped -> 'reconectando' -> 'en vivo'; 5 messages posted during the outage shown once; 0 duplicate cards"
    - "Browser: new messages appear without reload; agent filter shows only that agent's cards"
    - "Browser: /chat has no canvas; scrolled-up view kept, button counts new messages, click resumes following"
    - "test_ws_requires_valid_single_use_ticket, test_ws_replays_from_cursor_and_streams_live, test_events_cursor_and_resync, test_ws_filters_unauthorized_projects"
regressions: {detected: 0, unresolved: 0}
limitations: []
decision: {advance: true, next_phase: 7, reason: "Both dashboards reflect persisted state and live updates; no loss on WS outage"}
---
phase:
  id: 7
  name: "Raspberry Pi deployment"
  status: CONDITIONAL
repository:
  commit: "183fec1"
implementation:
  completed:
    - "agent-office.service (loopback, Restart=on-failure, hardened), healthcheck and backup timers"
    - "install.sh, update.sh (pre-update backup, automatic rollback), restore.sh (refuses if not stopped, auto rollback), backup.sh (SQLite online backup API)"
    - "Tailscale Serve, Healthchecks.io and migration procedures documented"
  pending:
    - "Run on a real Raspberry Pi (hardware not available)"
    - "Tailscale Serve HTTPS from participants' laptops; no access from outside the tailnet"
    - "Real Healthchecks.io check and alert e-mail"
verification:
  commands: ["bash deployment/verify.sh"]
  passed: 20
  failed: 0
  skipped: 0
  evidence:
    - "docs/evidence/deployment-verify-*.log: 20/20 on Linux x86_64 under systemd --user (NOT a Raspberry Pi)"
    - "Start, auth, loopback-only binding, persistence, restart after SIGKILL, backup under writes, restore, restore safety, heartbeat only when healthy, timers, update, rollback, broken-release rollback"
regressions: {detected: 0, unresolved: 0}
limitations:
  - "Not verified on Raspberry Pi hardware; Tailscale and Healthchecks.io not configured end to end"
decision: {advance: true, next_phase: 8, reason: "Scripts and service verified on Linux; hardware-dependent checks recorded as pending"}
---
phase:
  id: 8
  name: "Integral validation"
  status: CONDITIONAL
repository:
  commit: "183fec1"
implementation:
  completed:
    - "Integral E2E with five independent participants over real HTTP"
    - "Documentation (README + 12 docs), compatibility and limitation reports, installation and recovery procedures"
  pending:
    - "Phases 1, 4, 5 and 7 are CONDITIONAL (see above)"
    - "Production deployment on the Raspberry Pi"
verification:
  commands: ["make test", "make smoke", "bash deployment/verify.sh", "scripts/visual_check.py", "scripts/e2e_runtimes.py"]
  passed: 120
  failed: 0
  skipped: 0
  evidence:
    - "test_five_participants_full_lifecycle: election, tasks, claims, messaging, progress, agent failure, recovery, reassignment, coordinator failure, succession, completion, dashboard snapshot"
    - "Autonomous LLM communication verified separately with real runtimes (Claude Code <-> Codex)"
regressions: {detected: 0, unresolved: 0}
limitations:
  - "See phases 1, 4, 5, 7"
decision:
  advance: false
  next_phase: null
  reason: "Not every phase is PASSED and the production deployment is not verified on the target hardware"
```
