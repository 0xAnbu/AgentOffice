#!/usr/bin/env bash
# Stop AgentOffice: the backend, its health-check and backup timers, and (with --tailscale) the Tailscale daemon,
# which removes the tailscale0 interface. Then checks that nothing is left and lists what remains.
#   sudo deployment/stop.sh                  stop the app until the next reboot (or deployment/start.sh)
#   sudo deployment/stop.sh --tailscale      also stop tailscaled: no VPN, no tailscale0, no Serve/Funnel
#   sudo deployment/stop.sh --disable        also keep it stopped after a reboot (with --tailscale: tailscaled too)
#   --force   stop Tailscale even when this SSH session itself comes through the tailnet (it will drop)
# Data, configuration and the device's registration in the tailnet are kept: deployment/start.sh brings it all back.
set -euo pipefail
HERE=$(dirname "$(readlink -f "$0")")
. "$HERE/lib.sh"
TS=0 DISABLE=0 FORCE=0
for a in "$@"; do
  case $a in
    --tailscale) TS=1 ;;
    --disable) DISABLE=1 ;;
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option $a (see --help)" ;;
  esac
done

# An SSH session from a 100.64.0.0/10 address arrives over Tailscale: stopping it would cut this session off.
client=${SSH_CONNECTION:-}; client=${client%% *}
if [ "$TS" = 1 ] && [ "$FORCE" = 0 ] && [ -n "${SSH_CONNECTION:-}" ] && "$PYTHON" - "$client" <<'PY'
import ipaddress, sys
try:
    sys.exit(0 if ipaddress.ip_address(sys.argv[1]) in ipaddress.ip_network("100.64.0.0/10") else 1)
except ValueError:
    sys.exit(1)
PY
then
  die "this SSH session comes through Tailscale ($client): stopping it would disconnect you. Use the local network or the console, or add --force"
fi

[ "$AO_USER_MODE" = 1 ] || [ "$(id -u)" = 0 ] || die "run as root (sudo)"
[ "$TS" = 0 ] || [ "$AO_USER_MODE" = 0 ] || die "--tailscale is not available in user mode"

TIMERS="agent-office-healthcheck.timer agent-office-backup.timer"
SERVICES="agent-office.service agent-office-healthcheck.service agent-office-backup.service"
log "stopping $TIMERS $SERVICES"
# shellcheck disable=SC2086
sctl stop $TIMERS $SERVICES
if [ "$DISABLE" = 1 ]; then
  # shellcheck disable=SC2086
  sctl disable $TIMERS agent-office.service >/dev/null 2>&1 || true
  log "disabled: will not start at boot (deployment/start.sh enables it again)"
fi
if [ "$TS" = 1 ]; then
  if systemctl cat tailscaled.service >/dev/null 2>&1; then
    log "stopping tailscaled (the device stays registered; Serve/Funnel settings are kept for start.sh)"
    systemctl stop tailscaled.service
    [ "$DISABLE" = 0 ] || systemctl disable tailscaled.service >/dev/null 2>&1 || true
  else
    log "tailscaled is not installed: nothing to stop"
  fi
fi

# What is left?
left=0
for u in $TIMERS $SERVICES; do
  st=$(sctl is-active "$u" 2>/dev/null || true)
  if [ "$st" = active ] || [ "$st" = activating ]; then log "still running: $u ($st)"; left=1; fi
done
if ss -ltnH "sport = :$AO_PORT" 2>/dev/null | grep -q .; then log "port $AO_PORT is still listening: $(ss -ltnpH "sport = :$AO_PORT" 2>/dev/null | head -1)"; left=1; fi
if [ "$AO_USER_MODE" = 0 ] && id "$AO_USER" >/dev/null 2>&1 && pgrep -u "$AO_USER" >/dev/null; then
  log "processes of user $AO_USER still running: $(pgrep -u "$AO_USER" -d ' ')"; left=1
fi
if [ "$TS" = 1 ]; then
  if systemctl is-active --quiet tailscaled.service; then log "still running: tailscaled.service"; left=1; fi
  if pgrep -x tailscaled >/dev/null; then log "tailscaled process still running: $(pgrep -x tailscaled -d ' ')"; left=1; fi
  if ip link show tailscale0 >/dev/null 2>&1; then log "interface tailscale0 still present"; left=1; fi
fi
[ "$left" = 0 ] || die "not everything stopped (see above)"
log "stopped: no AgentOffice unit active, port $AO_PORT free$([ "$TS" = 1 ] && echo ", tailscaled stopped, no tailscale0 interface")"
[ "$TS" = 1 ] || log "Tailscale is still connected (tailscale0 up); add --tailscale to stop it too"
log "Healthchecks.io stops receiving heartbeats: it will alert after the grace period unless you pause the check"
