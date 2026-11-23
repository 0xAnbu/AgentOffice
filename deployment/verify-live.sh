#!/usr/bin/env bash
# Verify the installed server end to end (PASS / FAIL / NOT_VERIFIED) and print the AGENTOFFICE DEPLOYMENT REPORT.
# Changes nothing. Exit 0 only when the result is SUCCESS.   sudo deployment/verify-live.sh [--config FILE]
set -euo pipefail
exec "$(dirname "$(readlink -f "$0")")/bootstrap.sh" verify --non-interactive "$@"
