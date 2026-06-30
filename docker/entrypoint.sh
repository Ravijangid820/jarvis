#!/usr/bin/env bash
# First-run bootstrap for the orchestrator container, then exec uvicorn.
# Idempotent: safe on every start. Configuration comes from the mounted config + .env (see
# docs/setup/docker.md). JARVIS_HOME=/app and JARVIS_CONFIG are set in the image.
set -uo pipefail
cd /app
log()  { printf '[jarvis] %s\n' "$1"; }
rule() { printf '[jarvis] %s\n' "────────────────────────────────────────────────────────────"; }

# 1) Config — seed jarvis.json from the Docker template on first run (relative paths, llama URL).
if [ ! -f config/jarvis.json ]; then
  cp config/jarvis.docker.json config/jarvis.json && log "created config/jarvis.json from the Docker template"
fi

# 2) Embedding model (gated Gemma) — ensure it's in the HF cache. First run downloads ~1.2GB and
#    needs HF_TOKEN; progress is shown (not silenced) so it doesn't look frozen.
log "checking embedding model (first run downloads ~1.2GB; needs HF_TOKEN)…"
if uv run python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('google/embeddinggemma-300m')"; then
  EMB="ready"
else
  EMB="UNAVAILABLE — set HF_TOKEN + accept the Gemma license, then restart"
fi

# 3) Database schema (init is idempotent).
if uv run python -c "import sys; sys.path.insert(0,'src/orchestrator'); import db; db.init_db()" >/dev/null 2>&1; then
  DB="ready"
else
  DB="INIT FAILED — check volume permissions / config paths"
fi

# 4) Admin user. Defaults to admin/admin so the stack runs with zero config; override ADMIN_USER /
#    ADMIN_PASS via -e, compose, or .env. create-admin is a no-op if the user already exists.
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-admin}"
WEAK_PASS=""
[ "$ADMIN_PASS" = "admin" ] && WEAK_PASS="yes"
if uv run python src/scripts/manage.py create-admin "$ADMIN_USER" "$ADMIN_PASS" >/dev/null 2>&1; then
  ADMIN="$ADMIN_USER (created)"
else
  ADMIN="$ADMIN_USER (already exists — unchanged)"
fi

# 5) TLS — opt in by mounting a tls/ dir holding server.crt + server.key (e.g. from setup_tls.sh).
#    Present -> serve HTTPS; absent -> HTTP (put a TLS proxy in front, or mount certs).
SSL_ARGS=(); SCHEME="http"; TLS="off — HTTP (add a TLS proxy, or mount tls/ for HTTPS)"
if [ -f tls/server.crt ] && [ -f tls/server.key ]; then
  SSL_ARGS=(--ssl-certfile tls/server.crt --ssl-keyfile tls/server.key)
  SCHEME="https"; TLS="on — serving HTTPS from the mounted tls/"
fi

# 6) Summary banner, then serve. HOST_PORT is the host-published port (default 5000).
PORT="${HOST_PORT:-5000}"
echo
rule
log "Jarvis orchestrator — starting"
log "  Web UI / API : ${SCHEME}://localhost:${PORT}"
log "  TLS          : ${TLS}"
log "  Admin user   : ${ADMIN}"
[ "$WEAK_PASS" = yes ] && log "  Admin pass   : 'admin' (DEFAULT — set ADMIN_PASS to change; do so for anything exposed)"
log "  Embedding    : ${EMB}"
log "  Database     : ${DB}   (persisted in the /app/memory volume)"
log "  LLM backend  : http://llama:8081   (the 'llama' service)"
log "  Mint API key : docker compose exec orchestrator uv run python src/scripts/manage.py mint-key <user>"
rule
echo
exec uv run uvicorn main:app --app-dir src/orchestrator --host 0.0.0.0 --port 5000 "${SSL_ARGS[@]}"
