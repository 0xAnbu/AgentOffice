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
| `runtime-e2e-20261122T024122Z.json` | `--only codex` | Codex 4/4 through the supervisor loop after the timeout/keep-alive changes |
| `runtime-e2e-20261122T024142Z.json` | `--only codex-timeout` | Real Codex turn cut at 1 s: exit 124, supervisor survives, message stays pending, not re-run. 4/4 |
| `deployment-verify-*.log` | `deployment/verify.sh` | 22/22 deployment checks (20/20 before stop.sh/start.sh) on Linux x86_64 under `systemd --user` (**not** a Raspberry Pi) |
| `automated-deployment-audit.md` | review before the automated deployment work | state of the tooling at `5987a92`, gaps, risks, decisions |
| `automated-deployment-validation.md` | the deploy test levels | results by level (real services and Raspberry Pi **NOT_VERIFIED**), per-phase YAML contracts |
| `automated-deployment-tests-20261123.log` | `pytest -v` on the deploy tests + e2e | each deploy test result, the end-to-end bootstrap run and the suite summary |
| `render-before-main-392f361-*.json` | `render_check.py --repo <main checkout> --no-rates` | Before the rendering fixes: resources leaked on every rebuild and theme switch; fixed 30 fps loop |
| `render-after-*.json` | `render_check.py` | After: no leak, adaptive frame rate, CPU/memory per case. Software WebGL: compare ratios, not absolute values |
| `visual/` | `scripts/visual_check.py --admin-token` | Headless Chromium (SwiftShader, no GPU): screenshots of the building, a floor, an agent, `/chat` and `/tareas` (board and task panel); `report.json` with JS errors (none), scene summary, WebSocket drop/replay and 300-event burst results, the anonymous view (only the login form), the `/chat` scroll behaviour and `/tareas` (refused for the monitor token, board, grouping and panel for the admin) |

Reproduce: see [TESTING.md](../TESTING.md).

## Agent workflow lab (2026-11-22)

Historical notes from the original agent guide. These observations refer to that lab and its message IDs.

- Trabajo registrado en varios hilos de `admin-lab`. La solicitud inicial #294 se publicó en `chatting`. El resultado de recuperación quedó en #383 y el cierre bilateral en #384 y #387.
- Administración: pruebas con `lab-bot`, `lab-monitor` y `lab-token-6c2f29c9`. Para este último, la revocación del token original y la deshabilitación del agente devolvieron HTTP 401 al intentar autenticar con el token afectado. El borrado eliminó sus dos tokens.
- Tareas: `#21` (done), `#22` (checkpoint + renew), `#25/#26` (dependencias + release),
  `#27/#28` (UI, canceladas en limpieza), `#29` (recovery), `#30` (recovery por expiración, de codex).
- Coordinación: epoch 3 (accept/assign/resign). El decline del epoch 5 devolvió `session_ended`. El decline del epoch 6 se aceptó y el servidor reofertó con epoch 7. Super-Deep confirmó que también declinó esa oferta.
- Leases: `src/lab.py` (conflicto 409), `src/codex.py` (acquire/release), `ui/test.py`, `ui/live.py`.
- KV: `lab:config` v1; `if_version=0` → 409.
- WebSocket: ticket de un uso, `hello` + `events`; actualización en vivo en `/chat` y `/`.
- MCP: `initialize`, `tools/list` (18), `whoami`.
- UI `/chat`: login, búsqueda, filtros, hilo, live, paneles. Captura `chat-ui-final.png`.
- UI 3D: cámara por planta, Día/Noche, presencia `trabajando`→`cerrado`, actividad en vivo.
  Capturas `office-3d-checks.png`, `office-3d-presence.png`.
- Recuperación: `task.orphaned` → reclaim epoch 2 → `task.stale_write` 409 en el holder viejo.
- Limpieza final: `lab-bot` y `lab-monitor` borrados; tareas de UI canceladas; sin leases activos.
