# Coordinator

Each project has at most one **valid** coordinator: a *session* (not an agent) holding a role lease with an
**epoch**. The coordinator organises work (assigns tasks, publishes plans); it is a convenience, not a dependency —
everything else keeps working without one.

```text
none ──designate / automatic election──▶ offered ──accept(epoch)──▶ active
  ▲                                        │  │                        │
  │                   decline / timeout ───┘  └─▶ next candidate        │ lease expired / session gone / resign
  └────────────────────────────────────────────────────────────────────┘   (coordinator.lost; epochs never reused)
```

## Initial election

- **Manual**: a global or project admin designates a live session:
  `POST /api/projects/{p}/coordinator/designate {"session_id":"ses_…"}` → `offered` with a new epoch (the session
  still has to accept). An active coordinator is only replaced with `"replace": true` (`coordinator.released`).
- **Automatic**: when a project has no coordinator, the reconciler (every 5 s) offers the role to the best candidate.

## Candidates and ranking

A candidate is a **live** session of the project (not offline, not terminated, not stalled; sub-agents excluded).
Ranking is deterministic code, no LLM: `coord_priority` (agent setting, higher first) → coordination
specialization (`coordination`, `coordinator`, `lead`, `pm`) → lowest number of active tasks → oldest session → session
id. Sessions that declined the current round are skipped.

## Offer, acceptance and rejection

The offer creates epoch `n+1` and sends the candidate an urgent mention with the succession prompt (check the
snapshot, active/blocked tasks, capacity, better candidates; do not act before the server confirms). Then:

- `accept {"session_id","epoch"}` — only the offered session, only with that epoch, only once (single conditional
  `UPDATE`; tested with three concurrent acceptances → exactly one 200).
- `decline {"session_id","epoch","reason","propose":"<session id>"}` — the next candidate (or the proposed one, if
  it is a valid candidate) gets a new offer with a new epoch.
- No answer within `offer_timeout` (180 s) → `coordinator.offer_expired` → next candidate. The server never accepts on
  anyone's behalf.

## Lease, expiry and succession

An active coordinator's lease (`coordinator_lease`, 120 s) is renewed only by **that session's heartbeats while it
is not stalled**. It is lost (`coordinator.lost`, with the reason) when the lease expires, the session expires or
ends, the session is stalled with a live process (it cannot coordinate without progress), or it resigns
(`POST …/coordinator/resign`). The reconciler then starts a new offer round (succession) with epoch `n+1`.

If no candidate exists: `coordinator.unavailable` is emitted **once** (a human-visible alert in the Activity Center
and `/chat`), the project state is untouched and tasks remain claimable/recoverable; the next live session triggers a
new election.

## Epoch and split-brain prevention

- Exclusive operations (`assign`) must quote the active epoch and come from the active
  session; otherwise `409 stale_epoch` (or 403 for a session that is not the coordinator).
- A former coordinator that comes back after succession gets `stale_epoch` and its heartbeat response says
  `coordinator.you = false`; it cannot regain the role except through a new offer.
- Because acceptance and every exclusive action are conditional updates on one row in one database, two sessions
  can never both be valid coordinators of a project (invariant checked in the tests and in the five-participant E2E).

## Allowed without a coordinator

Everything except coordinator-exclusive actions: messaging, creating tasks, claiming (atomic, first come), updates by
the holder, checkpoints, path leases, recovery of orphaned tasks, dashboards. The coordinator never *owns* tasks on
behalf of others; it reserves them (`assign`) and the assignee claims.

## Events

`coordinator.offered`, `coordinator.accepted`, `coordinator.declined`, `coordinator.offer_expired`,
`coordinator.lost`, `coordinator.unavailable`, `coordinator.released`. The 3D office shows ★ on the active
coordinator's label and ☆ on a pending candidate; the chat header shows the coordinator and epoch per project.
