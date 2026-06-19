#!/usr/bin/env bash
# One-click install (Linux / macOS / Pi): setup (deps + verified model download) then install the
# persistent service. Chains setup.sh + service.sh — read those to see exactly what runs.
#
#   bash install.sh              # camera + faces
#   bash install.sh --with-pose  # also pose + gestures
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bash "$DIR/setup.sh" "$@"          # passes --with-pose through if given
bash "$DIR/service.sh" install

echo
echo "Installed. The camera agent runs as a systemd user service."
echo "  Save your DEVICE key to  $DIR/config/agent.key  so it can recognize people."
echo "  Manage:  bash service.sh status | uninstall"
