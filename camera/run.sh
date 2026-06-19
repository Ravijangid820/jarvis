#!/usr/bin/env bash
# Run the camera agent in the FOREGROUND (testing) — no service, nothing persistent. Ctrl-C stops it.
# Linux / macOS / Raspberry Pi. (Run setup.sh first.) Extra args pass through to the agent.
#
#   bash run.sh              # live: posts events to the server
#   bash run.sh --dry-run    # logs events locally, sends nothing
set -euo pipefail
CAMERA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$CAMERA/.venv/bin/python"
[ -x "$PY" ] || { echo "No venv — run:  bash setup.sh"; exit 1; }
cd "$CAMERA"
exec "$PY" -m jarvis_camera.agent "$@"
