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
#   EMBED_ONNX_BASE  override the ONNX-bundle source (default: the project's pinned public HF repo)
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cyan() { printf '\033[1;36m▸ %s\033[0m\n' "$1"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$1"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$1"; }

# 1) Embedding model → torch-free ONNX bundle (the FULL sentence-transformers pipeline in one graph,
#    converted from the official google/embeddinggemma-300m weights by src/scripts/export_embed_onnx.py
#    and verified cosine 1.000000 vs torch). Hosted PUBLIC on the project owner's HF — NO token needed.
#    Every file is SHA-256-pinned. Custom EMBED_MODEL? Export your own bundle (see the exporter script).
EMBED_MODEL="${EMBED_MODEL:-google/embeddinggemma-300m}"
EMBED_ONNX_DIR="$REPO/models/embed_onnx"
EMBED_ONNX_BASE="${EMBED_ONNX_BASE:-https://huggingface.co/Ravijangid820/embeddinggemma-300m-onnx/resolve/main}"
cyan "Embedding model: $EMBED_MODEL (ONNX bundle, no token)"
if [ "$EMBED_MODEL" != "google/embeddinggemma-300m" ]; then
  warn "custom EMBED_MODEL — the pinned bundle is for embeddinggemma; export your own with:"
  warn "  uv run --index https://download.pytorch.org/whl/cpu --with sentence-transformers --with onnx --with onnxscript python src/scripts/export_embed_onnx.py"
else
  mkdir -p "$EMBED_ONNX_DIR"
  fetch_onnx() {  # $1=file  $2=sha256 — skip when present+verified; else download (retry+resume) + verify
    f="$EMBED_ONNX_DIR/$1"
    if [ -f "$f" ] && echo "$2  $f" | sha256sum -c - >/dev/null 2>&1; then return 0; fi
    curl -L --fail --retry 5 --retry-all-errors --retry-delay 5 -C - -o "$f" "$EMBED_ONNX_BASE/$1" || return 1
    echo "$2  $f" | sha256sum -c - >/dev/null 2>&1 || { warn "$1 SHA-256 MISMATCH — deleting"; rm -f "$f"; return 1; }
  }
  if fetch_onnx model.onnx           39a1f3039ed66e39c5174469dc5ce0417ef57993590170164b18beb8254de2d0 \
  && fetch_onnx model.onnx.data      1d5fb11500ae836f3a42efc3c7123076416d9e527ae479d19d940a3c784f0035 \
  && fetch_onnx tokenizer.json       3f797e7e336523ba3845bf09a648fd87c14bf357f26beb091d8284dff48ea27c \
  && fetch_onnx tokenizer_config.json 7f4973d11e065de3097b85319ef7c43e143aa7f1df619ebe1056f81b8de97edb \
  && fetch_onnx meta.json            b149d450b0bd383a207b3328cb9dd093082077c84eb4e178418a2db0f4f2dccf; then
    ok "ONNX embedding bundle present + verified ($EMBED_ONNX_DIR)"
  else
    warn "ONNX bundle incomplete — memory/RAG will be disabled until it downloads (re-run this script)"
  fi
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
