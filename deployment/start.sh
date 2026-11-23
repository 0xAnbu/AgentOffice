#!/usr/bin/env bash
# Start AgentOffice again after deployment/stop.sh: tailscaled (if installed and stopped; it reconnects on its own
# with the saved registration and Serve/Funnel settings), then the backend and its timers, enabled at boot.
#   sudo deployment/start.sh
# Prints the URL (Tailscale when connected) and the admin token. To check everything: sudo deployment/verify-live.sh
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

# Where to open it and how to log in: the private Tailscale URL (if this server is on a tailnet) and the admin token.
url="http://127.0.0.1:$AO_PORT"
if [ "$AO_USER_MODE" = 0 ] && command -v tailscale >/dev/null 2>&1; then
  dns=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("Self",{}).get("DNSName","").rstrip("."))' 2>/dev/null || true)
  [ -n "$dns" ] && url="https://$dns"
fi
token=""
[ -r "$AO_ENV_FILE" ] && token=$(sed -n 's/^AGENTLINK_ADMIN_TOKEN=//p' "$AO_ENV_FILE" | tail -1)
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then B=$'\033[1m'; G=$'\033[1;32m'; C=$'\033[36m'; D=$'\033[2m'; R=$'\033[0m'; else B= G= C= D= R=; fi
printf '\n%s── AgentOffice is running ──%s\n' "$C" "$R"
printf '  %-13s %s%s%s\n' "URL:" "$B" "$url" "$R"
printf '  %-13s %s%s%s\n' "Admin token:" "$G" "${token:-(not found in $AO_ENV_FILE)}" "$R"
printf '  %-13s %s\n' "Admin page:" "$url/admin"
printf '%s  Keep the token private: it can do everything.%s\n' "$D" "$R"
