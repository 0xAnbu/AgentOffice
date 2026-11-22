#!/usr/bin/env bash
# End-to-end smoke test: real uvicorn server + CLI + MCP over HTTP. Exits non-zero on any failure.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-$PWD/.venv/bin/python}
WORK=$(mktemp -d)
PORT=${PORT:-$((20000 + RANDOM % 20000))}
export AGENTLINK_ADMIN_TOKEN="al_$($PY -c 'import secrets;print(secrets.token_urlsafe(32))')"
export AGENTLINK_DB_PATH="$WORK/smoke.db" AGENTLINK_STATE_DIR="$WORK/state" GITHUB_WEBHOOK_SECRET=smoke-secret
export AGENTLINK_URL="http://127.0.0.1:$PORT"

$PY -m agentlink.serve --port "$PORT" --env-file "$WORK/none.env" >"$WORK/server.log" 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true; rm -rf "$WORK"' EXIT
for _ in $(seq 50); do curl -fs "$AGENTLINK_URL/api/health" >/dev/null && break; sleep 0.2; done

cli() { $PY -m agentlink.cli "$@"; }
as() { local t=$1; shift; AGENTLINK_TOKEN=$t cli "$@"; }
step() { printf '\n== %s\n' "$*"; }
jget() { $PY -c "import json,sys; print(json.load(sys.stdin)$1)"; }
ADMIN=$AGENTLINK_ADMIN_TOKEN

step "health + auth"
curl -fs "$AGENTLINK_URL/api/health"; echo
code=$(curl -s -o /dev/null -w '%{http_code}' "$AGENTLINK_URL/api/me")
[ "$code" = 401 ] || { echo "expected 401 without token, got $code"; exit 1; }

step "admin creates agents and a channel"
ALICE=$(as "$ADMIN" agent-create alice | jget '["token"]')
BOB=$(as "$ADMIN" agent-create bob | jget '["token"]')
MON=$(as "$ADMIN" agent-create monitor --role monitor | jget '["token"]')
as "$ADMIN" channel-create dev --repo acme/app --members alice,bob >/dev/null

step "alice posts, bob long-polls his inbox"
( sleep 1; as "$ALICE" post dev "@bob can you take the login API?" --kind question >/dev/null ) &
POSTER=$!
OUT=$(as "$BOB" inbox --wait 10 --since-last)
echo "$OUT"
grep -q "@alice \[question\]" <<<"$OUT"
wait "$POSTER"

step "tasks: create, atomic claim, conflict"
TID=$(as "$ALICE" task-create dev "Login API" --priority 1 | jget '["id"]')
as "$BOB" claim "$TID" | jget '["assignee"]'
if as "$ALICE" claim "$TID" 2>/dev/null; then echo "second claim should fail"; exit 1; fi
as "$BOB" task-update "$TID" --status in_progress --note "starting" | jget '["status"]'

step "leases: bob locks src/auth/, alice is refused"
as "$BOB" lease dev src/auth/ --note "login API" | jget '["holder"]'
if as "$ALICE" lease dev src/auth/session.py 2>/dev/null; then echo "overlapping lease should fail"; exit 1; fi

step "shared context"
as "$ALICE" ctx-set dev decisions/auth '{"scheme":"jwt","ttl":"15m"}' | jget '["version"]'
as "$BOB" ctx-get dev decisions/auth | jget '["value"]'

step "monitor is read-only"
as "$MON" read dev --limit 3
if as "$MON" post dev "nope" 2>/dev/null; then echo "monitor write should fail"; exit 1; fi

step "MCP over HTTP"
curl -fs "$AGENTLINK_URL/mcp" -H "Authorization: Bearer $BOB" -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' | jget '["result"]["serverInfo"]'
curl -fs "$AGENTLINK_URL/mcp" -H "Authorization: Bearer $BOB" -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"read_messages","arguments":{"channel":"dev","limit":2}}}' \
  | jget '["result"]["content"][0]["text"]'

step "GitHub webhook (signed)"
BODY='{"ref":"refs/heads/main","after":"abc123","commits":[{}],"head_commit":{"message":"fix"},"repository":{"full_name":"acme/app"},"sender":{"login":"octo"}}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac smoke-secret | awk '{print $NF}')"
curl -fs "$AGENTLINK_URL/api/github/webhook" -H "x-github-event: push" -H "x-github-delivery: s1" \
  -H "x-hub-signature-256: $SIG" -d "$BODY"; echo

step "archive + verified prune"
AGENTLINK_TOKEN=$ADMIN cli archive --older-than-days 0 --out "$WORK/arch"
AGENTLINK_TOKEN=$ADMIN cli archive-prune "$WORK/arch"
ls -la "$WORK/arch"
as "$ADMIN" stats | jget '["archives"][0]["count"]'

step "release lease, finish task"
as "$BOB" release dev src/auth/ | jget '["released"]'
as "$BOB" task-update "$TID" --status done --note "merged" | jget '["status"]'

step "agent office: UI routes, deep health, session, snapshot, events, WebSocket, reconciler"
for path in / /chat /office/oficina.js /static/chat/app.js; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$AGENTLINK_URL$path"); [ "$code" = 200 ] || { echo "$path -> $code"; exit 1; }
done
curl -fs "$AGENTLINK_URL/api/health?deep=1" | jget '["reconciler"]'
api() { local t=$1 m=$2 u=$3; shift 3; curl -fs -X "$m" -H "Authorization: Bearer $t" -H 'content-type: application/json' "$@" "$AGENTLINK_URL$u"; }
SID=$(api "$BOB" POST /api/sessions -d '{"runtime":"generic","specialization":"backend"}' | jget '["id"]')
api "$BOB" POST "/api/sessions/$SID/heartbeat" -d '{"progress":true,"state":"executing"}' | jget '["status"]'
api "$MON" GET /api/office/snapshot | jget '["building"]["floors"][0]["key"]'
api "$MON" GET "/api/events?after=0&limit=5" | jget '["head"]'
TICKET=$(api "$MON" POST /api/ws-ticket | jget '["ticket"]')
$PY - "$AGENTLINK_URL" "$TICKET" <<'PYEOF'
import asyncio, json, sys
import websockets
async def main(url, ticket):
    async with websockets.connect(url.replace("http", "ws", 1) + f"/ws/events?ticket={ticket}&after=0") as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert hello["type"] == "hello", hello
        msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert msg["type"] in ("events", "resync"), msg
        print("ws ok:", hello["type"], msg["type"], len(msg.get("events", [])), "events")
    try:  # the ticket is single-use
        async with websockets.connect(url.replace("http", "ws", 1) + f"/ws/events?ticket={ticket}") as ws:
            await asyncio.wait_for(ws.recv(), 5)
        raise SystemExit("ticket reuse was accepted")
    except websockets.InvalidStatus as e:  # rejected before the upgrade
        print("ticket reuse refused: HTTP", e.response.status_code)
asyncio.run(main(*sys.argv[1:]))
PYEOF
api "$ADMIN" POST /api/admin/reconcile | $PY -c "import json,sys; print(sorted(json.load(sys.stdin))[:4])"
api "$BOB" POST "/api/sessions/$SID/end" -d '{"reason":"smoke done"}' | jget '["sessions_ended"]'

printf '\nSMOKE OK\n'
