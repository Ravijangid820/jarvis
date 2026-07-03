#!/usr/bin/env bash
# One-shot setup for a fresh checkout — system or container. Idempotent: safe to re-run.
# Bootstraps everything (env, config, frontend, DB, admin, native build, models), seeds a default
# admin/admin, then RUNS both services (Ctrl-C to stop). For a boot service instead: install_services.sh.
#
#   bash src/scripts/setup.sh
#
# Env toggles:
#   SKIP_NATIVE=1            skip the whisper.cpp / llama.cpp C++ build
#   SKIP_MODELS=1            skip model downloads (run download_models.sh later)
#   SKIP_RUN=1              bootstrap only — don't start the services at the end
#   ADMIN_USER, ADMIN_PASS   admin login to seed (default admin/admin)
#   LLM_GGUF_URL, HF_TOKEN   passed through to download_models.sh
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
# Optional ./.env (same file docker compose reads; shell-set variables win). See .env.example.
source "$REPO/src/scripts/load_env.sh"
step() { printf '\n\033[1;36m━━ %s ━━\033[0m\n' "$1"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$1"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$1"; }

step "Prerequisites"
command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/"; exit 1; }
ok "uv present"
command -v node >/dev/null && ok "node $(node -v)" || warn "node not found — frontend build will be skipped"
if [ "${SKIP_NATIVE:-}" != 1 ]; then
  { command -v cc >/dev/null && command -v cmake >/dev/null; } && ok "C toolchain (cc + cmake)" \
    || warn "cc/cmake missing — the native whisper/llama build will fail; install build-essential + cmake (or set SKIP_NATIVE=1)"
fi
free_gb="$(df -P -BG "$REPO" 2>/dev/null | awk 'NR==2{gsub(/G/,"",$4);print $4}')"
{ [ -n "$free_gb" ] && [ "$free_gb" -ge 8 ]; } 2>/dev/null && ok "disk: ${free_gb}G free" \
  || warn "low disk (${free_gb:-?}G free) — models + native builds need several GB"
if [ "${SKIP_MODELS:-}" != 1 ]; then
  [ -n "${HF_TOKEN:-}" ] && ok "HF_TOKEN set (for the gated embedding model)" \
    || warn "HF_TOKEN not set — the Gemma embedding model is gated; export HF_TOKEN or run 'uv run huggingface-cli login' first"
fi

step "Python environment (uv sync --frozen)"
uv sync --frozen && ok ".venv ready (locked — exact versions from uv.lock)" || { echo "uv sync --frozen failed"; exit 1; }

step "Config"
if [ ! -f config/jarvis.json ]; then
  cp config/jarvis.example.json config/jarvis.json
  ok "created config/jarvis.json from the example — review host / model paths"
else
  ok "config/jarvis.json already exists (left as-is)"
fi

step "Frontend build"
if command -v npm >/dev/null; then
  (cd frontend && npm ci && npm run build) && ok "frontend/dist built" || warn "frontend build failed"
else
  warn "npm not found — skipping (GET / returns 404 until the frontend is built)"
fi

step "Database"
if uv run python -c "import sys; sys.path.insert(0,'src/orchestrator'); import db; db.init_db(); print(db.DB_PATH)"; then
  ok "schema initialized"
else
  warn "DB init failed (check config/jarvis.json paths)"
fi

step "Admin user"
# Default to admin/admin so a fresh setup runs with zero config (same as the Docker path); override with
# ADMIN_USER / ADMIN_PASS. create-admin is a no-op if the user already exists.
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-admin}"
if uv run python src/scripts/manage.py create-admin "$ADMIN_USER" "$ADMIN_PASS" >/dev/null 2>&1; then
  ok "admin '$ADMIN_USER' created"
  [ "$ADMIN_PASS" = "admin" ] && warn "login is admin/admin (default) — change the password for anything exposed"
else
  ok "admin '$ADMIN_USER' already exists (unchanged)"
fi

if [ "${SKIP_NATIVE:-0}" != "1" ]; then
  step "Native engines (whisper.cpp + llama.cpp)"
  bash "$REPO/src/scripts/build_native.sh" || warn "native build had issues (see above)"
else
  warn "SKIP_NATIVE=1 — skipping whisper.cpp/llama.cpp build (needed for voice + the LLM server)"
fi

if [ "${SKIP_MODELS:-0}" != "1" ]; then
  step "Models"
  bash "$REPO/src/scripts/download_models.sh" || true
else
  warn "SKIP_MODELS=1 — run src/scripts/download_models.sh when ready"
fi

step "Setup complete"
if [ "${SKIP_RUN:-0}" = "1" ]; then
  # Bootstrap-only (e.g. setup-server.sh, which goes on to install the systemd units).
  cat <<EOF
  • Run it now:       bash src/scripts/run.sh                       (both services, Ctrl-C to stop)
  • Or as a service:  sudo bash src/scripts/install_services.sh     (systemd; see docs/DEPLOY.md)
  • Config: config/jarvis.json (host, db_path, fast_brain_url). Set LLM_GGUF_URL for a different model.
EOF
else
  step "Starting Jarvis — Ctrl-C to stop.  (Run as a boot service instead: sudo bash src/scripts/install_services.sh)"
  exec bash "$REPO/src/scripts/run.sh"
fi
