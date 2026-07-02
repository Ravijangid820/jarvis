#!/usr/bin/env bash
# Download the models Jarvis needs into the (gitignored) model dirs.
# Idempotent and non-fatal per step: a failure warns and continues so you can see
# everything that's missing in one pass. Paths are relative to the repo, so this works
# in any checkout or container — not just /srv/jarvis.
#
#   bash src/scripts/download_models.sh
#
# Env (all optional — sensible defaults, same pinned model the Docker images use):
#   LLM_GGUF_URL     LLM GGUF source (default: pinned Qwen3.5-2B-Q4_K_M from unsloth, SHA-verified)
#   LLM_GGUF_SHA256  expected SHA-256 of that GGUF (default matches the pinned model)
#   EMBED_MODEL      embedding model repo (default embeddinggemma-300m; use a non-gated one to skip the token)
#   HF_TOKEN         HuggingFace token — only needed for a *gated* embedding model (the default is gated)
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cyan() { printf '\033[1;36m▸ %s\033[0m\n' "$1"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$1"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$1"; }

# 1) Embedding model → HuggingFace cache (~/.cache/huggingface). The default (embeddinggemma) is
#    gated (Gemma license): accept the terms on the model page and `uv run huggingface-cli login`
#    (or set HF_TOKEN). Override with EMBED_MODEL=<repo> for a different (e.g. non-gated) model.
EMBED_MODEL="${EMBED_MODEL:-google/embeddinggemma-300m}"
cyan "Embedding model: $EMBED_MODEL (HuggingFace cache)"
if EMBED_MODEL="$EMBED_MODEL" uv run python - <<'PY'
import os
from sentence_transformers import SentenceTransformer
SentenceTransformer(os.environ["EMBED_MODEL"])
PY
then ok "embedding model present in cache"
else warn "could not fetch $EMBED_MODEL — accept its license on HuggingFace and run 'uv run huggingface-cli login' (or set HF_TOKEN), then re-run"
fi

# 2) Piper TTS (binary + en_GB-alan-medium voice)
cyan "Piper TTS (binary + voice)"
if bash "$REPO/src/scripts/piper_setup.sh"; then ok "Piper ready"; else warn "Piper setup failed"; fi

# 3) Whisper STT model (base.en) via whisper.cpp's own downloader
cyan "Whisper model: base.en"
if [ -f "$REPO/whisper/models/download-ggml-model.sh" ]; then
  if (cd "$REPO/whisper" && bash ./models/download-ggml-model.sh base.en); then ok "whisper base.en downloaded"
  else warn "whisper model download failed"; fi
else
  warn "whisper.cpp not found at $REPO/whisper — run 'bash src/scripts/build_native.sh' first, then re-run"
fi

# 4) LLM GGUF — defaults to the SAME pinned model the Docker images bake (public + SHA-verified),
#    so a fresh native setup gets a working LLM with zero config. Override LLM_GGUF_URL /
#    LLM_GGUF_SHA256 for a different model.
cyan "LLM GGUF"
GGUF_DEST="$REPO/models/qwen3.5_2b/Qwen3.5-2B-Q4_K_M.gguf"
LLM_GGUF_URL="${LLM_GGUF_URL:-https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/resolve/main/Qwen3.5-2B-Q4_K_M.gguf}"
LLM_GGUF_SHA256="${LLM_GGUF_SHA256:-aaf42c8b7c3cab2bf3d69c355048d4a0ee9973d48f16c731c0520ee914699223}"
if [ -f "$GGUF_DEST" ]; then
  ok "present: $GGUF_DEST"
else
  mkdir -p "$(dirname "$GGUF_DEST")"
  case "$LLM_GGUF_URL" in
    https://*|file://*) : ;;
    *) warn "LLM_GGUF_URL is not https:// — model could be tampered in transit (set an https URL)";;
  esac
  cyan "downloading ${LLM_GGUF_URL##*/} (~1.3 GB, first run only)…"
  if curl -L --fail --retry 5 --retry-all-errors --retry-delay 5 -C - -o "$GGUF_DEST" "$LLM_GGUF_URL"; then
    if [ -n "${LLM_GGUF_SHA256:-}" ]; then
      if echo "${LLM_GGUF_SHA256}  ${GGUF_DEST}" | sha256sum -c - >/dev/null 2>&1; then ok "downloaded + checksum verified"
      else warn "GGUF SHA-256 MISMATCH — deleting the suspect file"; rm -f "$GGUF_DEST"; fi
    else
      ok "downloaded (set LLM_GGUF_SHA256 to verify integrity)"
    fi
  else warn "GGUF download failed from $LLM_GGUF_URL"; fi
fi

cyan "Model setup pass complete (review any ! warnings above)."
