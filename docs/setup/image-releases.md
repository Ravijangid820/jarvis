# Container image releases (GHCR)

Published at `ghcr.io/ravijangid820/jarvis-server:<tag>`. The image is the **server stack** (orchestrator
+ llama.cpp); the camera/volume agents and voice listener run natively. See
[docker.md](docker.md) for how to run each shape (two-container compose, single-container all-in-one, or
raw `docker run`).

> **Tag numbering.** Image tags track the **repo version**: pushing git tag `vX.Y.Z` builds image `X.Y.Z`
> **+** `latest` (Docker tags drop the leading `v`, so the number matches the repo tag exactly). The
> earlier ad-hoc `0.1`/`0.2` tags predate this — `0.2`'s content is identical to **`2.2.0`** (repo v2.2.0).
> **Use `2.2.0` or `latest` going forward** (re-run the Actions workflow on the `v2.2.0` tag to publish them).

## `2.2.0` / `latest` — current, recommended (first published as `0.2`)
Built via the GitHub Actions workflow. Everything `0.1` had, plus:

- **Embedding model baked in.** `embeddinggemma-300m` ships inside the image, so **memory/RAG works
  offline at runtime with no HF token** (like the native box). Configurable via `EMBED_MODEL`.
- **Single-container (all-in-one) mode.** Run llama-server + orchestrator in *one* container over
  loopback: `--entrypoint /app/docker/all-in-one.sh`. (Two-container compose still supported.)
- **Zero-config defaults + optional `.env`.** Runs with no config file; login defaults to `admin`/`admin`
  (override via env); every value has a default.
- **Gemma license bundled** (`licenses/gemma/`) so the baked embedding is redistribution-compliant.
- **Built + pushed on GitHub Actions** (no multi-GB upload from a local machine).

## `0.1` — initial containerized image
The first working container. Contains:

- **LLM baked in** (`Qwen3.5-2B-Q4_K_M`, unsloth GGUF, SHA-verified).
- **Runs on any x86-64 CPU** — llama.cpp built with `GGML_CPU_ALL_VARIANTS`, so it auto-detects and loads
  the best backend (SSE4.2 → AVX → AVX2 → AVX-512) at runtime. No per-CPU rebuild, no crashes.
- **CPU-only PyTorch** (no ~5 GB CUDA), **Python 3.13** base, resilient build (retrying clone/downloads,
  capped compile), CRLF-tolerant scripts, reliable Piper TTS.
- **Two containers** (orchestrator + `llama`) via compose.
- **Embedding NOT baked** → memory needed an **`HF_TOKEN` at runtime** (downloaded on first start).

## What changed, `0.1` → `2.2.0` (was `0.2`)
| | `0.1` | `2.2.0` |
| --- | --- | --- |
| Embedding / memory | runtime download, needs `HF_TOKEN` at run | **baked in — offline, no token** |
| Deployment shapes | two-container only | + **single-container all-in-one** |
| Config | env-driven | + **admin/admin zero-config**, optional `.env` |
| Embedding model | fixed (embeddinggemma) | **configurable** (`EMBED_MODEL`) |
| Licensing | — | **Gemma terms bundled** |
| Build | local `docker compose build` | **GitHub Actions → GHCR** |

Unchanged in both: baked Qwen LLM, all-CPU-variant portability, CPU-only, self-contained server image.

**Use `2.2.0` / `latest`** — it's the superset (same content first published as `0.2`). `0.1` remains for
reference/rollback.
