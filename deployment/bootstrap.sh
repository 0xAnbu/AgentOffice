#!/usr/bin/env bash
# AgentOffice automated deployment — single entry point.
#
#   sudo deployment/bootstrap.sh [--config FILE] [--secrets-file FILE] [--non-interactive]
#   sudo deployment/bootstrap.sh --check | --dry-run | --verify
#   deployment/bootstrap.sh --help
#
# Runs, in order and idempotently: precheck → system packages → Python ≥ 3.12 → install.sh → Tailscale → private
# HTTPS (Tailscale Serve) → [GitHub webhook] → Healthchecks.io → systemd units/backups → live verification report.
# Re-running detects what is already done and only does what is missing. The logic lives in provision.py (standard
# library only); this wrapper only makes sure a python3 >= 3.11 exists to run it. Secrets are never read from the
# command line: they come from the --secrets-file (mode 0600) or AO_* environment variables (see --help).
#
# Internal use: the first argument may name a provision.py sub-command (python, tailscale, monitor, verify, wizard,
# detect-os); the provision-*.sh, preflight.sh, verify-live.sh and deploy.sh wrappers use that.
set -euo pipefail   # never `set -x`: this process handles credentials
HERE=$(dirname "$(readlink -f "$0")")

CMD=bootstrap
case "${1:-}" in python|tailscale|monitor|verify|wizard|detect-os|bootstrap) CMD=$1; shift;; esac

usable() { "$1" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3; do
  p=$(command -v "$c" 2>/dev/null) || continue
  if usable "$p"; then PY=$p; break; fi
done

if [ -z "$PY" ]; then
  apply=1
  for a in "$@"; do case "$a" in --check|--dry-run|--verify|--help|-h) apply=0;; esac; done
  if [ "$apply" = 1 ] && [ "$(id -u)" = 0 ] && command -v apt-get >/dev/null; then
    echo "[agent-office] python3 >= 3.11 not found: installing the distribution python3" >&2
    DEBIAN_FRONTEND=noninteractive apt-get update -q >&2
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends python3 >&2
    PY=$(command -v python3)
    usable "$PY" || { echo "[agent-office] ERROR: the distribution python3 is older than 3.11 (unsupported OS)" >&2; exit 1; }
  else
    echo "[agent-office] ERROR: python3 >= 3.11 is required to run the bootstrap (sudo apt-get install python3)" >&2
    exit 1
  fi
fi
exec "$PY" -I "$HERE/provision.py" "$CMD" "$@"
