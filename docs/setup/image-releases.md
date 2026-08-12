# Container images (GHCR)

> **Upgrading to 3.4.0 or later from 3.3.0 or earlier.** `ALLOWED_ORIGINS` empty now means "allow
> no cross-origin caller"; it used to fall back to `*`. A UI served from another origin (GitHub
> Pages, a separate nginx) must name its origin explicitly — set it *before* pulling, or every call
> is blocked and the browser reports a CORS error. Same-origin deployments are unaffected. See
> [docker.md](docker.md#upgrading-to-340--read-this-first).


The **LLM engine is upstream's** (`ghcr.io/ggml-org/llama.cpp:server`) — we don't compile llama.cpp.
This repo publishes **four** images, all at the same version, all built together by the Actions
workflow on a `v*` tag:

| Image | Contains | Run as |
| --- | --- | --- |
| **`ghcr.io/ravijangid820/jarvis-combined`** | official `llama-server` + orchestrator + UI + baked LLM & embedding, in one image | **single container** (default entrypoint runs both) — simplest / **Proxmox OCI** |
| **`ghcr.io/ravijangid820/jarvis-orchestrator`** | **app only** — FastAPI + UI + embeddings + TTS, **no LLM** | the **two-service split**: pairs with a `llama` service (`docker-compose.yml`). Not runnable standalone. |
| **`ghcr.io/ravijangid820/jarvis-llama`** | the official `llama-server` with the pinned GGUF **baked in** | drop-in for the upstream image when you'd rather not manage a `./models` mount |
| **`ghcr.io/ravijangid820/jarvis-frontend`** | the built SPA on nginx, proxying `/api/` to the orchestrator | optional separate web tier; the orchestrator already serves the UI |

Every image that serves the SPA also carries the **browser-side model bundles** — face (YuNet +
SFace), wake word (openWakeWord), and the failsafe Whisper copy — because the page's Web Workers
fetch those from whichever origin served the SPA, not from the API. See
[docker.md](docker.md#browser-side-model-bundles-face--wake-word--stt).

- `jarvis-combined` is built **on** the official `ggml-org/llama.cpp:server` image (Ubuntu 24.04 + its
  prebuilt, all-CPU-variant `llama-server`) — no compile; a new llama.cpp release is a `LLAMA_IMAGE` bump.
- `jarvis-orchestrator` (from `Dockerfile.orchestrator`) has **no LLM** — on its own it serves the UI but
  needs a companion `llama` service.

`jarvis-combined` and `jarvis-orchestrator` both bake the **torch-free ONNX embedding bundle**
(offline memory; no token at build OR runtime; ~2 GB smaller than the torch-based 2.3.x images) and
ship the Gemma license. The camera/volume agents and voice listener run natively. See
[docker.md](docker.md) for how to run each.

> **Tag numbering.** Image tags track the **repo version**: git tag `vX.Y.Z` → image `X.Y.Z` + `latest`
> (Docker tags drop the leading `v`). Those are the **only** things that move `:latest` — merging to
> `main` publishes nothing, and a manual workflow run publishes only the tag you type. For
> reproducible production builds, pin `LLAMA_IMAGE` to a specific
> `ghcr.io/ggml-org/llama.cpp:server-b<NNNN>` tag (`:server` floats).

## History
Earlier releases published a single fat **`jarvis-server`** image that **compiled llama.cpp from source**
(tags `0.1`, then `0.2` = `2.2.0`). As of **v2.3.0** that image is **retired**: we ride the official
prebuilt llama.cpp binary instead — the same all-CPU-variant portability (runs on the AVX-only box), with
zero compile to maintain and automatic benefit from upstream releases.

- `0.1` — first container; LLM baked, but embedding **not** baked (needed an `HF_TOKEN` at runtime).
- `0.2` = `2.2.0` — baked both models, added the all-in-one mode, zero-config defaults, Gemma license.
- `2.3.0` — dropped the from-source build; `jarvis-combined` (on the official image) + `jarvis-orchestrator`.
