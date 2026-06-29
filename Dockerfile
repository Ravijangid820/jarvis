# syntax=docker/dockerfile:1
# Jarvis server image — ONE self-contained image holding the orchestrator AND a from-source
# llama.cpp build (no dependency on a prebuilt llama image). It mirrors the native install's
# controlled CPU flags (see src/scripts/build_native.sh). Compose runs this one image twice:
# once as the LLM server (llama-server), once as the orchestrator (uvicorn).
#
# Status: initial — build on a Docker host and iterate (see docs/setup/docker.md).

# --- Stage 1: build the React frontend ---
FROM node:20-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- Stage 2: compile llama.cpp (server) from source, with controllable CPU flags ---
FROM debian:12-slim AS native
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# Pin the upstream release for a reproducible, tamper-evident build (recommended). Mirrors
# build_native.sh: unset = upstream HEAD (not pinned).
ARG LLAMA_CPP_REF=
# CPU baseline. Default targets modern hosts (AVX2). For AVX-only / older CPUs set GGML_AVX2=OFF
# (this is what the native old-box build does). NATIVE=OFF keeps the binary portable, not pinned
# to the build machine's exact CPU.
ARG GGML_NATIVE=OFF
ARG GGML_AVX2=ON
WORKDIR /src
RUN if [ -n "$LLAMA_CPP_REF" ]; then \
      git clone --branch "$LLAMA_CPP_REF" --depth 1 https://github.com/ggml-org/llama.cpp . ; \
    else \
      echo "WARN: LLAMA_CPP_REF unset — building upstream HEAD (not pinned). Pass --build-arg LLAMA_CPP_REF=<tag> to pin." ; \
      git clone --depth 1 https://github.com/ggml-org/llama.cpp . ; \
    fi \
 && echo "llama.cpp at: $(git rev-parse HEAD)" \
 && cmake -S . -B build \
      -DGGML_NATIVE=${GGML_NATIVE} -DGGML_AVX2=${GGML_AVX2} \
      -DLLAMA_CURL=OFF -DBUILD_SHARED_LIBS=OFF \
 && cmake --build build -j --target llama-server

# --- Stage 3: resolve the default model — use what's in ./models, else download + verify it ---
FROM debian:12-slim AS model
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
ARG LLM_GGUF_URL=
ARG LLM_GGUF_SHA256=
# Which GGUF to bake when ./models has several. Prefer this exact filename; fall back to the first
# one found. Avoids silently baking the wrong model (e.g. a 4B sitting next to the intended 2B).
ARG DEFAULT_MODEL=Qwen3.5-2B-Q4_K_M.gguf
COPY models/ /staged/
RUN set -eu; mkdir -p /out; \
    existing="$(find /staged -name "$DEFAULT_MODEL" -type f | head -n1 || true)"; \
    [ -z "$existing" ] && existing="$(find /staged -name '*.gguf' -type f | sort | head -n1 || true)"; \
    if [ -n "$existing" ]; then \
      echo "Baking model from build context: $existing"; cp "$existing" /out/; \
    elif [ -n "$LLM_GGUF_URL" ]; then \
      echo "No model in ./models — downloading from LLM_GGUF_URL"; \
      case "$LLM_GGUF_URL" in https://*) ;; *) echo "WARN: LLM_GGUF_URL is not https:// — could be tampered in transit";; esac; \
      fn="$(basename "$LLM_GGUF_URL")"; case "$fn" in *.gguf) ;; *) fn=model.gguf;; esac; \
      curl -L --fail -o "/out/$fn" "$LLM_GGUF_URL"; \
      if [ -n "$LLM_GGUF_SHA256" ]; then echo "${LLM_GGUF_SHA256}  /out/$fn" | sha256sum -c -; \
      else echo "WARN: LLM_GGUF_SHA256 not set — skipping integrity check (set it to verify the download)"; fi; \
    else \
      echo "NOTE: no model in ./models and no LLM_GGUF_URL — image ships without a baked default (set one to bake)."; \
    fi

# --- Stage 4: Python runtime (orchestrator) + the compiled llama-server ---
FROM python:3.12-slim AS app
ENV JARVIS_HOME=/app \
    JARVIS_CONFIG=/app/config/jarvis.json \
    HF_HOME=/app/.cache/huggingface \
    PYTHONUNBUFFERED=1
# Runtime libs: libgomp1 + libstdc++ for the llama-server binary and torch/onnxruntime;
# curl/tar for the Piper fetch.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl tar gzip libgomp1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
# uv (copied from the official image — pinned, no curl|sh)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
WORKDIR /app
# Python deps first so the layer caches across source edits.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
# Application source + config templates (config/jarvis.json is excluded by .dockerignore).
COPY src/ ./src/
COPY config/ ./config/
COPY --from=web /web/dist ./frontend/dist
# The from-source llama-server binary (statically linked — single file).
COPY --from=native /src/build/bin/llama-server ./llama.cpp/build/bin/llama-server
# Bake the default model resolved by the `model` stage (from ./models, or auto-downloaded at build).
# Kept at /opt so a ./models bind-mount override never hides it; the first .gguf found is the default.
COPY --from=model /out/ /opt/jarvis/models/
# Bake Piper (binary + voice) so TTS works offline at startup.
RUN bash src/scripts/piper_setup.sh || echo "WARN: piper_setup failed — TTS will be unavailable until fixed"
# Container entry scripts (orchestrator bootstrap + llama model-ensure wrapper).
COPY docker/ ./docker/
RUN chmod +x docker/*.sh
# 5000 = orchestrator (HTTP/S), 8081 = llama-server (used when this image runs as the llama service)
EXPOSE 5000 8081
ENTRYPOINT ["/app/docker/entrypoint.sh"]
