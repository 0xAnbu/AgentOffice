#!/usr/bin/env bash
# Run by agent-office-healthcheck.timer, a process independent of the backend. Pings the external monitor
# (Healthchecks.io) ONLY when the local deep check passes: process up, SQLite read+write ok, reconciler loop alive.
# When anything fails it sends nothing, so the missing heartbeat is what makes Healthchecks.io alert a human —
# a dead server never has to report its own death. No message content or other internal data leaves the machine:
# the ping is an empty request to the configured URL.
set -euo pipefail
. "$(dirname "$(readlink -f "$0")")/lib.sh"
[ -r "$AO_ENV_FILE" ] && HC_PING_URL=${HC_PING_URL:-$(sed -n 's/^HC_PING_URL=//p' "$AO_ENV_FILE" | tail -1)}
HC_PING_URL=${HC_PING_URL:-}
BODY=$(curl -fsS -m 10 "$AO_URL/api/health?deep=1" 2>&1) || { log "UNHEALTHY: $BODY"; exit 1; }
"$PYTHON" -c 'import json,sys; d=json.loads(sys.argv[1]); sys.exit(0 if d.get("ok") and d.get("db") and d.get("reconciler") else 1)' "$BODY" \
  || { log "UNHEALTHY: $BODY"; exit 1; }
if [ -n "$HC_PING_URL" ]; then
  curl -fsS -m 10 --retry 3 -o /dev/null "$HC_PING_URL" || { log "healthy, but the external ping failed"; exit 2; }
  log "healthy; heartbeat sent"
else
  log "healthy (HC_PING_URL not set: no external heartbeat)"
fi
