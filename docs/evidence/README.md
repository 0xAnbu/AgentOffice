# Evidence

Raw, unedited outputs of verification runs. Failed and invalid runs are kept on purpose. No tokens are stored.

| File | Produced by | What it shows |
|---|---|---|
| `runtime-e2e-20261121T221923Z.json` | `scripts/e2e_runtimes.py --only claude` | Claude Code 2.1.330, real session: hooks → injection → the agent replies → `processed`. 8/8 |
| `runtime-e2e-20261121T221937Z.json` | `--only codex,opencode` | Codex 4/4. **OpenCode half invalid**: the runtime failed (402) and a since-fixed bug still marked the delivery processed |
| `runtime-e2e-20261121T223223Z.json` | `--only opencode` | **Failed**: OpenCode turn hung 10 min (prompt suggested unusable tools). Fixed afterwards |
| `runtime-e2e-20261121T224136Z.json` | `--only opencode` | OpenCode v2.1.4 with `opencode-go/deepseek-v4.1-flash`: 4/4 (direct `exec_deliver`) |
| `runtime-e2e-20261121T224306Z.json` | `--only cross` | Claude Code → server → Codex → server → the same resumed Claude session acknowledges and uses the answer. 6/6 |
| `runtime-e2e-20261121T232513Z.json` | `--only codex,opencode` | Codex 4/4 and OpenCode 4/4 through the production supervisor loop (`--headless-delivery`) |
| `deployment-verify-*.log` | `deployment/verify.sh` | 18/18 deployment checks on Linux x86_64 under `systemd --user` (**not** a Raspberry Pi) |
| `visual/` | `scripts/visual_check.py --admin-token` | Headless Chromium (SwiftShader, no GPU): screenshots of the building, a floor, an agent and `/chat`; `report.json` with JS errors (none), scene summary, WebSocket drop/replay and 300-event burst results |

Reproduce: see [TESTING.md](../TESTING.md).
