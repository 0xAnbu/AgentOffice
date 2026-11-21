#!/usr/bin/env bash
# Update or roll back. The database is backed up BEFORE switching code; if the new release is not healthy within
# 40 s it rolls back automatically (and restores that backup if the new code had migrated the schema).
#   sudo deployment/update.sh [git-ref]     default: fetch and deploy origin/main of the install checkout
#   sudo deployment/update.sh --rollback    back to the previous release (restores the pre-update backup if needed)
set -euo pipefail
HERE=$(dirname "$(readlink -f "$0")")
. "$HERE/lib.sh"
[ "$AO_USER_MODE" = 1 ] || [ "$(id -u)" = 0 ] || die "run as root (sudo)"
STATE="$AO_HOME/last-update.env"
DB=$(db_path)

rollback() {  # $1 = release to go back to, $2 = pre-update backup, $3 = schema before the update
  local to=$1 bk=$2 before=$3 now
  log "rolling back to $(basename "$to")"
  switch_to "$to"
  now=$(schema_of "$DB")
  if [ "$now" != "$before" ]; then
    log "schema changed v$before -> v$now: restoring the pre-update backup $bk (writes made after the update are lost)"
    "$HERE/restore.sh" "$bk" --yes
  else
    sctl restart agent-office.service
    wait_healthy 40 || die "rollback release is not healthy either; inspect: journalctl -u agent-office"
  fi
  log "rolled back to $(basename "$to")"
}

if [ "${1:-}" = "--rollback" ]; then
  [ -f "$STATE" ] || die "no previous update recorded"
  # shellcheck disable=SC1090
  . "$STATE"
  rollback "$PREVIOUS" "$PRE_BACKUP" "$SCHEMA_BEFORE"
  exit 0
fi

SRC=$(sed -n 's/^AO_SRC=//p' "$AO_ETC/deploy.conf" 2>/dev/null || true)
SRC=${AO_SRC:-${SRC:-$(readlink -f "$HERE/..")}}
REF=${1:-}
if [ -z "$REF" ]; then
  git -C "$SRC" fetch --quiet origin
  REF=origin/main
fi
PREVIOUS=$(readlink -f "$AO_HOME/current")
NEW=$(build_release "$SRC" "$REF")
PRE_BACKUP=$("$HERE/backup.sh" pre-update)
SCHEMA_BEFORE=$(schema_of "$DB")
printf 'PREVIOUS=%q\nPRE_BACKUP=%q\nSCHEMA_BEFORE=%q\nNEW=%q\n' "$PREVIOUS" "$PRE_BACKUP" "$SCHEMA_BEFORE" "$NEW" > "$STATE"
switch_to "$NEW"
# Units may have changed with the release.
for u in agent-office.service agent-office-healthcheck.service agent-office-healthcheck.timer \
         agent-office-backup.service agent-office-backup.timer; do
  render_unit "$NEW/deployment/$u" "$AO_UNIT_DIR/$u"
done
sctl daemon-reload
sctl restart agent-office.service
if wait_healthy 40; then
  prune_releases
  log "updated to $(basename "$NEW") (rollback: deployment/update.sh --rollback)"
else
  log "new release is unhealthy"
  rollback "$PREVIOUS" "$PRE_BACKUP" "$SCHEMA_BEFORE"
  exit 1
fi
