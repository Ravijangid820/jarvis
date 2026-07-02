#!/usr/bin/env bash
# One-shot setup for a fresh checkout — system or container. Idempotent: safe to re-run.
#
#   bash src/scripts/setup.sh
#
# Env toggles:
#   SKIP_NATIVE=1            skip the whisper.cpp / llama.cpp C++ build
#   SKIP_MODELS=1            skip model downloads (run download_models.sh later)
#   ADMIN_USER, ADMIN_PASS   create an admin user non-interactively
#   LLM_GGUF_URL, HF_TOKEN   passed through to download_models.sh
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
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
if uv run python src/scripts/manage.py list-users 2>/dev/null | grep -q "no users"; then
  if [ -n "${ADMIN_USER:-}" ] && [ -n "${ADMIN_PASS:-}" ]; then
    uv run python src/scripts/manage.py create-admin "$ADMIN_USER" "$ADMIN_PASS" && ok "admin '$ADMIN_USER' created"
  else
    warn "no users yet — create one: uv run python src/scripts/manage.py create-admin <user> <pass>"
  fi
else
  ok "users already exist"
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
cat <<EOF
Next steps:
  • Review config/jarvis.json (host, db_path, fast_brain_url).
  • LLM GGUF: the default Qwen3.5-2B downloads automatically — if it failed above (network), just
    re-run; set LLM_GGUF_URL only to use a *different* model.
  • Start the LLM:  <repo>/llama.cpp/build/bin/llama-server -m <gguf> -c 4096 --host 127.0.0.1 --port 8081
  • Start the app:  cd src/orchestrator && uv run uvicorn main:app --host 127.0.0.1 --port 5000
  • Or install the units in systemd/ (see docs/DEPLOY.md), then add TLS (docs/DEPLOY.md → "Adding TLS").
EOF
