#!/usr/bin/env bash
# Pre-download the embedding model into ./embed-cache so `docker compose build` bakes it into the image
# (memory then works OFFLINE at runtime — no HF token needed). This is the network-resilient path:
# the download happens here (re-run to resume), and the build just copies the result.
#
#   bash src/scripts/prepare_embed_cache.sh
#
# Needs the Python env (run `uv sync` first) and, for the gated default (embeddinggemma), an accepted
# Gemma license + a token: `uv run huggingface-cli login` or export HF_TOKEN. Override the model with
# EMBED_MODEL=<repo> (e.g. a non-gated model needs no token). See licenses/gemma/ for the Gemma terms.
#
# Docker-only host (no local Python)? Skip this and bake at build instead:
#   HF_TOKEN=hf_xxx docker compose build
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
EMBED_MODEL="${EMBED_MODEL:-google/embeddinggemma-300m}"
DEST="$REPO/embed-cache"
mkdir -p "$DEST"

echo "Downloading embedding model '$EMBED_MODEL' into $DEST …"
HF_HOME="$DEST" EMBED_MODEL="$EMBED_MODEL" uv run python - <<'PY'
import os
from sentence_transformers import SentenceTransformer
SentenceTransformer(os.environ["EMBED_MODEL"])
print("ok")
PY

echo "Done. 'docker compose build' will now bake '$EMBED_MODEL' into the image (offline at runtime)."
