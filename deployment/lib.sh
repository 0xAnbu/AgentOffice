# Shared helpers for the deployment scripts. Sourced, never executed.
# Every path can be overridden (that is how deployment/verify.sh exercises the real scripts in a sandbox with
# `systemd --user`), but the defaults are the production layout:
#   /opt/agent-office/releases/<id>/   code + its own .venv (one per release, for rollback)
#   /opt/agent-office/current          symlink to the live release
#   /var/lib/agent-office/             SQLite database (WAL) — the only state
#   /etc/agent-office/agent-office.env secrets + configuration (0640 root:agent-office)
#   /var/backups/agent-office/         consistent backups (SQLite online backup API)
set -euo pipefail

AO_HOME=${AO_HOME:-/opt/agent-office}
AO_DATA=${AO_DATA:-/var/lib/agent-office}
AO_ETC=${AO_ETC:-/etc/agent-office}
AO_BACKUP_DIR=${AO_BACKUP_DIR:-/var/backups/agent-office}
AO_UNIT_DIR=${AO_UNIT_DIR:-/etc/systemd/system}
AO_USER=${AO_USER:-agent-office}
AO_PORT=${AO_PORT:-8080}
AO_URL=${AO_URL:-http://127.0.0.1:$AO_PORT}
AO_USER_MODE=${AO_USER_MODE:-0}          # 1 = units under `systemctl --user` (verification sandbox only)
AO_KEEP_RELEASES=${AO_KEEP_RELEASES:-5}
AO_ENV_FILE=${AO_ENV_FILE:-$AO_ETC/agent-office.env}
PYTHON=${PYTHON:-python3}

log() { printf '[agent-office] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

sctl() { if [ "$AO_USER_MODE" = 1 ]; then systemctl --user "$@"; else systemctl "$@"; fi; }

# The database path, as the backend sees it.
db_path() {
  local p=""
  [ -r "$AO_ENV_FILE" ] && p=$(sed -n 's/^AGENTLINK_DB_PATH=//p' "$AO_ENV_FILE" | tail -1)
  printf '%s\n' "${AGENTLINK_DB_PATH:-${p:-$AO_DATA/agent-office.db}}"
}

# Highest applied schema version of a SQLite file (0 if none).
schema_of() {
  "$PYTHON" - "$1" <<'PY'
import sqlite3, sys
try:
    c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    print(c.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0])
except sqlite3.Error:
    print(0)
PY
}

# Wait for /api/health?deep=1 to be ok (database read/write + reconciler alive).
wait_healthy() {
  local secs=${1:-40}
  for _ in $(seq "$secs"); do
    if curl -fsS -m 3 "$AO_URL/api/health?deep=1" 2>/dev/null | grep -q '"ok":true'; then return 0; fi
    sleep 1
  done
  return 1
}

# Copy a unit file into place, rewriting the default paths when they were overridden.
render_unit() {
  local src=$1 dst=$2
  sed -e "s#/opt/agent-office#$AO_HOME#g" -e "s#/var/lib/agent-office#$AO_DATA#g" \
      -e "s#/etc/agent-office#$AO_ETC#g" -e "s#/var/backups/agent-office#$AO_BACKUP_DIR#g" \
      -e "s#--port 8080#--port $AO_PORT#" "$src" > "$dst.tmp"
  if [ "$AO_USER_MODE" = 1 ]; then
    # A user manager cannot switch users or build mount namespaces: drop those lines (sandbox verification only).
    sed -i -E '/^(User|Group|ProtectSystem|ProtectHome|ReadWritePaths|PrivateTmp|ProtectKernel[A-Za-z]*|ProtectControlGroups|RestrictSUIDSGID|RestrictAddressFamilies)=/d' "$dst.tmp"
    sed -i 's/^WantedBy=multi-user.target/WantedBy=default.target/' "$dst.tmp"
  fi
  mv "$dst.tmp" "$dst"
}

# Build a release directory from a git checkout + ref (or a plain directory), with its own virtualenv.
build_release() {
  local src=$1 ref=$2 id rel
  id="$(date -u +%Y%m%dT%H%M%SZ)"
  if git -C "$src" rev-parse --git-dir >/dev/null 2>&1; then
    id="$id-$(git -C "$src" rev-parse --short "$ref")"
  fi
  rel="$AO_HOME/releases/$id"
  mkdir -p "$rel"
  if git -C "$src" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$src" archive "$ref" | tar -x -C "$rel"
  else
    (cd "$src" && tar --exclude=.venv --exclude=.git --exclude=data -cf - .) | tar -x -C "$rel"
  fi
  log "release $id: creating virtualenv"
  "$PYTHON" -m venv "$rel/.venv"
  "$rel/.venv/bin/pip" install -q --disable-pip-version-check -r "$rel/requirements.txt"
  # Import check before anything is switched: a broken release never becomes `current`.
  (cd / && PYTHONPATH="$rel" "$rel/.venv/bin/python" -c "import agentlink.app, agentlink.serve") || { rm -rf "$rel"; die "release $id does not import"; }
  printf '%s\n' "$rel"
}

switch_to() {  # atomic symlink swap
  ln -sfn "$1" "$AO_HOME/current.new"
  mv -T "$AO_HOME/current.new" "$AO_HOME/current"
}

prune_releases() {
  local cur keep=$AO_KEEP_RELEASES
  cur=$(readlink -f "$AO_HOME/current" || true)
  ls -1dt "$AO_HOME"/releases/*/ 2>/dev/null | tail -n +"$((keep + 1))" | while read -r d; do
    [ "$(readlink -f "$d")" = "$cur" ] || rm -rf "$d"
  done
}
