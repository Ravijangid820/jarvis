# Docker (server stack)

Run the **server** — orchestrator + llama.cpp — in containers. The **camera agent, volume agent, and
voice listener run natively** on the machines that have the camera / mic / speaker; they are not part of
this compose (install them with `docs/setup/camera.md`, `volume-agent.md`, `voice.md`).

> **Status: initial.** These files are authored but not yet build-tested on this box (the production
> host is an LXC without Docker). Build on a Docker host, then iterate — `docker compose build` and
> report errors; expect a short pass to green.

## What runs
**The LLM is the official, upstream-maintained `ghcr.io/ggml-org/llama.cpp:server` image — we don't
compile llama.cpp.** The only images this repo builds are the orchestrator (+ a combined convenience
image built *on top of* that official image). Two images are published:

| Image | What it is | Run as |
| --- | --- | --- |
| **`jarvis-combined`** | official `llama-server` + orchestrator + baked LLM & embedding, in **one** image | a **single container** (default entrypoint runs both) — simplest / **Proxmox OCI** |
| **`jarvis-orchestrator`** | **slim app only** — FastAPI + UI + embeddings + TTS, **no LLM** | the **two-service split**: pairs with the official llama image over the network |

The two-service split (`docker-compose.yml`) runs:

| Service | Role |
| --- | --- |
| `llama` | the **official** llama.cpp server: loads the GGUF, serves the OpenAI-style API on `:8081` (internal only). |
| `orchestrator` | FastAPI API + built React UI + Piper TTS + embeddings. Serves **HTTP** on `:5000`. |

Why the official image: llama.cpp is upstream's to maintain — we ride their prebuilt, **all-CPU-variant**
binary (runs on AVX / AVX2 / AVX-512, auto-detected — including the old box), so a new llama.cpp release is
a one-line image bump and there's **no compile to own**. `jarvis-combined` is built directly on that same
official image (Ubuntu 24.04 + `/app/llama-server`), with our orchestrator layered on.

## Prerequisites
- **Docker + Compose** on the build host. The image's llama.cpp is built with **all CPU variants** and
  auto-detects the best one for the host at runtime — so it runs on AVX-only and AVX2 machines alike,
  no flag to set. (The 2011 production box has no Docker and stays on the native
  `src/scripts/setup-server.sh` build.)
- A **model** — nothing to do by default: `.env.example` ships a verified `LLM_GGUF_URL` for
  Qwen3.5-2B, so an empty `./models` auto-downloads + SHA-verifies it at build. To use your own instead,
  drop a `.gguf` in `./models` (it takes precedence) or point `LLM_GGUF_URL` elsewhere. See
  [The model](#the-model).
- A **HuggingFace token** — only to *bake the embedding model* (memory) into the image, and only for the
  gated default; accept its license first (<https://huggingface.co/google/embeddinggemma-300m>). See
  [Embedding model](#embedding-model-memory--baked-in). Not needed for the LLM or to just run.

## Run
The default `docker-compose.yml` is the **two-service split**: the official llama image + the published
`jarvis-orchestrator`. Put a GGUF in `./models`, then pull + run (no config file — every value defaults,
login `admin`/`admin`):
```bash
bash src/scripts/download_models.sh             # GGUF → ./models (for the llama service)
docker compose pull && docker compose up -d     # official llama + published orchestrator
docker compose logs -f                          # banner shows the login URL
curl http://localhost:5000/health
```
Prefer to build the orchestrator yourself (and bake memory in)? Pass the token and `--build`:
```bash
HF_TOKEN=hf_xxx docker compose up -d --build    # builds jarvis-orchestrator, bakes the embedding model
```
Without the token it still runs — just without memory until you add it. Override anything on the CLI
(or an optional `.env`), e.g. `ADMIN_PASS=secret docker compose up -d`.

> **One container instead of two?** Use the **`jarvis-combined`** image (self-contained; runs both
> services) — see [Single container](#single-container-all-in-one--combined). That's the image for
> **Proxmox OCI**.

> **Windows PowerShell** doesn't support the inline `VAR=value cmd` form — set it first:
> ```powershell
> $env:HF_TOKEN = "hf_xxx"
> docker compose build
> docker compose up -d
> ```
> (`Remove-Item Env:\HF_TOKEN` to clear it afterwards.) The `VAR=value cmd …` examples elsewhere in
> this doc are bash/macOS — translate them the same way on PowerShell.
Both models are baked into the image, so a built image runs with **zero config and no runtime token** —
including memory, offline.

## What you see at startup
Logs go to `docker compose logs -f` (or stream live if you run `up` without `-d`):
- The **`llama`** container prints llama.cpp's own output — the model metadata it loaded and
  `HTTP server listening on 0.0.0.0:8081`.
- The **`orchestrator`** container prints a summary banner once bootstrap is done, then uvicorn's
  `Uvicorn running…`:
  ```
  [jarvis] ────────────────────────────────────────────────────────────
  [jarvis] Jarvis orchestrator — starting
  [jarvis]   Web UI / API : http://localhost:5000   (HTTP — add a TLS proxy for HTTPS)
  [jarvis]   Admin user   : admin (created)
  [jarvis]   Embedding    : ready
  [jarvis]   Database     : ready   (persisted in the /app/memory volume)
  [jarvis]   LLM backend  : http://llama:8081   (the 'llama' service)
  [jarvis] ────────────────────────────────────────────────────────────
  ```
  If the embedding line says `UNAVAILABLE`, set `HF_TOKEN` (and accept the Gemma license) and restart.

## The model
**Simple by default, flexible when you need it.** A default model (**Qwen3.5-2B**) is **baked into the
image**, so a fresh `docker compose up` runs with zero model configuration — no URLs, no files to place
at deploy time.

How baking works at build time: if you've put a `.gguf` in `./models`, it's copied into the image (at
`/opt/jarvis/models`). When several are present it bakes the one named by `DEFAULT_MODEL`
(`Qwen3.5-2B-Q4_K_M.gguf` by default), else the first found — so a stray larger model sitting next to the
intended one won't be baked by accident. **If `./models` is empty, the build downloads the model itself**
from `LLM_GGUF_URL` and verifies `LLM_GGUF_SHA256` — so you can build with nothing local. Either way the
image is self-contained and portable afterwards.

### Using your own model
The baked default sits at a **separate path** from the `./models` mount, so overriding is purely
additive — your model is used without removing or hiding the default, and no rebuild is needed:
1. Drop the `.gguf` under `./models/` (e.g. `./models/llama3.2-3b/Llama-3.2-3B-Instruct-Q4_K_M.gguf`).
2. Set `LLM_MODEL=llama3.2-3b/Llama-3.2-3B-Instruct-Q4_K_M.gguf` in `.env` (path relative to `./models`).
3. `docker compose up -d` (only the `llama` container restarts).

(Or, instead of placing the file, set `LLM_GGUF_URL` + `LLM_GGUF_SHA256` to fetch your override on first
run — verified by SHA-256.) Leaving `LLM_MODEL` blank always falls back to the baked-in default.

Tune `LLM_CTX` (context window — bigger needs more RAM) and `LLAMA_THREADS` in the same file. The chat
template comes from the GGUF's own metadata, so most instruct models work as-is. Note: the default
system prompt in `config/jarvis.docker.json` ends with Qwen's `/no_think` token — harmless on other
models, but you can drop it if you switch families.

> Trade-off: baking the model makes the image larger (the GGUF rides inside it). That's the price of
> zero-config — worth it here since you build and run locally rather than pushing to a registry.

## Tuning inference
Nothing is locked down — three layers, none needing a rebuild:

- **`config/jarvis.json`** (mounted; edit + `docker compose restart orchestrator`):
  - `default_temperature`, `max_context_tokens`, `request_timeout_seconds`.
  - `reasoning` — `true` = thinking on, `false` = off (toggles the Qwen `/no_think` token for you), omit
    to leave `system_prompt` exactly as written.
  - `sampling` — any of `top_k`, `top_p`, `min_p`, `repeat_penalty`, `presence_penalty`,
    `frequency_penalty`, `max_tokens`, `seed`. Anything you omit uses llama.cpp's own default. e.g.
    ```json
    "sampling": { "top_k": 40, "top_p": 0.9, "repeat_penalty": 1.1 }
    ```
  - `system_prompt` — the persona/instructions.
- **`.env`** (server-level): `LLM_CTX` (context window), `LLAMA_THREADS`, and `LLAMA_EXTRA_ARGS` for any
  other llama-server flag (`--mlock`, `--n-gpu-layers`, …). Changing these restarts the `llama` container.
- These apply to native installs too — the same `config/jarvis.json` keys work outside Docker.

## Embedding model (memory) — baked in
Long-term memory/RAG needs an embedding model. It's **baked into the image** so it works **offline at
runtime with no HF token** (like the native box). Default: `google/embeddinggemma-300m`.

Bake it at build time — two ways:
- **Token secret (simplest):** `HF_TOKEN=hf_xxx docker compose build`. The token is passed as a build
  **secret** (never stored in the image) and the model is downloaded + baked. The default is **gated** —
  accept the license at <https://huggingface.co/google/embeddinggemma-300m> with that token's account,
  and make sure the token can read gated repos.
- **Pre-download (robust on a flaky network):** `bash src/scripts/prepare_embed_cache.sh` fills
  `./embed-cache/` once (resumable); then `docker compose build` just copies it in — a fully offline
  build, no token at build.

Bake neither and the build still succeeds — the model is fetched **at runtime** instead (needs `HF_TOKEN`).

> **Cache gotcha:** BuildKit excludes the token secret from the layer cache, so if a build already ran
> *without* a token (caching the "not baked" result), simply re-running with the token won't re-bake —
> the step stays `CACHED`. Force it: change `embed-cache/` contents, or `docker compose build
> --no-cache` (heavier — also rebuilds llama/LLM). Setting the token on the **first** build avoids this.

**Use a different embedding model:** set `EMBED_MODEL` (it's both a build arg and a runtime value). A
**non-gated** model (e.g. `BAAI/bge-small-en-v1.5`) needs **no token at all**. Changing it **re-indexes**
memory (different vector space) and may need different prefixes (`embedding.doc_prefix`/`query_prefix`
in `jarvis.json`).

> **License:** embeddinggemma is under the **Gemma Terms of Use** (not open-source). Shipping an image
> with it baked in carries redistribution obligations — the required NOTICE + Terms + Prohibited Use
> Policy live in `licenses/gemma/` and are baked into the image. To avoid them, pick a permissive
> `EMBED_MODEL`.

## Admin login & API keys
Nothing to bootstrap at build time — there's no master key or signing secret. The admin account is
created on first start from `ADMIN_USER` / `ADMIN_PASS`, which **default to `admin` / `admin`** so the
stack runs with zero config. That default is **insecure** — set `ADMIN_PASS` (CLI, `.env`, or
`docker run -e`) for anything reachable beyond your machine; the banner warns while the default is in
use. Everything else is minted at runtime and stored (hashed) in the DB volume:

```bash
# a user/device API key (jk-…), printed once:
docker compose exec orchestrator uv run python src/scripts/manage.py mint-key <username> "my laptop"
# bind a key to a device (agents): add the device id as a third arg
docker compose exec orchestrator uv run python src/scripts/manage.py mint-key <username> "camera" cam-01
```

Or mint them from the **admin UI**. The startup banner reprints the `mint-key` command as a reminder.

## How configuration works
Layers — tunables in env (all defaulted), app settings in a file, data in volumes:

1. **Env vars** (all optional — every one has a default in `docker-compose.yml`). Set them on the CLI
   (`HF_TOKEN=… docker compose up`), in an **optional** `.env`, or via `docker run -e`:
   `HF_TOKEN` (memory; default off), `ADMIN_USER`/`ADMIN_PASS` (default `admin`/`admin`), `HOST_PORT`,
   `LLM_MODEL`, `LLM_CTX`, `LLAMA_THREADS`, `LLAMA_IMAGE`, plus combined-image build args `LLM_GGUF_URL`/`LLM_GGUF_SHA256`.
2. **`config/jarvis.json`** — app settings. On first run the entrypoint copies it from
   `config/jarvis.docker.json` (relative paths + `fast_brain_url=http://llama:8081`). Edit it on the
   host and restart to change settings. Override the location with `JARVIS_CONFIG` if you prefer.
3. **Volumes** — `jarvis-data` (SQLite DB + ChromaDB vectors), `hf-cache` (embedding model),
   `./models` (GGUF), `./config` (your `jarvis.json`), `./tls` (optional certs for HTTPS).

Why a file and not pure env vars: paths derive from `JARVIS_HOME` (`/app` in the image) and relative
config paths resolve against it, so the same image is portable without rewriting the app to be
12-factor. Env vars cover the few things that are genuinely per-deployment (secrets, the admin seed).

All of this lives **on the host, not in the image** — `.env`, `config/jarvis.json`, and the volumes are
read at `docker compose up`, never baked in. So you build once and change config freely: edit `.env`
(or `jarvis.json`) and `docker compose up -d` again — **no rebuild**. (That's also why the image is safe
to share — it carries no secrets.) Admin creds are a first-run *seed*; to change an existing admin's
password later, use `manage.py reset-password` (above), not `.env`.

## Single container (all-in-one / combined)
Prefer **one** container over the split? The **`jarvis-combined`** image runs both llama-server and the
orchestrator in one container (they talk over loopback — no Docker network or `llama` hostname). Its
**default entrypoint** is already all-in-one, so no `--entrypoint` override — and it works where you
*can't* override the entrypoint (e.g. **Proxmox VE 9.1 OCI containers**, which run the image's default):
```bash
docker run --init -p 5000:5000 --restart unless-stopped \
  -e ADMIN_PASS=secret -v jarvis-data:/app/memory \
  ghcr.io/<owner>/jarvis-combined:latest
```
`all-in-one.sh` starts the (official, prebuilt) `llama-server` on `127.0.0.1:8081`, points the orchestrator
at it (via `JARVIS_FAST_BRAIN_URL`), and supervises both — mirroring the native box. `--init` gives a proper
PID 1 that reaps children; `--restart unless-stopped` recovers if a service dies. All the usual env vars
apply (`ADMIN_PASS`, `EMBED_MODEL`, `LLM_CTX`, …).

`jarvis-combined` is built **on** the official `ggml-org/llama.cpp:server` image (its prebuilt all-variant
`llama-server`), with the orchestrator + baked LLM & embedding layered on — self-contained, no compile.

Trade-offs vs the split: simplest to run, but **no independent restart** (if either service dies the
container exits — rely on the restart policy) and logs interleave. Fine for single-node/personal use.

## Two-image split (default `docker-compose.yml`)
The **production shape** — and the compose default — two images with independent lifecycles:
- **`llama`** → the **official** `ghcr.io/ggml-org/llama.cpp:server` image — nothing to build; point it at a GGUF.
- **`orchestrator`** → **`ghcr.io/<owner>/jarvis-orchestrator`** (slim: FastAPI + UI + embeddings + TTS, **no LLM**).

They talk over the compose network (`orchestrator → http://llama:8081`); the orchestrator waits on the
llama `/health` check before starting. Run it — pull the published images, or `--build` the orchestrator:
```bash
bash src/scripts/download_models.sh              # GGUF → ./models (for the llama service)
docker compose pull && docker compose up -d      # (or: up -d --build  to build the orchestrator locally)
```
Set `LLM_MODEL` if your GGUF's filename differs from `Qwen3.5-2B-Q4_K_M.gguf`; `LLAMA_IMAGE` to pin the llama
tag; `EMBED_MODEL`, `ADMIN_PASS`, `HF_TOKEN`, `LLM_CTX`, `LLAMA_THREADS`, `HOST_PORT` apply as usual.

**When to use which:**
| Shape | Best for |
| --- | --- |
| `jarvis-combined` (1 container) | simplest personal/single-node use; **Proxmox OCI** |
| split, 2 services (`docker-compose.yml`) | production: official llama + slim app, independent updates/scaling |

Both images use the official all-CPU-variant `llama-server`, so both run on **any x86-64 (AVX, AVX2,
AVX-512)** — including the old AVX-only box. Details: [CPU / architecture support](#cpu--architecture-support-portability).

## Running without Compose (optional)
Compose just wraps `docker run`. By hand:

**One container** — `jarvis-combined` (simplest):
```bash
docker run -d --name jarvis --init -p 5000:5000 --restart unless-stopped \
  -e ADMIN_PASS=secret -v jarvis-data:/app/memory \
  ghcr.io/<owner>/jarvis-combined:latest
```

**Two containers** — the split (official llama + orchestrator on a shared network; the orchestrator
resolves the llama container by name, `http://llama:8081`):
```bash
docker network create jarvis

# LLM: the official image + your GGUF
docker run -d --name llama --network jarvis \
  -v "$PWD/models:/models:ro" \
  ghcr.io/ggml-org/llama.cpp:server \
  -m /models/Qwen3.5-2B-Q4_K_M.gguf -c 4096 -t 4 --host 0.0.0.0 --port 8081 --parallel 1

# Orchestrator — defaults to admin/admin; add -e to override
docker run -d --name jarvis-orchestrator --network jarvis \
  -p 5000:5000 -e ADMIN_PASS=secret \
  -v "$PWD/config:/app/config" -v jarvis-data:/app/memory \
  ghcr.io/<owner>/jarvis-orchestrator:latest
```
Same result as `docker compose up`. (Windows PowerShell: use `${PWD}` for the paths.)

## HTTPS / certificates
Two options:

- **Reverse proxy (recommended for anything public)** — keep the orchestrator on HTTP and put Caddy /
  Traefik / nginx in front to terminate TLS. Point the agents at the proxy URL.
- **Built-in TLS (LAN, local CA)** — reuse the native local-CA flow, then mount the certs:
  ```bash
  bash src/scripts/setup_tls.sh        # creates tls/{ca.crt,server.crt,server.key}
  docker compose up -d                 # tls/ is already mounted at /app/tls
  ```
  When `tls/server.crt` + `tls/server.key` are present, the entrypoint serves **HTTPS** automatically
  (the banner shows `TLS: on` and an `https://` URL). Devices fetch the CA from `GET /ca.crt` and pin its
  fingerprint, exactly as in the native setup. No certs mounted → HTTP (the default).

## CPU / architecture support (portability)
The stack runs on **any x86-64 CPU with AVX — roughly 2011 onward, old or new** — including the deployment
box (Sandy-Bridge i5: AVX, no AVX2). Audited per component:

| Component | Floor | Notes |
| --- | --- | --- |
| **LLM (llama.cpp)** | any x86-64 (even no-AVX) | We use the **official** `llama.cpp:server` binary, built `GGML_CPU_ALL_VARIANTS` + `GGML_BACKEND_DL` (upstream [`.devops/cpu.Dockerfile`](https://github.com/ggml-org/llama.cpp/blob/master/.devops/cpu.Dockerfile)) — ggml auto-loads the best CPU backend at runtime; no rebuild, no illegal-instruction crash. |
| **Embedding / memory (PyTorch)** | **AVX** | Official torch CPU wheels require AVX (the AVX2-minimum proposal [pytorch#94021](https://github.com/pytorch/pytorch/issues/94021) is still open/unimplemented). On a CPU *without* AVX, torch illegal-instructions → **memory/RAG won't load, but chat/LLM still works**. |
| **TTS (Piper)** | x86-64 (prebuilt) | `piper_linux_x86_64`; if it can't run it degrades gracefully — TTS off, everything else fine. |
| **API / UI / Python** | arch-generic | pure Python on the amd64 base. |

**Architecture:** the image is `linux/amd64`. **ARM** (Apple Silicon, Raspberry Pi) needs an arm64 build
(`docker buildx --platform linux/arm64`); the official llama image is already multi-arch, but the
orchestrator image would need rebuilding.

**Bottom line:** any x86-64 from ~2011 (AVX) runs the *whole* stack, no flags. Pre-AVX x86-64 loses
**memory only** (chat still works). ARM needs a rebuild.

## Publishing the images
Two images are published — **`jarvis-combined`** and **`jarvis-orchestrator`** (roles + tags:
**[image-releases.md](image-releases.md)**). Tags track the repo version (git `vX.Y.Z` → image `X.Y.Z` +
`latest`). We do **not** publish llama.cpp — that's the upstream `ggml-org/llama.cpp:server` image.

### Build + push on GitHub Actions (no upload from your machine)
`.github/workflows/build-push.yml` builds **on GitHub's runners** (fast link) and pushes to GHCR — two
parallel jobs, one per image. One-time setup:
1. Add a repo secret **`HF_TOKEN`** (Settings → Secrets and variables → Actions) — accepts the Gemma
   license + reads gated repos. (Without it the LLM still bakes; the embedding won't.)
2. Run it: Actions → **Build & push images (GHCR)** → *Run workflow* (pick a tag), or push a `v*` git tag.
3. Images land at `ghcr.io/<owner>/jarvis-combined` and `…/jarvis-orchestrator`. New packages are
   **private** by default — make them public in the package settings to pull without creds.

Each job frees ~25 GB of runner disk first, passes `HF_TOKEN` as a BuildKit secret (never in the image),
and downloads the model(s) during the build. Pushing publicly **redistributes the baked weights** — the
LLM is Apache-2.0, the embedding (Gemma) carries the Gemma Terms (bundled in `licenses/gemma/`).

## Build options
Build-time args:
- **`LLAMA_IMAGE`** (combined) — the official llama base to build on. **Pin a specific tag**
  (`ghcr.io/ggml-org/llama.cpp:server-b<NNNN>`) for reproducible production builds; `:server` floats.
- **`LLM_GGUF_URL` / `LLM_GGUF_SHA256` / `DEFAULT_MODEL`** (combined) — the LLM baked in, SHA-verified.
- **`EMBED_MODEL`** (both) — the embedding model baked in.

Rebuild: `docker compose build --no-cache` (orchestrator), or re-run the Actions workflow.

## Notes / known rough edges (verify on first build)
- **Image size**: CPU **torch** (embeddings) plus the **baked-in model** make the image large (several GB
  + the GGUF). Both containers share the one image (cached once on disk); the `llama` container simply
  doesn't use the Python side. Fine for local build-and-run; heavy if you push it to a registry.
- **Build context**: `./models` is sent to the daemon (that's how the GGUF gets baked) — keep only the
  model you want as the default there.
- **llama.cpp build**: compiled statically (`BUILD_SHARED_LIBS=OFF`) so the runtime stage needs only the
  single `llama-server` binary. If a future llama.cpp release relocates the binary or needs extra libs,
  the build will say so — adjust the `COPY --from=native` line.
- **Piper** is fetched at build time (`piper_setup.sh`); if that step fails the build continues and TTS
  is simply unavailable until fixed.
- **GGUF path**: the `llama` command hard-codes `qwen3.5_2b/Qwen3.5-2B-Q4_K_M.gguf` — change it in
  `docker-compose.yml` if you use a different model file.
- **Agents reach the server** over your LAN/VPN at the published `:5000` (HTTP) — point them at the
  proxy URL if you add TLS.
