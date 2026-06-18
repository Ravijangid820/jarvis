#!/usr/bin/env bash
# Run llama-fast as the non-root `jarvis` user (audit F3, follow-up). The llama.cpp build lives in
# /root (0700, unreadable to non-root), so this copies it to /opt/llama.cpp and installs the
# non-root unit. Idempotent; copies (leaves the /root build intact). Run as root:
#
#   sudo bash src/scripts/harden_llama.sh
#
# Rollback: restore the previous unit from git and restart:
#   git -C /srv/jarvis show HEAD~1:systemd/llama-fast.service > /etc/systemd/system/llama-fast.service
#   systemctl daemon-reload && systemctl restart llama-fast
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
USER_NAME=jarvis
SRC=/root/llama.cpp
DEST=/opt/llama.cpp
say() { printf '\033[1;36m▸ %s\033[0m\n' "$1"; }

[ "$(id -u)" -eq 0 ] || { echo "must run as root"; exit 1; }
id "$USER_NAME" >/dev/null 2>&1 || { echo "user '$USER_NAME' missing — run harden_service.sh first"; exit 1; }

say "1/4  copy llama.cpp build to $DEST (readable by the non-root user)"
if [ ! -x "$DEST/build/bin/llama-server" ]; then
  [ -x "$SRC/build/bin/llama-server" ] || { echo "  no build at $SRC — build it first (build_native.sh)"; exit 1; }
  rm -rf "$DEST"; cp -a "$SRC" "$DEST"
else
  echo "  already present"
fi
chown -R root:root "$DEST"
chmod -R a+rX "$DEST"          # root-owned; world-readable/traversable so the service user can load it

say "2/4  ensure the GGUF is readable by $USER_NAME"
chmod a+r /srv/jarvis/models/qwen3.5_2b/*.gguf 2>/dev/null || true

say "3/4  install the non-root unit + restart"
cp "$REPO/systemd/llama-fast.service" /etc/systemd/system/llama-fast.service
systemctl daemon-reload
systemctl restart llama-fast

say "4/4  verify (model load can take ~20s)"
ok=""
for _ in $(seq 1 30); do
  curl -fsS --max-time 3 http://127.0.0.1:8081/health >/dev/null 2>&1 && { ok=1; break; }
  sleep 2
done
if [ -n "$ok" ]; then
  echo "  ✓ llama-server healthy on 127.0.0.1:8081, running as '$USER_NAME'"
else
  echo "  ✗ health FAILED. Inspect:  journalctl -u llama-fast -n 50 --no-pager"
  exit 1
fi
