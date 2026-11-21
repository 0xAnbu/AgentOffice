#!/usr/bin/env bash
# Restore a backup made by backup.sh. Verifies checksum + integrity + schema compatibility BEFORE stopping anything,
# keeps the replaced database (and its -wal/-shm) aside, then restarts and waits for the deep health check.
#   deployment/restore.sh /var/backups/agent-office/agent-office-<ts>.db.gz [--yes]
set -euo pipefail
. "$(dirname "$(readlink -f "$0")")/lib.sh"
BK=${1:?usage: restore.sh <backup.db.gz> [--yes]}
[ -f "$BK" ] || die "no such backup: $BK"
if [ -f "$BK.sha256" ]; then (cd "$(dirname "$BK")" && sha256sum -c --quiet "$(basename "$BK").sha256") || die "checksum mismatch"; fi
DB=$(db_path)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
gzip -dc "$BK" > "$WORK/restore.db"
CODE_PY="$AO_HOME/current/.venv/bin/python"
[ -x "$CODE_PY" ] || CODE_PY=$PYTHON
"$CODE_PY" - "$WORK/restore.db" "$AO_HOME/current" <<'PY'
import sqlite3, sys
sys.path.insert(0, sys.argv[2])
c = sqlite3.connect(sys.argv[1])
ok = c.execute("PRAGMA integrity_check").fetchone()[0]
if ok != "ok":
    sys.exit(f"backup is corrupt: {ok}")
ver = c.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
try:
    from agentlink.db import SCHEMA_VERSION
except Exception:
    SCHEMA_VERSION = ver
if ver > SCHEMA_VERSION:
    sys.exit(f"backup schema v{ver} is newer than the installed code (v{SCHEMA_VERSION}); install matching code first")
print(f"backup ok: schema v{ver} (code v{SCHEMA_VERSION}); older schemas are migrated on start", file=sys.stderr)
PY
if [ "${2:-}" != "--yes" ]; then
  read -r -p "Replace $DB with $BK (the current database is kept aside)? [y/N] " a
  [ "$a" = y ] || die "aborted"
fi
sctl stop agent-office.service || true
ASIDE="$(dirname "$DB")/replaced-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$ASIDE"
for f in "$DB" "$DB-wal" "$DB-shm"; do [ -e "$f" ] && mv "$f" "$ASIDE/"; done
install -m 600 "$WORK/restore.db" "$DB"
if [ "$(id -u)" = 0 ] && id "$AO_USER" >/dev/null 2>&1; then chown -R "$AO_USER:$AO_USER" "$(dirname "$DB")"; fi
sctl start agent-office.service
wait_healthy 40 || die "service unhealthy after restore; previous database kept in $ASIDE"
log "restored $BK; previous database kept in $ASIDE"
