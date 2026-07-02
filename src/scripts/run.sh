#!/usr/bin/env bash
# Run Jarvis from source: the LLM server (llama.cpp) + the orchestrator, together, in the foreground —
# Ctrl-C stops both. This is the "just run it" dev command (the native counterpart of the container's
# all-in-one entrypoint). To run it as a background/boot service instead, use install_services.sh (systemd).
#
#   bash src/scripts/run.sh
#
# Env: LLM_MODEL (GGUF path; default: the one under models/), LLM_CTX=4096, LLAMA_THREADS=4,
#      HOST=0.0.0.0, PORT=5000.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
cyan() { printf '\033[1;36m▸ %s\033[0m\n' "$1"; }
err()  { printf '\033[1;31m  ✗ %s\033[0m\n' "$1"; }

LLAMA_BIN="$REPO/llama.cpp/build/bin/llama-server"
[ -x "$LLAMA_BIN" ] || { err "llama-server not built yet — run: bash src/scripts/build_native.sh"; exit 1; }

# Resolve the model: LLM_MODEL override, else the first GGUF under models/.
MODEL="${LLM_MODEL:-}"
[ -n "$MODEL" ] || MODEL="$(find "$REPO/models" -name '*.gguf' 2>/dev/null | sort | head -n1)"
{ [ -n "$MODEL" ] && [ -f "$MODEL" ]; } || { err "no GGUF model found — run: bash src/scripts/download_models.sh"; exit 1; }

cyan "LLM: starting llama-server on 127.0.0.1:8081 — model ${MODEL##*/}"
"$LLAMA_BIN" -m "$MODEL" -c "${LLM_CTX:-4096}" -t "${LLAMA_THREADS:-4}" \
  --host 127.0.0.1 --port 8081 --parallel 1 &
LLAMA_PID=$!

# The orchestrator talks to the local llama; Ctrl-C (or a dying llama) stops everything.
export JARVIS_FAST_BRAIN_URL="http://127.0.0.1:8081/v1/chat/completions"
cleanup() { cyan "stopping…"; kill "$LLAMA_PID" 2>/dev/null; wait "$LLAMA_PID" 2>/dev/null || true; }
trap cleanup INT TERM EXIT

cyan "Orchestrator: http://${HOST:-0.0.0.0}:${PORT:-5000}   (Ctrl-C to stop both)"
uv run uvicorn main:app --app-dir src/orchestrator --host "${HOST:-0.0.0.0}" --port "${PORT:-5000}"
