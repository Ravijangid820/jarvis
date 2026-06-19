#!/usr/bin/env bash
# Make the camera agent PERSISTENT on Linux / Raspberry Pi via a systemd **user** service
# (runs as you — never root — and restarts on failure / at login). Run setup.sh first.
#
#   bash service.sh install     # install + start, enable at login
#   bash service.sh uninstall   # stop + remove
#   bash service.sh status      # is it running?
#
# Boot WITHOUT logging in (e.g. a headless Pi):  sudo loginctl enable-linger "$USER"
set -euo pipefail
CAMERA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$CAMERA/.venv/bin/python"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/jarvis-camera.service"

case "${1:-}" in
  install)
    [ -x "$PY" ] || { echo "No venv — run:  bash setup.sh"; exit 1; }
    command -v systemctl >/dev/null || { echo "systemd not found — use run.sh, or your platform's init."; exit 1; }
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT" <<EOF
[Unit]
Description=Jarvis camera agent (on-device vision)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$CAMERA
ExecStart=$PY -m jarvis_camera.agent
Restart=on-failure
RestartSec=5
# Hardening — least privilege (this is a *user* unit, so it never runs as root):
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$CAMERA
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6
LockPersonality=true

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now jarvis-camera.service
    echo "Installed + started (systemd user service, runs as $USER)."
    echo "  logs:   journalctl --user -u jarvis-camera -f"
    echo "  on Pi, to run before you log in:  sudo loginctl enable-linger $USER"
    ;;
  uninstall)
    systemctl --user disable --now jarvis-camera.service 2>/dev/null || true
    rm -f "$UNIT"
    systemctl --user daemon-reload
    echo "Removed."
    ;;
  status)
    systemctl --user status jarvis-camera.service --no-pager || true
    ;;
  *)
    echo "usage: bash service.sh {install|uninstall|status}"; exit 1 ;;
esac
