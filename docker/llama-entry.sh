#!/usr/bin/env bash
# Entrypoint for the `llama` service. Chooses the model, then execs llama-server.
#   default  : the model baked into the image (first .gguf under /opt/jarvis/models) — zero config.
#   your own : set LLM_MODEL=<path under ./models>; optionally LLM_GGUF_URL to fetch it on first run.
# The baked default lives at a separate path from the ./models mount, so overrides never hide it.
set -uo pipefail
log() { printf '[llama] %s\n' "$1"; }

BAKED_DIR="/opt/jarvis/models"   # baked into the image at build time
MOUNT_DIR="/app/models"          # ./models bind mount (user-supplied models)

if [ -n "${LLM_MODEL:-}" ]; then
  # --- User override -------------------------------------------------------
  MODEL_FILE="${MOUNT_DIR}/${LLM_MODEL}"
  if [ ! -f "$MODEL_FILE" ] && [ -n "${LLM_GGUF_URL:-}" ]; then
    log "override model not found — downloading to $MODEL_FILE"
    case "$LLM_GGUF_URL" in
      https://*) ;;
      *) log "WARN: LLM_GGUF_URL is not https:// — the download could be tampered with in transit" ;;
    esac
    mkdir -p "$(dirname "$MODEL_FILE")"
    if ! curl -L --fail -o "$MODEL_FILE" "$LLM_GGUF_URL"; then
      log "ERROR: download failed"; rm -f "$MODEL_FILE"; exit 1
    fi
    if [ -n "${LLM_GGUF_SHA256:-}" ]; then
      if echo "${LLM_GGUF_SHA256}  ${MODEL_FILE}" | sha256sum -c - >/dev/null 2>&1; then
        log "checksum verified"
      else
        log "ERROR: SHA-256 mismatch — refusing to start (deleting the bad file)"; rm -f "$MODEL_FILE"; exit 1
      fi
    else
      log "WARN: LLM_GGUF_SHA256 not set — skipping integrity check (set it to verify the download)"
    fi
  fi
  SRC="override (LLM_MODEL=${LLM_MODEL})"
else
  # --- Baked-in default ----------------------------------------------------
  MODEL_FILE="$(find "$BAKED_DIR" -name '*.gguf' -type f 2>/dev/null | sort | head -n1)"
  SRC="baked-in default"
fi

if [ -z "${MODEL_FILE:-}" ] || [ ! -f "$MODEL_FILE" ]; then
  log "ERROR: no model available."
  log "The default model is baked into the image (put a .gguf in ./models before 'docker compose build')."
  log "To use your own: set LLM_MODEL=<file under ./models> (optionally LLM_GGUF_URL to fetch it)."
  exit 1
fi

log "model: $MODEL_FILE  [${SRC}]"
# LLAMA_EXTRA_ARGS is intentionally word-split so users can pass any llama-server flags from .env.
# shellcheck disable=SC2086
exec /app/llama.cpp/build/bin/llama-server -m "$MODEL_FILE" ${LLAMA_EXTRA_ARGS:-} "$@"
