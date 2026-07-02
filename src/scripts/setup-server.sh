#!/usr/bin/env bash
# ONE-COMMAND server setup for a fresh box: bootstrap + systemd services + HTTPS, in the right order.
# Run as root (it installs systemd units and may create the service user):
#
#   sudo bash src/scripts/setup-server.sh
#
# Options (env):
#   JARVIS_USER=jarvis   service user (default jarvis; use `root` for the simple non-hardened mode)
#   SKIP_TLS=1           don't generate/enable HTTPS (leave it on plain HTTP)
#   ADMIN_USER, ADMIN_PASS, LLM_GGUF_URL, HF_TOKEN, SKIP_NATIVE, SKIP_MODELS  → passed to setup.sh
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
[ "$(id -u)" = 0 ] || { echo "Run as root (installs systemd units):  sudo bash src/scripts/setup-server.sh"; exit 1; }
SVC_USER="${JARVIS_USER:-jarvis}"
step() { printf '\n\033[1;36m▸ %s\033[0m\n' "$1"; }

step "1/4  Bootstrap (env, config, frontend, DB, admin, native build, models)"
# SKIP_RUN: bootstrap only — this installer starts the services as a systemd unit below, not inline.
SKIP_RUN=1 bash src/scripts/setup.sh

# The remaining steps install + start systemd units. On a box without systemd (a Codespace, most
# containers), that can't work — so stop cleanly after the bootstrap and point at the run script,
# instead of failing confusingly at systemctl.
if ! { command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; }; then
  step "No systemd here (Codespace / container) — bootstrap done; skipping the service install."
  cat <<EOF

Run Jarvis (both services, Ctrl-C to stop):
  bash src/scripts/run.sh

Login is admin/admin by default. Then open http://localhost:5000
EOF
  exit 0
fi

step "2/4  Install + start systemd services (service user: $SVC_USER)"
# install_services creates the user, relocates the llama build, chowns the writable data dirs
# (.venv/.cache/memory/logs/config) to the service user, and enables + starts both units (HTTP).
JARVIS_USER="$SVC_USER" bash src/scripts/install_services.sh
# read-only assets the (possibly non-root) service must read:
chmod -R a+rX "$REPO/models" "$REPO/whisper" "$REPO/piper" 2>/dev/null || true

if [ "${SKIP_TLS:-}" = 1 ]; then
  step "3/4  TLS skipped (SKIP_TLS=1) — serving plain HTTP on :5000"
  echo "Done. Health:  curl http://127.0.0.1:5000/health"
  exit 0
fi

step "3/4  Generate the local CA + server cert (note the printed fingerprint)"
TLS_SERVICE_USER="$SVC_USER" bash src/scripts/setup_tls.sh     # user now exists → server.key is readable

step "4/4  Switch the orchestrator to HTTPS"
install -d /etc/systemd/system/jarvis-orchestrator.service.d
install -m644 systemd/jarvis-orchestrator.service.d/tls.conf /etc/systemd/system/jarvis-orchestrator.service.d/tls.conf
systemctl daemon-reload && systemctl restart jarvis-orchestrator

echo
echo "Done. Verify:   curl --cacert tls/ca.crt https://127.0.0.1:5000/health"
echo "Next:"
echo "  • Mint a camera key:  uv run python src/scripts/manage.py mint-key <non-admin-user> laptop-cam laptop-cam"
echo "  • Trust on devices:   copy  tls/ca.crt  to the device (or download https://<ip>:5000/ca.crt)"
