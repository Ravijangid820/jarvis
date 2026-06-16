#!/usr/bin/env bash
# Download the models Jarvis needs into the (gitignored) model dirs.
# Idempotent and non-fatal per step: a failure warns and continues so you can see
# everything that's missing in one pass. Paths are relative to the repo, so this works
# in any checkout or container — not just /srv/jarvis.
#
#   bash src/scripts/download_models.sh
#
# Env:
#   LLM_GGUF_URL   URL to fetch the LLM GGUF from (the source isn't pinned in the repo)
#   HF_TOKEN       HuggingFace token, if the embedding model is gated for your account
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cyan() { printf '\033[1;36m▸ %s\033[0m\n' "$1"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$1"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$1"; }

# 1) Embedding model → HuggingFace cache (~/.cache/huggingface). Gated (Gemma license):
#    accept the terms on the model page and `uv run huggingface-cli login` (or set HF_TOKEN).
cyan "Embedding model: google/embeddinggemma-300m (HuggingFace cache)"
if uv run python - <<'PY'
from sentence_transformers import SentenceTransformer
SentenceTransformer("google/embeddinggemma-300m")
PY
then ok "embedding model present in cache"
else warn "could not fetch embeddinggemma-300m — accept its license on HuggingFace and run 'uv run huggingface-cli login' (or set HF_TOKEN), then re-run"
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

# 4) LLM GGUF — the source is not pinned in this repo (project-specific model). Provide
#    LLM_GGUF_URL to fetch it, or drop the file in place manually.
cyan "LLM GGUF"
GGUF_DEST="$REPO/models/qwen3.5_2b/Qwen3.5-2B-Q4_K_M.gguf"
if [ -f "$GGUF_DEST" ]; then
  ok "present: $GGUF_DEST"
elif [ -n "${LLM_GGUF_URL:-}" ]; then
  mkdir -p "$(dirname "$GGUF_DEST")"
  if curl -L --fail -o "$GGUF_DEST" "$LLM_GGUF_URL"; then ok "downloaded GGUF"
  else warn "GGUF download from \$LLM_GGUF_URL failed"; fi
else
  warn "GGUF missing. Set LLM_GGUF_URL=<url> and re-run, or place the file at: $GGUF_DEST"
fi

cyan "Model setup pass complete (review any ! warnings above)."
