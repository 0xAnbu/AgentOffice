#!/usr/bin/env bash
# Apply the deployment described by the configuration (default: ./deployment.toml, then
# /etc/agent-office/deployment.toml as written by `install.sh --wizard`). Same options as bootstrap.sh.
set -euo pipefail
exec "$(dirname "$(readlink -f "$0")")/bootstrap.sh" bootstrap "$@"
