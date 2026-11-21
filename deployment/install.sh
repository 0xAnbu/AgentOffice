#!/usr/bin/env bash
# First install on a 64-bit Linux host (Raspberry Pi OS 64-bit, Debian, Ubuntu...). Idempotent: re-running keeps the
# existing database and secrets. Run from a checkout of the repository:
#   sudo deployment/install.sh [git-ref]          (default ref: HEAD of this checkout)
set -euo pipefail
HERE=$(dirname "$(readlink -f "$0")")
. "$HERE/lib.sh"
SRC=$(readlink -f "$HERE/..")
REF=${1:-HEAD}

# -- requirements (checked, not assumed) --------------------------------------------------------------------------
[ "$(uname -s)" = Linux ] || die "Linux required"
case "$(uname -m)" in aarch64|arm64|x86_64) ;; *) log "WARNING: untested architecture $(uname -m) (64-bit expected)";; esac
"$PYTHON" - <<'PY' || die "Python >= 3.12 with sqlite3 >= 3.35 and venv is required (apt install python3 python3-venv)"
import sqlite3, sys, venv  # noqa: F401
assert sys.version_info >= (3, 12), sys.version
assert sqlite3.sqlite_version_info >= (3, 35), sqlite3.sqlite_version
PY
command -v curl >/dev/null || die "curl is required (apt install curl)"
if [ "$AO_USER_MODE" != 1 ]; then
  [ "$(id -u)" = 0 ] || die "run as root (sudo)"
  id "$AO_USER" >/dev/null 2>&1 || useradd --system --home-dir "$AO_DATA" --shell /usr/sbin/nologin "$AO_USER"
fi

# -- directories ----------------------------------------------------------------------------------------------------
mkdir -p "$AO_HOME/releases" "$AO_DATA" "$AO_ETC" "$AO_BACKUP_DIR"
chmod 750 "$AO_DATA" "$AO_ETC"
chmod 700 "$AO_BACKUP_DIR"
if [ "$AO_USER_MODE" != 1 ]; then
  chown "$AO_USER:$AO_USER" "$AO_DATA" "$AO_BACKUP_DIR"
  chown "root:$AO_USER" "$AO_ETC"
fi

# -- secrets: created once, never overwritten -------------------------------------------------------------------------
if [ ! -f "$AO_ENV_FILE" ]; then
  TOKEN="al_$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  umask 027
  sed -e "s#^AGENTLINK_ADMIN_TOKEN=.*#AGENTLINK_ADMIN_TOKEN=$TOKEN#" \
      -e "s#/var/lib/agent-office#$AO_DATA#" -e "s#/var/backups/agent-office#$AO_BACKUP_DIR#" \
      -e "s#^AO_URL=.*#AO_URL=$AO_URL#" "$HERE/agent-office.env.example" > "$AO_ENV_FILE"
  [ "$AO_USER_MODE" = 1 ] || chown "root:$AO_USER" "$AO_ENV_FILE"
  chmod 640 "$AO_ENV_FILE"
  printf '\n  Admin token (shown once, also in %s):\n\n    %s\n\n' "$AO_ENV_FILE" "$TOKEN"
fi
printf 'AO_SRC=%s\n' "$SRC" > "$AO_ETC/deploy.conf"

# -- code -----------------------------------------------------------------------------------------------------------
REL=$(build_release "$SRC" "$REF")
switch_to "$REL"

# -- units ----------------------------------------------------------------------------------------------------------
mkdir -p "$AO_UNIT_DIR"
for u in agent-office.service agent-office-healthcheck.service agent-office-healthcheck.timer \
         agent-office-backup.service agent-office-backup.timer; do
  render_unit "$HERE/$u" "$AO_UNIT_DIR/$u"
done
sctl daemon-reload
sctl enable --now agent-office.service agent-office-healthcheck.timer agent-office-backup.timer
sctl restart agent-office.service
wait_healthy 40 || die "service did not become healthy; see: journalctl -u agent-office -n 50"
log "installed $(basename "$REL"); listening on $AO_URL (loopback only)"
log "next: Tailscale Serve (docs/DEPLOYMENT.md §Tailscale) and HC_PING_URL in $AO_ENV_FILE"
