#!/usr/bin/env bash
# Check requirements, configuration and credentials without changing anything (= bootstrap.sh --check).
#   sudo deployment/preflight.sh [--config FILE] [--secrets-file FILE]
set -euo pipefail
exec "$(dirname "$(readlink -f "$0")")/bootstrap.sh" bootstrap --check "$@"
