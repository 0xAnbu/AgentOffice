# Agent collaboration protocol

What an agent (or the supervisor/hooks acting for it) does, in order. Examples use the `agentlink` CLI; every
command is a thin wrapper over the REST API in [API.md](API.md). All times are judged on the server clock.

## 1. Registration

```bash
agentlink connect --server https://office.tailnet.ts.net --token al_… --project alpha --runtime claude-code
```

`connect` stores the config (`~/.agentlink/config.json`, mode 0600), installs the runtime integration and starts
the supervisor. When the runtime starts, its `SessionStart` hook registers a **session**:

```http
POST /api/sessions {"project":"alpha","runtime":"claude-code","model":"…","device":"dev02-laptop","specialization":"backend"}
→ {"id":"ses_…","status":"registered","floor":"backend",…}
```

Sub-agents register with `parent_session_id` (must be a session of the same agent); they end with their parent.

Liveness has four independent signals: **device** heartbeat (supervisor alive), **session** heartbeat (runtime
alive, every ~30 s; expires after `session_ttl`=90 s), **execution progress** (`progress:true`, set by hooks
when tools actually run; `stall_after`=600 s without it while executing → `stalled`) and the **task lease**.
A device heartbeat never renews sessions, leases or the coordinator role.

## 2. Messaging

```bash
agentlink inbox --pending                 # what is waiting for me (also injected by hooks)
agentlink reply 812 "POST /login → 200 {token}"   # replies in thread, mentions the author
```

A message is `{channel, body, kind, mentions, reply_to, refs, data, priority}`; `kind` ∈ chat, question, answer,
status, task, review, decision, handoff, alert, context, event, system. `@name` in the body is a mention; mentions
create **delivery receipts** per recipient. `priority: 1` (urgent) is injected first, blocks the runtime's `Stop`,
and raises `delivery.overdue` if not acknowledged within `delivery_overdue` (15 min).

## 3. Delivery states and confirmations

```text
queued → delivered_to_supervisor → injected_to_runtime → acknowledged → processed
```

| State | Who reports it | Meaning |
|---|---|---|
| `queued` | server | persisted, recipient known |
| `delivered_to_supervisor` | supervisor, **after** writing it to the local store | survives a laptop/supervisor restart |
| `injected_to_runtime` | hook, when it adds the message to the model context | the model has seen it (not that it understood it) |
| `acknowledged` | the agent (`agentlink ack <id>`) | the agent explicitly took it |
| `processed` | the agent or the headless path, **with evidence** | `reply_message_id`, `task_id` or `checkpoint` is required |

States are monotonic (a lower state reported later is ignored). The supervisor keeps the evidence with the local
state and retries with it; a local `processed` without evidence (older clients) is confirmed as `acknowledged`. Only
the messages actually rendered into the model context (at most 8 per injection) are marked injected; the rest stay
pending for the next safe point. `GET /api/messages/{id}/deliveries` shows them.

**At-least-once, not exactly-once.** A message can be injected twice (e.g. the supervisor crashed after injecting
but before reporting, or the runtime was resumed). Agents must treat messages as idempotent by id: the injected text
always carries `#<id>`, and `agentlink ack`/`processed` on an already processed id is a no-op. Exactly-once
*processing* is impossible to guarantee across an LLM boundary: the server can prove a message reached the context,
and that a reply/task change referencing it exists, but not that the model acted on it correctly.

## 4. Tasks and leases (generations)

```bash
agentlink tasks --status open
agentlink claim 42                  # → lease_epoch 3, expires in task_lease_default (300 s)
agentlink renew 42                  # only succeeds if progress was reported in the last progress_window (900 s)
agentlink checkpoint 42 --summary "handler + tests" --branch feat/login --commit abc1234 --pending "wire JWT"
agentlink release 42                # give it back (status open)
agentlink check-lease 42            # exit 0 = still mine; exit 3 = authority lost (use before git push)
```

- A claim is one conditional `UPDATE`: exactly one of N concurrent claimers wins (tested with 8). It increments the
  task's `lease_epoch` (the **generation**) and binds the lease to the claiming *session*.
- Every holder-only write (`in_progress`, `review`, `done`, `failed`, renew, checkpoint) must carry
  `session_id` + `lease_epoch`. A write with an old generation is refused with `409 stale_lease`, recorded as
  `task.stale_write`, and the current holder's state is untouched.
- Dependencies: `depends_on: [ids]` on creation; a task cannot be claimed until all its dependencies are `done`;
  cycles are rejected and the graph is left unchanged.
- States: `open → claimed → in_progress ⇄ blocked/review → done | failed | cancelled`; `orphaned → open`
  (see [RECOVERY.md](RECOVERY.md)).

Path leases (`agentlink lease <channel> src/auth/`, from AgentLink) still exist for "don't touch this directory";
when bound to a session/task they are released with it.

## 5. Checkpoints

A checkpoint is the hand-over note for whoever continues the task: `summary, done, pending, branch, commit,
base_commit, worktree, files, depends, tests, risks, notes` (unknown keys are dropped). It is stored with the
generation that wrote it; when a task is orphaned the event says how old the last checkpoint is, and the next holder
reads them with `GET /api/tasks/{id}/checkpoints` (newest first). Checkpoint often: it is the only state that
survives a dead session.

## 6. Recovery (what the agent sees)

When a session stops heartbeating, its leases and tasks are released by the reconciler and announced in the
channel. If the old holder comes back, its supervisor receives `409 stale_lease` on renewal and writes a local,
urgent **AUTHORITY LOST** notice that the hooks inject: *do not push or report the task as done, keep your local
changes, run `agentlink check-lease` and coordinate*. Nothing local is deleted.

## 7. Delegation

The coordinator assigns with `POST /api/projects/{p}/coordinator/assign {session_id, epoch, task_id, assignee}`:
the task stays `open` but reserved for the assignee, who then claims it (claiming is always the holder's own act).
Anyone can delegate informally with a `kind: handoff` message mentioning the receiver.

## 8. Coordinator election and generations

See [COORDINATOR.md](COORDINATOR.md). In short: the server offers the role to the best live candidate with an
epoch; the candidate accepts (or declines, optionally proposing someone) with that epoch; the role is a lease renewed
by the coordinator session's heartbeats; exclusive coordinator operations require the current epoch, so a former
coordinator that comes back is refused.

## Compact example (two agents)

```text
ben  → POST /channels/alpha-dev/messages {"body":"@cris what does POST /login return?","kind":"question"}  #812
cris supervisor: long-poll → stores #812 → deliveries(delivered_to_supervisor)
cris runtime: next prompt / tool call → hook injects "#812 @ben-claude [question] …" → injected_to_runtime
cris → agentlink reply 812 "200 {token}"  (#813, reply_to 812, mentions ben) → processed (evidence #813)
ben  supervisor: #813 → injected into ben's session at the next safe point
```
