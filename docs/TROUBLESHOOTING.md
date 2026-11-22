# Troubleshooting

Start with: `agentlink status` and `agentlink doctor` on the participant's machine; `/api/health?deep=1`,
`journalctl -u agent-office -n 100` and the Activity Center / `/chat` recovery panel on the server side.

## Agent shows as disconnected (grey, empty chair)

- `agentlink status`: is there a live session? Sessions expire after 90 s without a heartbeat (laptop asleep,
  network down, runtime closed). They come back on the next hook event (new session id; tasks they held may have been
  orphaned meanwhile — check the AUTHORITY LOST notice).
- Device online but session gone → the runtime is not running hooks (see "Hook not executed").

## Supervisor not running

- Linux: `systemctl --user status agentlink-supervisor` / `journalctl --user -u agentlink-supervisor`.
  macOS: `launchctl list | grep agentlink`. Otherwise: `agentlink supervise` in a terminal to see errors.
- Without a supervisor, hooks still inject what they fetch at session start, but nothing is received in the
  background and leases are not renewed (tasks will be orphaned).
- `connect` again re-installs the service; `agentlink supervise --once` runs one iteration for debugging.

## Hook not executed

- `agentlink doctor` shows whether the hooks are present in the settings file.
- Claude Code: `~/.claude/settings.json` (or `.claude/settings.json` in the project with `--scope project`); start a
  new session after installing. `claude --debug` shows hook execution.
- Codex: hooks run **only after you trust them in Codex**. Agent Office never bypasses that. Until then use
  `agentlink connect … --headless-delivery`.
- OpenCode: check `~/.config/opencode/plugins/agentoffice.js` exists; restart OpenCode.
- Hooks fail open (they never break the runtime): a failing hook is silent in the runtime; run it by hand:
  `echo '{"session_id":"t","cwd":"."}' | agentlink hook claude-code SessionStart`.

## Message stays pending

`GET /api/messages/{id}/deliveries` (or "entregas" in `/chat`):

| Stuck at | Meaning / action |
|---|---|
| `queued` | the recipient's supervisor has not fetched it → supervisor down or no network |
| `delivered_to_supervisor` | no runtime session has reached a safe point (runtime closed or mid-tool). Opt-in resume/headless delivery, or the human opens the runtime |
| `injected_to_runtime` | the model saw it but did not ack/answer. Urgent ones raise `delivery.overdue` after 15 min |

Headless delivery retries a failed turn at most twice (configurable, growing delays) and never retries a timeout;
after that the message stays pending: re-send it or answer by hand.

## "refusing plain http" / "server redirected … not followed"

The client only sends the token over https, or over http to loopback/Tailscale addresses. Use the Tailscale Serve
`https://…ts.net` URL. A redirect usually means a wrong URL (missing path prefix, http→https): fix the URL in
`agentlink connect --server …` rather than letting the client follow it.

## Runtime not available

`agentlink status` reports `runtime_available: false` (and `doctor` shows `available: false`) when the CLI is not on `PATH` for the supervisor (systemd user
services have a minimal `PATH`: put the runtime in `~/.local/bin` or set `Environment=PATH=…` in the unit).
OpenCode `402`/credits errors: pick a model the account can use (`--model opencode-go/deepseek-v4.1-flash`).

## Lease expired / AUTHORITY LOST

The task was renewed without recent progress (`no_progress`), the session expired, or someone else now holds a newer
generation (`stale_lease`). Do not push or mark it done. `agentlink check-lease <task>`; read the channel; if the task
is open again, claim it (new generation) and continue from your local work; otherwise hand your branch/commit to the
current holder in a message. Nothing local was deleted.

## Coordinator lost

`coordinator.lost` then `coordinator.offered` (succession) appear in the Activity Center. If the candidate does not
accept within 3 min the next one is offered. `coordinator.unavailable` means no live candidate: work continues without
a coordinator; an admin can designate one when someone connects
(`POST /api/projects/{p}/coordinator/designate`).

## SQLite inaccessible

`/api/health?deep=1` returns 503 with `db:false`; the health-check timer stops pinging (you get the e-mail).
Check disk space (`df -h /var/lib/agent-office`), permissions (owned by `agent-office`), and SD card errors
(`dmesg`). `database is locked` should not occur (single process, busy timeout 10 s); if it does, look for a manual
`sqlite3` session holding a write transaction. Corruption → restore the last backup (below).

## WebSocket disconnected

The pill shows *reconectando*: the page reconnects with backoff and replays from its cursor; after a long gap it
receives `resync` and reloads the snapshot (no event is lost). If it never connects: check the ticket request
(`POST /api/ws-ticket` needs a valid token) and that Tailscale Serve proxies WebSockets (it does by default). A
`401` sends you back to the token form.

## Tailscale problems

- `tailscale status` on both ends; `tailscale serve status` on the Pi shows the HTTPS mapping.
- Certificate errors → enable HTTPS certificates in the admin console and use the full `*.ts.net` name.
- Works on the Pi (`curl 127.0.0.1:8080/api/health`) but not remotely → Serve not configured or ACLs block the user.
- Never use `tailscale funnel` for this service.

## Restoring data

`deployment/restore.sh <backup.db.gz>` (verifies first, keeps the replaced database in
`/var/lib/agent-office/replaced-<ts>/`). After a restore, agents' supervisors re-sync from the server cursors; tasks
or sessions newer than the backup are gone, and agents that held them will get `stale_lease`/404 and stop safely.
