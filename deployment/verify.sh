#!/usr/bin/env bash
# Verifies the REAL deployment scripts and units on this Linux machine without root, in a throw-away sandbox driven
# by `systemd --user`. It is NOT a Raspberry Pi verification (see docs/PHASE_ACCEPTANCE.md, phase 7).
# Checks: install, service start, auth, loopback-only binding, persistence across restart, automatic restart after a
# crash (SIGKILL), consistent backup under concurrent writes, restore, health check + heartbeat sent only when healthy,
# update, manual rollback and automatic rollback of a broken release. Cleans up after itself.
#   deployment/verify.sh [evidence-file]
set -euo pipefail
REPO=$(readlink -f "$(dirname "$0")/..")
S=$(mktemp -d /tmp/ao-verify.XXXXXX)
export AO_USER_MODE=1 AO_HOME=$S/opt AO_DATA=$S/data AO_ETC=$S/etc AO_BACKUP_DIR=$S/backups
export AO_UNIT_DIR=$HOME/.config/systemd/user AO_PORT=$((21000 + RANDOM % 9000))
export AO_URL=http://127.0.0.1:$AO_PORT AO_ENV_FILE=$S/etc/agent-office.env
UNITS="agent-office.service agent-office-healthcheck.service agent-office-healthcheck.timer agent-office-backup.service agent-office-backup.timer"
for u in $UNITS; do [ -e "$AO_UNIT_DIR/$u" ] && { echo "refusing: $AO_UNIT_DIR/$u already exists"; exit 1; }; done
PASS=0
ok() { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; exit 1; }
HC_PID=""
cleanup() {
  systemctl --user disable --now $UNITS >/dev/null 2>&1 || true
  for u in $UNITS; do rm -f "$AO_UNIT_DIR/$u"; done
  systemctl --user daemon-reload || true
  [ -n "$HC_PID" ] && kill "$HC_PID" 2>/dev/null || true
  rm -rf "$S"
}
trap cleanup EXIT
D="$REPO/deployment"
api() { local m=$1 u=$2; shift 2; curl -fsS -X "$m" -H "Authorization: Bearer $ADMIN" -H 'content-type: application/json' "$@" "$AO_URL$u"; }
count() { api GET "/api/channels/ops/messages?limit=200" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["messages"]))'; }
mainpid() { systemctl --user show -p MainPID --value agent-office.service; }
start() { systemctl --user reset-failed "$@" 2>/dev/null || true; systemctl --user start "$@"; }
healthy() { for _ in $(seq 60); do curl -fs "$AO_URL/api/health?deep=1" >/dev/null && return 0; sleep 0.5; done; return 1; }

echo "== sandbox $S, port $AO_PORT, commit $(git -C "$REPO" rev-parse --short HEAD)"
"$D/install.sh" HEAD > "$S/install.log" 2>&1 || { cat "$S/install.log"; fail "install.sh"; }
ADMIN=$(sed -n 's/^AGENTLINK_ADMIN_TOKEN=//p' "$AO_ENV_FILE")
{ [ "$(systemctl --user is-active agent-office.service)" = active ]; } || fail "install.sh: service active, deep health ok"; ok "install.sh: service active, deep health ok"
{ [ "$(stat -c %a "$AO_ENV_FILE")" = 640 ]; } || fail "secrets file mode 0640"; ok "secrets file mode 0640"

{ code=$(curl -s -o /dev/null -w '%{http_code}' "$AO_URL/api/me"); [ "$code" = 401 ]; } || fail "no token -> 401"; ok "no token -> 401"
{ code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer al_wrong" "$AO_URL/api/me"); [ "$code" = 401 ]; } || fail "wrong token -> 401"; ok "wrong token -> 401"
{ api GET /api/me >/dev/null; } || fail "admin token accepted"; ok "admin token accepted"
BIND=$(ss -ltnH "sport = :$AO_PORT" | awk '{print $4}' | sort -u | tr '\n' ' ')
{ [ "$BIND" = "127.0.0.1:$AO_PORT " ]; } || fail "listens on loopback only ($BIND)"; ok "listens on loopback only ($BIND)"

api POST /api/channels -d '{"name":"ops"}' >/dev/null
for i in 1 2 3; do api POST /api/channels/ops/messages -d "{\"body\":\"persist $i\"}" >/dev/null; done
systemctl --user restart agent-office.service; sleep 1
for _ in $(seq 40); do curl -fs "$AO_URL/api/health?deep=1" >/dev/null && break; sleep 0.5; done
{ [ "$(count)" = 3 ]; } || fail "persistence across restart (3 messages)"; ok "persistence across restart (3 messages)"

P1=$(mainpid); kill -9 "$P1"
for _ in $(seq 40); do P2=$(mainpid); [ "$P2" != 0 ] && [ "$P2" != "$P1" ] && curl -fs "$AO_URL/api/health?deep=1" >/dev/null && break; sleep 0.5; done
{ [ "$P2" != "$P1" ] && [ "$P2" != 0 ]; } || fail "automatic restart after SIGKILL (pid $P1 -> $P2)"; ok "automatic restart after SIGKILL (pid $P1 -> $P2)"
{ [ "$(count)" = 3 ]; } || fail "no data lost after crash"; ok "no data lost after crash"

( for i in $(seq 40); do api POST /api/channels/ops/messages -d "{\"body\":\"load $i\"}" >/dev/null || true; done ) &
W=$!
BK=$("$D/backup.sh" 2>"$S/backup.log")
wait $W
{ grep -q "integrity ok" "$S/backup.log" && [ -f "$BK.sha256" ]; } || fail "backup during concurrent writes: integrity ok, sha256 written ($(basename "$BK"))"; ok "backup during concurrent writes: integrity ok, sha256 written ($(basename "$BK"))"
N_BK=$(gzip -dc "$BK" | python3 -c 'import sqlite3,sys,tempfile;f=tempfile.NamedTemporaryFile(suffix=".db");f.write(sys.stdin.buffer.read());f.flush();print(sqlite3.connect(f.name).execute("select count(*) from messages m join channels c on c.id=m.channel_id where c.name=\"ops\"").fetchone()[0])')
api POST /api/channels/ops/messages -d '{"body":"after backup"}' >/dev/null
"$D/restore.sh" "$BK" --yes > "$S/restore.log" 2>&1 || { cat "$S/restore.log"; fail "restore"; }
{ [ "$(count)" = "$N_BK" ]; } || fail "restore: back to the backup's $N_BK messages, service healthy, old db kept aside"; ok "restore: back to the backup's $N_BK messages, service healthy, old db kept aside"
{ ls -d "$AO_DATA"/replaced-* >/dev/null; } || fail "replaced database kept aside"; ok "replaced database kept aside"

# Fake Healthchecks.io endpoint: counts pings.
python3 - "$S/pings" > /dev/null 2>&1 <<'PY' &
import http.server, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        open(sys.argv[1], "a").write(self.path + "\n"); self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *a): pass
s = http.server.HTTPServer(("127.0.0.1", 0), H); open(sys.argv[1] + ".port", "w").write(str(s.server_port)); s.serve_forever()
PY
HC_PID=$!
for _ in $(seq 20); do [ -s "$S/pings.port" ] && break; sleep 0.2; done
echo "HC_PING_URL=http://127.0.0.1:$(cat "$S/pings.port")/ping/test-uuid" >> "$AO_ENV_FILE"
start agent-office-healthcheck.service
{ [ "$(wc -l < "$S/pings")" = 1 ]; } || fail "healthcheck unit (timer target): healthy -> exactly one heartbeat sent"; ok "healthcheck unit (timer target): healthy -> exactly one heartbeat sent"
systemctl --user stop agent-office.service
if start agent-office-healthcheck.service 2>/dev/null; then fail "healthcheck should fail when the app is down"; fi
{ [ "$(wc -l < "$S/pings")" = 1 ]; } || fail "app down -> healthcheck fails and NO heartbeat is sent"; ok "app down -> healthcheck fails and NO heartbeat is sent"
start agent-office.service; healthy || fail "service did not come back"
{ systemctl --user list-timers --all | grep -q agent-office-healthcheck.timer && systemctl --user list-timers --all | grep -q agent-office-backup.timer; } || fail "healthcheck and backup timers installed"; ok "healthcheck and backup timers installed"

R1=$(readlink -f "$AO_HOME/current")
"$D/update.sh" HEAD > "$S/update.log" 2>&1 || { cat "$S/update.log"; fail "update"; }
R2=$(readlink -f "$AO_HOME/current")
{ [ "$R1" != "$R2" ] && [ "$(count)" -ge 1 ]; } || fail "update.sh: new release $(basename "$R2"), pre-update backup taken, healthy"; ok "update.sh: new release $(basename "$R2"), pre-update backup taken, healthy"
"$D/update.sh" --rollback > "$S/rollback.log" 2>&1 || { cat "$S/rollback.log"; fail "rollback"; }
{ [ "$(readlink -f "$AO_HOME/current")" = "$R1" ] && curl -fs "$AO_URL/api/health?deep=1" >/dev/null; } || fail "update.sh --rollback: back on $(basename "$R1"), healthy"; ok "update.sh --rollback: back on $(basename "$R1"), healthy"

# A release that imports fine but cannot serve: must be rolled back automatically.
BAD=$S/bad-src; git clone -q "$REPO" "$BAD"
sed -i 's/^def main() -> None:/def main() -> None:\n    raise SystemExit("deliberately broken release")/' "$BAD/agentlink/serve.py"
git -C "$BAD" -c user.email=v@local -c user.name=verify commit -qam "broken"
if AO_SRC=$BAD "$D/update.sh" HEAD > "$S/bad.log" 2>&1; then fail "broken update should fail"; fi
{ [ "$(readlink -f "$AO_HOME/current")" = "$R1" ] && curl -fs "$AO_URL/api/health?deep=1" >/dev/null; } || fail "broken release -> automatic rollback, service healthy"; ok "broken release -> automatic rollback, service healthy"

echo "== $PASS checks passed on $(uname -sm), systemd $(systemctl --version | head -1 | awk '{print $2}'), $(python3 --version)"
