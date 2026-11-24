# Codex: messages queued into the open terminal (`codex queue --thread`), 2026-11-24

codex-cli 0.171.0, Linux x86_64. A throwaway thread, its TUI (`codex resume <thread>`) driven inside tmux and read
with `tmux capture-pane`. Hooks not trusted in that TUI (only the delivery path was under test). No daemon, no
`--remote`, no MCP.

## Why not `codex exec resume`

A Codex participant reported (2026-11-23) that the previous auto-resume (`codex exec resume <thread> <prompt>`):
failed outside a git repo without `--skip-git-repo-check`; was marked `injected_to_runtime` even when the command
failed; failed with `thread-store conflict: … already has an active writer` while the participant's terminal held the
thread; and, when run as a separate turn, its answer reached #dev but never appeared in the terminal the human was
watching. The suggested fix (shared app-server daemon + `codex resume --remote` + `turn/start` over the control
socket) requires restarting the participant's terminal; `codex queue` does the same without it.

## Runs (screen captures, blank lines removed)

1. Idle TUI, `codex queue --thread <id> --message "[Agent Office #999 de @test] Responde solo: PONG"`:

       › [Agent Office #999 de @test] Responde solo: PONG
       • PONG
         Worked for 2s • 3:16 PM

2. Busy TUI (a long turn typed in the terminal), queue `… PONG2`: queued, answered in order, visible.
3. TUI closed: `codex queue` exits 0, the item stays in `~/.codex/queue_1.sqlite` (`queued_items` count 1 after 15 s)
   and is answered as soon as the thread is reopened (`PONG4`).
4. The adapter itself (`ADAPTERS["codex"].push` with `auto_resume`, text from `hooks.render`) → `codex queue`:

       › [AgentOffice] 1 message(s) addressed to you · project honey
         Reply: agentlink reply <id> "text"   ·   Acknowledge only: agentlink ack <id>   ·   Details: agentlink show <id>
         #4242 P2 from @javi in #dev [chat]: Prueba E2E del adaptador: contesta solo con la palabra RECIBIDO (no ejecutes comandos).
       • RECIBIDO
         Worked for 2s • 3:21 PM

Not covered by this run: the full supervisor loop against a live server with trusted hooks (automated in
`tests/test_codex_queue.py` with a fake `codex`), and the agent answering through `agentlink reply` from the TUI
sandbox (it may ask the human to approve network access).
