#!/usr/bin/env bash
# COMBINED / all-in-one mode: run llama-server AND the orchestrator in ONE container. They talk over
# 127.0.0.1 (same machine), so no Docker network or 'llama' hostname is needed — just:
#
#   docker run --init -p 5000:5000 --entrypoint /app/docker/all-in-one.sh <image>
#
# (`--init` gives a proper PID 1 that reaps children — recommended.) This mirrors the native box, where
# llama-server and the orchestrator run as two processes on one machine. Simpler than the two-container
# split; the trade-off is no independent restart — if either service dies the container exits (rely on a
# restart policy), and logs are interleaved. For scale/independent lifecycle, use the two-container compose.
set -uo pipefail
log() { printf '[all-in-one] %s\n' "$1"; }

# 1) LLM engine in the background, on loopback.
log "starting llama-server in the background (127.0.0.1:8081)…"
/app/docker/llama-entry.sh -c "${LLM_CTX:-4096}" -t "${LLAMA_THREADS:-4}" \
  --host 127.0.0.1 --port 8081 --parallel 1 &
LLAMA_PID=$!

# 2) Point the orchestrator at the local llama (loopback) instead of the compose 'llama' hostname.
export JARVIS_FAST_BRAIN_URL="http://127.0.0.1:8081/v1/chat/completions"

# 3) Orchestrator (its normal bootstrap + uvicorn) in the background too, so we can supervise both.
log "starting orchestrator…"
/app/docker/entrypoint.sh &
ORCH_PID=$!

# Clean shutdown on stop; exit if either service dies (fail-fast → external restart policy recovers).
shutdown() { log "shutting down…"; kill "$LLAMA_PID" "$ORCH_PID" 2>/dev/null; }
trap shutdown TERM INT
wait -n "$LLAMA_PID" "$ORCH_PID"
log "a service exited — stopping the container"
shutdown
wait 2>/dev/null || true
