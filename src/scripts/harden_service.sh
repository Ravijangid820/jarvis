#!/usr/bin/env bash
# Migrate the orchestrator to run as a dedicated NON-ROOT user (audit finding F3).
#
# Idempotent and conservative: it COPIES the HuggingFace cache (leaves the /root copy intact)
# and only changes ownership + the installed unit. Run as root on the box:
#
#   sudo bash src/scripts/harden_service.sh
#
# Rollback (back to the root unit) if anything misbehaves:
#   cp /srv/jarvis/systemd/jarvis-orchestrator.service /etc/systemd/system/ \
#     && systemctl daemon-reload && systemctl restart jarvis-orchestrator
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
USER_NAME=jarvis
say() { printf '\033[1;36m▸ %s\033[0m\n' "$1"; }

[ "$(id -u)" -eq 0 ] || { echo "must run as root"; exit 1; }

say "1/6  create system user '$USER_NAME'"
if ! id "$USER_NAME" >/dev/null 2>&1; then
  useradd --system --home-dir "$REPO" --shell /usr/sbin/nologin "$USER_NAME"
else
  echo "  already exists"
fi

say "2/6  make uv available system-wide (/usr/local/bin/uv)"
if [ ! -x /usr/local/bin/uv ]; then
  UV_BIN="$(command -v uv || echo /root/.local/bin/uv)"
  [ -x "$UV_BIN" ] || { echo "  uv not found ($UV_BIN); install uv first"; exit 1; }
  cp "$UV_BIN" /usr/local/bin/uv
else
  echo "  already present"
fi

say "3/6  copy the HuggingFace model cache under the owned tree"
if [ ! -d "$REPO/.cache/huggingface" ] && [ -d /root/.cache/huggingface ]; then
  mkdir -p "$REPO/.cache"
  cp -a /root/.cache/huggingface "$REPO/.cache/huggingface"   # copy, not move (safe fallback)
  echo "  copied (~1.2G)"
else
  echo "  already in place or no /root cache"
fi

say "4/6  chown the tree to $USER_NAME"
chown -R "$USER_NAME:$USER_NAME" "$REPO"

say "5/6  install the hardened unit"
cp "$REPO/systemd/jarvis-orchestrator.hardened.service" /etc/systemd/system/jarvis-orchestrator.service
systemctl daemon-reload
systemctl restart jarvis-orchestrator

say "6/6  verify"
sleep 3
if curl -fsS http://localhost:5000/health >/dev/null 2>&1; then
  echo "  ✓ /health OK — orchestrator is running as '$USER_NAME'"
else
  echo "  ✗ health check FAILED. Inspect:  journalctl -u jarvis-orchestrator -n 50 --no-pager"
  echo "    Rollback:  cp $REPO/systemd/jarvis-orchestrator.service /etc/systemd/system/ && systemctl daemon-reload && systemctl restart jarvis-orchestrator"
  exit 1
fi
echo
echo "Note: llama-fast.service still runs from /root as root — it only serves the local GGUF on"
echo "loopback, so it's lower risk; hardening it similarly is a follow-up."
