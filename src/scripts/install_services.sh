#!/usr/bin/env bash
# Install Jarvis as systemd services — works from ANY checkout path. Run after setup.sh.
#
# Choose how the services run:
#   sudo bash src/scripts/install_services.sh                      # run as root (simplest)
#   sudo JARVIS_USER=jarvis bash src/scripts/install_services.sh   # dedicated non-root user (hardened)
#
# Optional env: JARVIS_HOST (default 0.0.0.0), JARVIS_PORT (5000), DRY_RUN=1 (write units to
# ./systemd/generated and skip user/chown/systemctl — preview only).
#
# It auto-detects the repo, uv, the llama-server binary, and the GGUF, then generates both unit
# files for the chosen mode. The non-root mode also creates the user, moves the model cache under
# the repo, narrows write access to the data dirs, and relocates a /root llama build to /opt.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SVC_USER="${JARVIS_USER:-root}"
HOST="${JARVIS_HOST:-0.0.0.0}"
PORT="${JARVIS_PORT:-5000}"
DRY="${DRY_RUN:-0}"
say() { printf '\033[1;36m▸ %s\033[0m\n' "$1"; }

if [ "$DRY" != 1 ] && [ "$(id -u)" -ne 0 ]; then
  echo "must run as root (installs systemd units). Use DRY_RUN=1 to preview without root."; exit 1
fi

# --- detect toolchain + artifacts -------------------------------------------
UV="$(command -v uv || true)"; [ -n "$UV" ] || UV=/root/.local/bin/uv
[ -x "$UV" ] || { echo "uv not found — install it first"; exit 1; }

LLAMA_BIN=""
for c in "$REPO/llama.cpp/build/bin/llama-server" /opt/llama.cpp/build/bin/llama-server /root/llama.cpp/build/bin/llama-server; do
  [ -x "$c" ] && { LLAMA_BIN="$c"; break; }
done
[ -n "$LLAMA_BIN" ] || { echo "llama-server not found — run build_native.sh first"; exit 1; }

if [ -n "${JARVIS_GGUF:-}" ]; then
  GGUF="$JARVIS_GGUF"
  [ -f "$GGUF" ] || { echo "JARVIS_GGUF=$GGUF not found"; exit 1; }
else
  mapfile -t _ggufs < <(find "$REPO/models" -name '*.gguf' 2>/dev/null)
  [ "${#_ggufs[@]}" -ge 1 ] || { echo "no GGUF under $REPO/models — run download_models.sh (or place one)"; exit 1; }
  GGUF="${_ggufs[0]}"
  [ "${#_ggufs[@]}" -gt 1 ] && echo "  ! multiple GGUFs found — using $GGUF; override with JARVIS_GGUF=<path>"
fi
say "repo=$REPO  user=$SVC_USER  uv=$UV"
say "llama=$LLAMA_BIN"
say "gguf=$GGUF"

# --- non-root preparation ----------------------------------------------------
if [ "$SVC_USER" != root ] && [ "$DRY" != 1 ]; then
  say "prepare non-root user '$SVC_USER'"
  id "$SVC_USER" >/dev/null 2>&1 || useradd --system --home-dir "$REPO" --shell /usr/sbin/nologin "$SVC_USER"
  [ -x /usr/local/bin/uv ] || cp "$UV" /usr/local/bin/uv
  UV=/usr/local/bin/uv
  mkdir -p "$REPO/.cache"
  if [ ! -d "$REPO/.cache/huggingface" ] && [ -d "$HOME/.cache/huggingface" ]; then
    cp -a "$HOME/.cache/huggingface" "$REPO/.cache/huggingface"
  fi
  # llama build under /root is unreadable to a non-root user — relocate to /opt.
  case "$LLAMA_BIN" in
    /root/*) [ -x /opt/llama.cpp/build/bin/llama-server ] || cp -a /root/llama.cpp /opt/llama.cpp
             chown -R root:root /opt/llama.cpp; chmod -R a+rX /opt/llama.cpp
             LLAMA_BIN=/opt/llama.cpp/build/bin/llama-server ;;
  esac
  chmod a+r "$GGUF" 2>/dev/null || true
  # ownership: writable data dirs → service user; source + .git stay root (read-only to the service)
  chown -R root:root "$REPO"
  for d in .venv .cache memory logs config; do
    [ -e "$REPO/$d" ] && chown -R "$SVC_USER:$SVC_USER" "$REPO/$d"
  done
fi
LLAMA_LIB="$(dirname "$LLAMA_BIN")"

# --- per-mode unit fragments -------------------------------------------------
if [ "$SVC_USER" = root ]; then
  ORCH_USER=""; ORCH_PROTECT="ProtectSystem=full"; ORCH_RW=""; ORCH_HOME=""; ORCH_PH=""
  LLAMA_USER=""; LLAMA_PROTECT="ProtectSystem=full"; LLAMA_PH=""; LLAMA_LD=""
else
  ORCH_USER="User=$SVC_USER
Group=$SVC_USER"
  ORCH_PROTECT="ProtectSystem=strict"
  ORCH_RW="ReadWritePaths=$REPO/memory $REPO/logs $REPO/.cache $REPO/.venv"
  ORCH_HOME="Environment=\"HOME=$REPO\"
Environment=\"HF_HOME=$REPO/.cache/huggingface\""
  ORCH_PH="ProtectHome=true"
  LLAMA_USER="User=$SVC_USER
Group=$SVC_USER"
  LLAMA_PROTECT="ProtectSystem=strict"
  LLAMA_PH="ProtectHome=true"
  LLAMA_LD="Environment=\"LD_LIBRARY_PATH=$LLAMA_LIB\""
fi

OUTDIR=/etc/systemd/system
[ "$DRY" = 1 ] && { OUTDIR="$REPO/systemd/generated"; mkdir -p "$OUTDIR"; }

say "write $OUTDIR/jarvis-orchestrator.service"
cat > "$OUTDIR/jarvis-orchestrator.service" <<EOF
[Unit]
Description=Jarvis AI Orchestrator
After=network.target llama-fast.service
Wants=llama-fast.service
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
$ORCH_USER
WorkingDirectory=$REPO/src/orchestrator
Environment="PATH=/usr/local/bin:/usr/bin:/bin"
Environment="HF_HUB_OFFLINE=1"
Environment="TRANSFORMERS_OFFLINE=1"
$ORCH_HOME
ExecStart=$UV run --no-sync uvicorn main:app --host $HOST --port $PORT --workers 1
Restart=always
RestartSec=5
SyslogIdentifier=jarvis-orchestrator
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
$ORCH_PROTECT
$ORCH_RW
$ORCH_PH
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LockPersonality=true
UMask=0077
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

say "write $OUTDIR/llama-fast.service"
cat > "$OUTDIR/llama-fast.service" <<EOF
[Unit]
Description=Jarvis Fast Brain - LLM Server
After=network.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
$LLAMA_USER
$LLAMA_LD
ExecStart=$LLAMA_BIN -m $GGUF -c 4096 -t 2 --batch-size 256 --ubatch-size 256 --host 127.0.0.1 --port 8081 --parallel 1 --reasoning off
Restart=on-failure
RestartSec=10
SyslogIdentifier=llama-fast
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
$LLAMA_PROTECT
$LLAMA_PH
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictAddressFamilies=AF_INET AF_INET6
LockPersonality=true
UMask=0077
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

if [ "$DRY" = 1 ]; then
  echo "DRY_RUN: units written to $OUTDIR (not installed). Review them, then re-run without DRY_RUN as root."
  exit 0
fi

say "enable + start"
systemctl daemon-reload
systemctl enable --now llama-fast jarvis-orchestrator >/dev/null 2>&1 || systemctl restart llama-fast jarvis-orchestrator

say "verify (model load can take ~30s)"
ok=""
for _ in $(seq 1 40); do
  curl -fsS --max-time 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && { ok=1; break; }
  sleep 2
done
if [ -n "$ok" ]; then
  echo "  ✓ Jarvis is up on :$PORT as '${SVC_USER}'."
else
  echo "  ✗ health check failed. Inspect:  journalctl -u jarvis-orchestrator -u llama-fast -n 60 --no-pager"
  exit 1
fi
