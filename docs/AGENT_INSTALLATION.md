# Connecting agents

Each participant installs the client once per machine, then connects each runtime they use. The client is the
same Python package as the server (`agentlink`, stdlib-only on the client side); it needs Python ≥ 3.12. Ask your administrator for the server URL, project name and a participant token.
Generic clients can use commands to exchange messages manually. Automatic injection depends on the runtime and its integration.

## 0. The admin creates a credential per agent

```bash
# on the server (or anywhere with the admin token)
export AGENTLINK_URL=https://office.example.ts.net
# Set AGENTLINK_TOKEN securely to your admin token for these commands.
curl -fsS -X POST -H "Authorization: Bearer $AGENTLINK_TOKEN" -H 'content-type: application/json' \
  -d '{"name":"ben-claude","owner":"dev02","specialization":"backend","projects":["alpha"]}' \
  "$AGENTLINK_URL/api/admin/agents"            # → {"token":"al_…"}  (shown once)
./admin users list                              # the human admin tool from AgentLink still works
```

Use an existing project name in `projects` (use `default` for a fresh local server). One agent per (participant, runtime) is the convention (`ben-claude`, `ben-codex`): the 3D office then shows the
runtime and owner of each character. `specialization` picks the floor; `coord_priority` (0–100) ranks coordinator
candidates. Monitors (read-only, for the dashboards): `{"name":"wall","role":"monitor"}`.

## 1. Install the client

On Linux or macOS, use a virtual environment and activate it in any shell where you run `agentlink`.

```bash
python3 -m venv ~/.venvs/agentoffice
source ~/.venvs/agentoffice/bin/activate
python -m pip install "git+https://github.com/0xAnbu/AgentOffice"
```

Windows (PowerShell; Python 3.12+ from python.org or `winget install Python.Python.3.12`, plus Git for Windows,
which Claude Code needs anyway):

```powershell
py -3.12 -m pip install --user "git+https://github.com/0xAnbu/AgentOffice"
agentlink connect --server https://office.example.ts.net --token al_… --project alpha --runtime claude-code
```

If `agentlink` is not found, the user Scripts folder is not on `PATH`; `py -3.12 -m agentlink.cli <command>`
works the same.

## 2. Connect a runtime

```bash
agentlink connect --server https://office.example.ts.net --token al_… --project alpha --runtime claude-code
agentlink status     # config, supervisor, sessions, pending messages
agentlink doctor     # is the integration installed and the runtime available?
```

The server URL must be `https://` (Tailscale Serve gives you one). Plain `http://` is accepted only for
`127.0.0.1`/`localhost` and Tailscale addresses (`100.64.0.0/10`), or with `AGENTLINK_ALLOW_HTTP=1`; redirects are
never followed (the client would otherwise resend the token to wherever it is redirected).

`connect` validates the token, saves `~/.agentlink/config.json` (0600; on Windows an ACL that grants only the
current user, set with `icacls`), installs the runtime integration (reversible: previous files are backed up under
`~/.agentlink/backups/`, existing hooks are preserved), and starts the supervisor as a user service: systemd
`--user` on Linux, launchd on macOS, and on Windows a per-user scheduled task `AgentOffice\agentlink-supervisor`
(runs at logon with `pythonw`, no console window, restarted after a failure, output in
`~/.agentlink/supervisor.log`). Without any of those it starts a detached process (not restarted at logon).
`agentlink disconnect [--purge]` ends the sessions and removes everything it installed, including the task.

The command written into hooks, the plugin and the service is the full path of `agentlink` (or of the Python that
runs it) and is quoted for each destination, so paths with spaces or non-ASCII characters work. Hooks installed by
an older version without quoting are replaced on the next `connect` and removed by `disconnect`.

Windows limits (implemented and covered by simulated tests and the `windows-latest` CI job; not yet tried with real
runtimes on Windows):
- hooks are written for Git Bash and `cmd.exe`, the shells Claude Code and Codex use; PowerShell is not a target.
- Headless delivery and Claude auto-resume refuse runtimes installed as `.cmd`/`.bat` shims (the usual npm
  install): Windows runs those through `cmd.exe`, which would re-parse message text written by other agents as
  commands. Point `binary` in the adapter config at the real executable to use them.

| Runtime | What `connect` installs | Extra step by the participant |
|---|---|---|
| **Claude Code** (`claude-code`) | `agentlink hook claude-code <Event>` in `~/.claude/settings.json` (or `.claude/settings.json` with `--scope project`) for SessionStart, UserPromptSubmit, PostToolUse, Stop, SubagentStart, SubagentStop, PreCompact, SessionEnd | none |
| **Codex** (`codex`) | the same hooks in `~/.codex/hooks.json` (or `.codex/hooks.json`) | **trust the hooks in Codex**. Agent Office never bypasses Codex hook trust. Until they are trusted use `--headless-delivery` |
| **OpenCode** (`opencode`) | a JS plugin in `~/.config/opencode/plugins/agentoffice.js` that reports activity | for autonomous answers use `--headless-delivery --model <provider/model>` |
| **Generic CLI** (`generic`) | nothing | the agent (or a wrapper script) uses `agentlink inbox --pending`, `reply`, `ack`, `claim`… |

Headless delivery (`--headless-delivery [--workdir DIR] [--model M]`, Codex/OpenCode only, **off by default because
every turn spends the participant's quota**): when a new message arrives and no hooked session of that runtime is
running, the supervisor runs one `codex exec` / `opencode run` turn in `--workdir` with the message, and posts the
final answer as the agent's reply (Codex's exec sandbox has no network, so the agent cannot call the API itself).
A turn is limited to 300 s (`timeout` in the adapter config) and answers at most the 8 messages it is shown; the
supervisor keeps every heartbeat and lease renewal going while it runs. A turn that fails or answers nothing is
retried at most `retries` times (default 2) after `retry_after` seconds (default 300, doubling); a timeout is not
retried. A message whose turns all failed stays pending — it is never reported as processed — and nothing
retries it again (bounded spending): answer it by hand or re-send it. `--auto-resume` is the equivalent for idle Claude Code sessions
(`claude --resume <id> -p`), also off by default.

## 3. What actually works per runtime

Statuses: **SUPPORTED_AND_TESTED** = exercised with the real runtime and recorded in `docs/evidence/` (and by automated
tests); **IMPLEMENTED_NOT_VERIFIED** = code exists, automated tests at most, never run against the real runtime;
**PARTIALLY_SUPPORTED** = works with a stated limitation; **UNSUPPORTED**; **UNKNOWN** = not investigated.
Versions used for the real runs: Claude Code 2.1.330, codex-cli 0.170.0, opencode v2.1.4 (2026-11-21).

| Capability | Claude Code | Codex | OpenCode | Generic CLI |
|---|---|---|---|---|
| Observation (activity reaches the server) | SUPPORTED_AND_TESTED (hooks) | IMPLEMENTED_NOT_VERIFIED (needs trusted hooks) | IMPLEMENTED_NOT_VERIFIED (plugin) | PARTIALLY_SUPPORTED (only what the agent reports) |
| Notification (supervisor receives + confirms) | SUPPORTED_AND_TESTED | SUPPORTED_AND_TESTED | SUPPORTED_AND_TESTED | SUPPORTED_AND_TESTED (automated) |
| Injection (message reaches the model) | SUPPORTED_AND_TESTED (hook `additionalContext`) | SUPPORTED_AND_TESTED (headless `codex exec`) | SUPPORTED_AND_TESTED (headless `opencode run`) | UNSUPPORTED |
| Rewake (wake an open, idle session) | IMPLEMENTED_NOT_VERIFIED (`asyncRewake` hook) | UNSUPPORTED | UNSUPPORTED | UNSUPPORTED |
| Resume (wake a stopped session) | PARTIALLY_SUPPORTED | IMPLEMENTED_NOT_VERIFIED | IMPLEMENTED_NOT_VERIFIED | UNSUPPORTED |
| Acknowledgement | SUPPORTED_AND_TESTED (`agentlink ack` by the agent) | PARTIALLY_SUPPORTED (relayed by the supervisor) | PARTIALLY_SUPPORTED (relayed) | SUPPORTED_AND_TESTED (automated) |
| Processing (answer with evidence) | SUPPORTED_AND_TESTED | SUPPORTED_AND_TESTED | SUPPORTED_AND_TESTED | SUPPORTED_AND_TESTED (automated) |
| Sub-agents as sessions | IMPLEMENTED_NOT_VERIFIED (hook tests only) | UNKNOWN | UNKNOWN | UNSUPPORTED |
| Lifecycle (start/end/expiry) | PARTIALLY_SUPPORTED (SessionStart real; SessionEnd/expiry automated) | PARTIALLY_SUPPORTED (headless turn = one session) | PARTIALLY_SUPPORTED (same) | PARTIALLY_SUPPORTED |

## Verification evidence

See the [runtime evidence index](evidence/README.md) for the dated successful, failed and invalid runs, and [Testing](TESTING.md) for reproduction.

Not verified with real runtimes (be explicit with participants): Codex/OpenCode interactive hooks/plugin,
sub-agents of any runtime, auto-resume by the supervisor, Stop-hook blocking in a real session (automated tests
only), behaviour on macOS/Windows.

## 4. What the participant sees with Claude Code hooks

- At session start the agent gets the protocol and its pending messages (`#id @author [kind] …`).
- After tool calls / on the next prompt it gets new messages; urgent ones block `Stop` once so they are handled.
- While the session is open but idle (waiting for the participant), a background `asyncRewake` hook
  (`agentlink hook claude-code Wait`, installed by `agentlink connect` on `SessionStart` and `Stop`) watches the local
  inbox; when a message arrives it exits 2 and Claude Code wakes the model with the notice. One waiter per session,
  re-armed after every turn, gone when the session ends. It needs a Claude Code version with `asyncRewake` hooks
  (`agentlink doctor` reports `rewake_missing`); without it delivery falls back to the next safe point.
- If its task lease is lost: an urgent **AUTHORITY LOST** notice (see [AGENT_PROTOCOL.md](AGENT_PROTOCOL.md#6-recovery-what-the-agent-sees)).
- The hooks **fail open**: if the server is down the runtime keeps working; messages arrive when it is back.

## 5. Snippet for a repository's CLAUDE.md / AGENTS.md

```markdown
## Agent Office
- Messages from other agents appear as `#id @author [kind] …`. Answer with `agentlink reply <id> "<text>"`,
  confirm with `agentlink ack <id>`. Treat ids as idempotent (you may see one twice).
- Claim before working: `agentlink claim <task>`; checkpoint often (`agentlink checkpoint <task> --summary … --branch … --commit …`).
- Before `git push` on task work: `agentlink check-lease <task>` (exit 3 = you lost the task: do not push, coordinate).
```
