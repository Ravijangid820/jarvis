#!/usr/bin/env bash
# Bootstrap the Jarvis edge agent. Run THIS ON THE RASPBERRY PI (not the server).
set -euo pipefail
EDGE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cyan() { printf '\033[1;36m▸ %s\033[0m\n' "$1"; }

cyan "OS / RAM check"
if [ "$(getconf LONG_BIT)" = "64" ]; then
  echo "  64-bit OS ✓ (MediaPipe-capable)"
else
  echo "  ! 32-bit OS — MediaPipe (pose/gestures) needs 64-bit Pi OS; motion + faces still work."
fi
mem_mb=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
[ "$mem_mb" -lt 1500 ] && echo "  ! ~${mem_mb} MB RAM — add a swapfile (1–2 GB) before enabling heavy detectors."

cyan "System packages (OpenCV + picamera2 via apt — lighter than building pip wheels on a Pi)"
sudo apt-get update
sudo apt-get install -y python3-opencv python3-picamera2 libatlas-base-dev python3-venv

cyan "Python env via uv (--system-site-packages so apt's cv2/picamera2 are visible)"
command -v uv >/dev/null || { echo "  uv required — install it: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
uv venv --system-site-packages "$EDGE/.venv"
uv pip install --python "$EDGE/.venv/bin/python" -r "$EDGE/requirements.txt"

cyan "Config"
mkdir -p "$EDGE/config"
[ -f "$EDGE/config/config.json" ] || cp "$EDGE/config.example.json" "$EDGE/config/config.json"

cat <<EOF

Setup done. Next:
  1. On the SERVER, mint a device key:
       uv run python src/scripts/manage.py mint-key <user> pi-vision
     and save the printed key to:  $EDGE/config/edge.key
  2. Review $EDGE/config/config.json (server.url, camera, which detectors are enabled).
  3. Try it:  cd "$EDGE" && .venv/bin/python -m jarvis_edge.agent --dry-run
     (run via the venv's python so the sandboxed deps are used; drop --dry-run once the
      server /events endpoint is reachable and the key is in place)
EOF
