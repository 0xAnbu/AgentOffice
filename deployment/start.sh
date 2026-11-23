#!/usr/bin/env bash
# Start AgentOffice again after deployment/stop.sh: tailscaled (if installed and stopped; it reconnects on its own
# with the saved registration and Serve/Funnel settings), then the backend and its timers, enabled at boot.
#   sudo deployment/start.sh
# To check everything afterwards: sudo deployment/verify-live.sh
set -euo pipefail
HERE=$(dirname "$(readlink -f "$0")")
. "$HERE/lib.sh"
case ${1:-} in
  "") ;;
  -h|--help) sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) die "unknown option $1 (see --help)" ;;
esac
[ "$AO_USER_MODE" = 1 ] || [ "$(id -u)" = 0 ] || die "run as root (sudo)"
if [ "$AO_USER_MODE" = 0 ] && systemctl cat tailscaled.service >/dev/null 2>&1 && ! systemctl is-active --quiet tailscaled.service; then
  log "starting tailscaled"
  systemctl enable --now tailscaled.service >/dev/null
  for _ in $(seq 30); do tailscale status >/dev/null 2>&1 && break; sleep 1; done
  tailscale status >/dev/null 2>&1 || log "WARN: Tailscale is not connected yet; check with: tailscale status"
fi
sctl reset-failed agent-office.service 2>/dev/null || true
sctl enable --now agent-office.service agent-office-healthcheck.timer agent-office-backup.timer >/dev/null 2>&1
wait_healthy 40 || die "agent-office.service is not healthy; inspect: journalctl -u agent-office -n 50"
log "started: agent-office.service healthy on 127.0.0.1:$AO_PORT, timers active"
