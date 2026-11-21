# Recovery

Nothing in Agent Office waits forever on a participant. Authority (task leases, path leases, the coordinator role) is
always a lease held by a session and judged on the server clock; the reconciler takes back whatever is not renewed.

## Timeouts (defaults, `AGENTLINK_*` environment variables)

| Setting | Default | Meaning |
|---|---|---|
| `SESSION_TTL` | 90 s | no session heartbeat → session `offline`, its authority released (`session.expired`) |
| `STALL_AFTER` | 600 s | `executing` without progress → `stalled` (`session.stalled`) |
| `TASK_LEASE_DEFAULT` / `TASK_LEASE_MAX` | 300 s / 3600 s | task lease TTL on claim / renewal cap |
| `PROGRESS_WINDOW` | 900 s | a lease is only renewed if real progress was reported this recently (`no_progress`) |
| `COORDINATOR_LEASE` | 120 s | renewed by the coordinator session's (non-stalled) heartbeats |
| `OFFER_TIMEOUT` | 180 s | a coordinator offer not accepted in time moves to the next candidate |
| `ORPHAN_HOLD` | 30 s | an orphaned task returns to `open` after this (it is claimable meanwhile) |
| `WAIT_TIMEOUT` | 3600 s | a `blocked` task raises `task.wait_timeout` once |
| `DELIVERY_OVERDUE` | 900 s | an urgent mention not acknowledged → `delivery.overdue` |
| `RECONCILE_INTERVAL` | 5 s | reconciler period (0 disables the loop; tests drive it explicitly) |

## Heartbeats

| Signal | Sent by | Renews | Never renews |
|---|---|---|---|
| Device | supervisor, every loop | the device row (supervisor alive) | sessions, leases, coordinator |
| Session | hooks + supervisor for live local sessions | the session | task leases (without progress) |
| Progress (`progress:true`) | hooks after real tool activity | session progress, held tasks' `last_progress_at` | — |
| Task renewal | supervisor (`renew`), only if progress is recent | the task lease | — |

So a runtime that is alive but stuck (quota exhausted, context full, waiting on a prompt) keeps its session for a
while but **loses its tasks**: no progress → no renewal → lease expiry → orphan (tested: "stalled runtime cannot
keep a lease alive").

## Session states

`registered → idle | executing | waiting` (reported) and derived by the server: `stalled` (executing without
progress), `offline` (heartbeat older than TTL), `terminated` (ended). Precedence: terminated > offline > stalled >
reported.

## Task states and transitions

```text
open ──claim──▶ claimed ──▶ in_progress ⇄ blocked / review ──▶ done | failed | cancelled
  ▲                 │            │
  │      holder session dies / lease expires (reconciler)
  │                 ▼            ▼
  └──── orphan_hold ── orphaned (claimable meanwhile; epoch kept for fencing)
```

Holder-only transitions require `session_id` + current `lease_epoch`. `done/failed/cancelled` can be reopened.

## Reclaiming abandoned tasks

1. The reconciler finds tasks whose holder session is gone or whose lease expired.
2. In one transaction: status `orphaned`, holder cleared, path leases of that session released, a system message in
   the channel and a `task.orphaned` event with `previous_holder`, `epoch`, `reason` and the age of the last
   checkpoint.
3. Anyone can claim it right away; the claim increments the generation (`lease_epoch`). After `ORPHAN_HOLD` it is
   simply `open` again (`task.recovered`).
4. The new holder reads the checkpoints (`GET /api/tasks/{id}/checkpoints`) and continues from there.

## Split-brain

- Tasks: every holder write carries the generation; a write from an older generation is refused (`409 stale_lease`)
  and recorded (`task.stale_write`). Two sessions can never both be authorised for the same task and generation.
- Coordinator: see [COORDINATOR.md](COORDINATOR.md#epoch-and-split-brain-prevention).
- Supervisor side: when renewal fails with `stale_lease`/`no_progress`/403/404, the supervisor marks the task lost and
  injects an urgent local **AUTHORITY LOST** notice (negative local id, never sent to the server).

## Reconciliation

`recovery.reconcile()` runs every `RECONCILE_INTERVAL` inside the backend (and on `POST /api/admin/reconcile`). It is
idempotent and each step is its own transaction: (1–2) expire sessions and detect stalls, releasing their authority →
(3) expire task leases → orphan, and return orphans to `open` after the hold → (4) block open tasks whose dependency
failed or was cancelled (and unblock them if that changes) → (5) `task.wait_timeout` for long-blocked tasks, once →
(6) expire path leases → (7) coordinator loss, offers, offer timeouts, vacancy alert → (8) overdue urgent deliveries. The deep health check fails if the loop has not run recently, so a dead
reconciler stops the external heartbeat.

After a **backend restart** nothing is lost: all state is in SQLite; the first reconciliation applies whatever expired
while it was down (tested). After a **supervisor restart or loss of its local store**, the supervisor re-reads its
inbox from the server cursors and pending delivery receipts; no message is lost or duplicated in the store (tested).

## Checkpoints

The only durable hand-over between generations; see [AGENT_PROTOCOL.md](AGENT_PROTOCOL.md#5-checkpoints).
Without a checkpoint the next holder only has the channel history and the repository.

## Duplicate work

At-least-once delivery and lease expiry mean two agents can, in a bad interleaving, both work on the same task for
a while (the old holder before it learns it lost the lease). Agent Office guarantees that only the current
generation can change the task in Agent Office, and makes the conflict observable (`task.stale_write`, the AUTHORITY
LOST notice, the Activity Center). It does not merge or discard anyone's code.

## Git

Agent Office cannot see or block Git operations made outside it. A former holder can still commit and push to its
branch; nothing deletes local work. Mitigations: use one branch per task generation, call
`agentlink check-lease <task>` in a pre-push hook (exit 3 = authority lost), and record branch/commit in
checkpoints so the next holder can decide what to keep. Do not read the fencing tests as a guarantee about Git.

## LLM execution limits

- "Injected" means the text reached the model context, not that the model understood or acted on it.
- Hooks inject only at safe points (session start, prompt submit, after a tool, at stop); a long tool call delays
  delivery; a stopped session receives nothing until it is resumed (opt-in, spends quota) or the human returns.
- A runtime can stop making progress without dying (quota, context, rate limits); this is detected as no progress /
  stalled, never as a crash.
- Headless turns (Codex/OpenCode) answer from the message alone; they run without network inside the runtime's
  sandbox and do not see other messages unless included.
