#!/usr/bin/env bash
# Install Tailscale (official apt repository), register this device with the auth key from the secrets file (only if
# it is not registered yet), and publish the backend privately with Tailscale Serve (HTTPS, tailnet only; never
# Funnel).   sudo deployment/provision-tailscale.sh [--config FILE] [--secrets-file FILE] [--dry-run]
set -euo pipefail
exec "$(dirname "$(readlink -f "$0")")/bootstrap.sh" tailscale "$@"
