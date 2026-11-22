#!/usr/bin/env bash
# Consistent backup of the live database while the service runs (SQLite online backup API, never a raw file copy:
# with WAL the main file alone can be missing committed transactions). Verifies the copy, compresses it, writes a
# sha256 and keeps the newest $AO_BACKUP_KEEP backups.
#   deployment/backup.sh [label]      -> prints the backup path
set -euo pipefail
. "$(dirname "$(readlink -f "$0")")/lib.sh"
[ -r "$AO_ENV_FILE" ] && AO_BACKUP_DIR=$(sed -n 's/^AO_BACKUP_DIR=//p' "$AO_ENV_FILE" | tail -1 | grep . || echo "$AO_BACKUP_DIR")
KEEP=${AO_BACKUP_KEEP:-14}
SRC=$(db_path)
[ -f "$SRC" ] || die "database not found: $SRC"
mkdir -p "$AO_BACKUP_DIR"
chmod 700 "$AO_BACKUP_DIR"
NAME="agent-office-$(date -u +%Y%m%dT%H%M%SZ)${1:+-$1}.db"
TMP="$AO_BACKUP_DIR/.$NAME.partial"
trap 'rm -f "$TMP"' EXIT

"$PYTHON" - "$SRC" "$TMP" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1], timeout=30)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)                        # one step: a consistent snapshot even with concurrent writers (WAL)
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
if ok != "ok":
    sys.exit(f"backup integrity check failed: {ok}")
dst.execute("PRAGMA journal_mode=DELETE")  # self-contained single file
ver = dst.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
n = dst.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
dst.close()
print(f"verified: integrity ok, schema v{ver}, {n} messages", file=sys.stderr)
PY
gzip -9 -c "$TMP" > "$AO_BACKUP_DIR/$NAME.gz.partial"
mv "$AO_BACKUP_DIR/$NAME.gz.partial" "$AO_BACKUP_DIR/$NAME.gz"
(cd "$AO_BACKUP_DIR" && sha256sum "$NAME.gz" > "$NAME.gz.sha256")
chmod 600 "$AO_BACKUP_DIR/$NAME.gz" "$AO_BACKUP_DIR/$NAME.gz.sha256"
ls -1t "$AO_BACKUP_DIR"/agent-office-*.db.gz | tail -n +"$((KEEP + 1))" | while read -r old; do rm -f "$old" "$old.sha256"; done
log "backup written: $AO_BACKUP_DIR/$NAME.gz"
printf '%s\n' "$AO_BACKUP_DIR/$NAME.gz"
