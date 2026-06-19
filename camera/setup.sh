#!/usr/bin/env bash
# Bootstrap the Jarvis camera agent on Linux / macOS / Raspberry Pi (run on the device, not the
# server). Auto-detects the platform and installs deps accordingly, then downloads + verifies the
# face models. For Windows use setup.ps1.
#
#   bash setup.sh                # camera + faces (YuNet+SFace)
#   bash setup.sh --with-pose    # also install mediapipe (pose + hand gestures)
set -euo pipefail
CAMERA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cyan() { printf '\033[1;36m▸ %s\033[0m\n' "$1"; }

WITH_POSE=0
[ "${1:-}" = "--with-pose" ] && WITH_POSE=1

# ---- detect platform ----
OS="$(uname -s)"
if [ "$OS" = "Linux" ] && [ -f /proc/device-tree/model ] && grep -qi raspberry /proc/device-tree/model 2>/dev/null; then
  PLATFORM="pi"
elif [ "$OS" = "Darwin" ]; then
  PLATFORM="macos"
elif [ "$OS" = "Linux" ]; then
  PLATFORM="linux"
else
  echo "Unsupported OS '$OS' — on Windows use setup.ps1."; exit 1
fi
cyan "Platform: $PLATFORM"

command -v uv >/dev/null || { echo "  uv required — install it: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }

# ---- python env + deps (per platform) ----
if [ "$PLATFORM" = "pi" ]; then
  [ "$(getconf LONG_BIT)" = "64" ] || echo "  ! 32-bit OS — mediapipe (pose/gestures) needs 64-bit; faces + motion still work."
  cyan "Raspberry Pi: OpenCV + picamera2 via apt (lighter than building wheels), venv sees them"
  sudo apt-get update
  sudo apt-get install -y python3-opencv python3-picamera2 libatlas-base-dev python3-venv
  uv venv --system-site-packages "$CAMERA/.venv"
  PY="$CAMERA/.venv/bin/python"
  uv pip install --python "$PY" numpy requests          # opencv/picamera2 come from apt
else
  cyan "Desktop ($PLATFORM): sandboxed Python 3.12 venv + opencv via pip"
  uv venv --python 3.12 "$CAMERA/.venv"                  # 3.12 keeps the optional mediapipe path open
  PY="$CAMERA/.venv/bin/python"
  uv pip install --python "$PY" -r "$CAMERA/requirements-desktop.txt"
fi

if [ "$WITH_POSE" = 1 ]; then
  cyan "Optional pose/gestures: mediapipe"
  uv pip install --python "$PY" "mediapipe>=0.10,<0.11"
fi

# ---- face models: official OpenCV Zoo, sha256-verified ----
sha_of() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi; }
fetch_model() {  # name url sha256
  local out="$CAMERA/models/$1"
  if [ -f "$out" ] && [ "$(sha_of "$out")" = "$3" ]; then echo "  $1 ✓ (cached)"; return; fi
  echo "  downloading $1 ..."
  curl -fL --retry 3 -o "$out" "$2"
  if [ "$(sha_of "$out")" != "$3" ]; then
    rm -f "$out"; echo "  ✗ $1: SHA-256 mismatch — refusing (supply-chain check failed)"; exit 1
  fi
  echo "  $1 ✓ verified"
}
cyan "Face models (YuNet + SFace) — official OpenCV Zoo, sha256-verified"
mkdir -p "$CAMERA/models"
ZOO="https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models"
fetch_model face_detection_yunet_2023mar.onnx \
  "$ZOO/face_detection_yunet/face_detection_yunet_2023mar.onnx" \
  8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4
fetch_model face_recognition_sface_2021dec.onnx \
  "$ZOO/face_recognition_sface/face_recognition_sface_2021dec.onnx" \
  0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79

# ---- config ----
cyan "Config"
mkdir -p "$CAMERA/config"
[ -f "$CAMERA/config/config.json" ] || cp "$CAMERA/config.example.json" "$CAMERA/config/config.json"

cat <<EOF

Setup done ($PLATFORM). Next:
  1. On the SERVER, mint a DEVICE key (admin → Keys, set a Device ID like this camera's name; mint it
     under a NON-admin user) and save it to:  $CAMERA/config/agent.key   (chmod 600)
  2. Review $CAMERA/config/config.json (server.url, camera.device, which detectors are enabled).
  3. Try it (run via the venv's python):
       cd "$CAMERA" && .venv/bin/python -m jarvis_camera.facecli verify     # who's at the camera (local)
       cd "$CAMERA" && .venv/bin/python -m jarvis_camera.agent --dry-run    # events logged, not sent
  To enroll/delete faces you also need an ADMIN key in $CAMERA/config/admin.key (remove it when done).
EOF
