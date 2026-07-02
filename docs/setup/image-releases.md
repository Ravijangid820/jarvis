# Container images (GHCR)

The **LLM is the official upstream image** (`ghcr.io/ggml-org/llama.cpp:server`) — we don't build or
publish llama.cpp. This repo publishes **two** images (same version, built together by the Actions
workflow):

| Image | Contains | Run as |
| --- | --- | --- |
| **`ghcr.io/ravijangid820/jarvis-combined`** | official `llama-server` + orchestrator + baked LLM & embedding, in one image | **single container** (default entrypoint runs both) — simplest / **Proxmox OCI** |
| **`ghcr.io/ravijangid820/jarvis-orchestrator`** | **slim app only** — FastAPI + UI + embeddings + TTS, **no LLM** | the **two-service split**: pairs with the official `llama.cpp:server` image (`docker-compose.yml`). Not runnable standalone. |

- `jarvis-combined` is built **on** the official `ggml-org/llama.cpp:server` image (Ubuntu 24.04 + its
  prebuilt, all-CPU-variant `llama-server`) — no compile; a new llama.cpp release is a `LLAMA_IMAGE` bump.
- `jarvis-orchestrator` (from `Dockerfile.orchestrator`) has **no LLM** — on its own it serves the UI but
  needs a companion `llama` service.

Both bake the embedding model (offline memory, no runtime token) + ship the Gemma license. The
camera/volume agents and voice listener run natively. See [docker.md](docker.md) for how to run each.

> **Tag numbering.** Image tags track the **repo version**: git tag `vX.Y.Z` → image `X.Y.Z` + `latest`
> (Docker tags drop the leading `v`). For reproducible production builds, pin `LLAMA_IMAGE` to a specific
> `ghcr.io/ggml-org/llama.cpp:server-b<NNNN>` tag (`:server` floats).

## History
Earlier releases published a single fat **`jarvis-server`** image that **compiled llama.cpp from source**
(tags `0.1`, then `0.2` = `2.2.0`). As of **v2.3.0** that image is **retired**: we ride the official
prebuilt llama.cpp binary instead — the same all-CPU-variant portability (runs on the AVX-only box), with
zero compile to maintain and automatic benefit from upstream releases.

- `0.1` — first container; LLM baked, but embedding **not** baked (needed an `HF_TOKEN` at runtime).
- `0.2` = `2.2.0` — baked both models, added the all-in-one mode, zero-config defaults, Gemma license.
- `2.3.0` — dropped the from-source build; `jarvis-combined` (on the official image) + `jarvis-orchestrator`.
