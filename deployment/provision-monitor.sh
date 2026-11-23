#!/usr/bin/env bash
# Create or reuse the Healthchecks.io check (Management API v3) and store only its ping URL (HC_PING_URL) in the
# protected env file. The API key is used for this run only and never stored.
#   sudo deployment/provision-monitor.sh [--config FILE] [--secrets-file FILE] [--dry-run]
set -euo pipefail
exec "$(dirname "$(readlink -f "$0")")/bootstrap.sh" monitor "$@"
