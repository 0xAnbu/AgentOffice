#!/usr/bin/env bash
# Find or provision a Python >= 3.12 (with venv and SQLite >= 3.35) and print its path. Ubuntu 24.04+/Debian 13: the
# distribution python3; Debian 12: a uv-managed CPython under /opt/agent-office/python (uv pinned by SHA-256). The
# system Python is never replaced.   sudo deployment/provision-python.sh [--config FILE] [--dry-run]
set -euo pipefail
exec "$(dirname "$(readlink -f "$0")")/bootstrap.sh" python "$@"
