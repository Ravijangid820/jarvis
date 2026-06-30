# Docker (server stack)

Run the **server** — orchestrator + llama.cpp — in containers. The **camera agent, volume agent, and
voice listener run natively** on the machines that have the camera / mic / speaker; they are not part of
this compose (install them with `docs/setup/camera.md`, `volume-agent.md`, `voice.md`).

> **Status: initial.** These files are authored but not yet build-tested on this box (the production
> host is an LXC without Docker). Build on a Docker host, then iterate — `docker compose build` and
> report errors; expect a short pass to green.

## What runs
**One image, two services.** The `Dockerfile` builds a single image containing the orchestrator **and**
a from-source `llama-server` (compiled with controllable CPU flags, mirroring the native
`build_native.sh` build — no dependency on a prebuilt llama image). Compose runs that one image twice:

| Service | Role |
| --- | --- |
| `llama` | Runs the compiled `llama-server`: loads the GGUF, serves the OpenAI-style API on `:8081` (internal only). |
| `orchestrator` | FastAPI API + built React UI + Piper TTS + embeddings. Serves **HTTP** on `:5000`. |

Why from source (not the official `ghcr.io/ggml-org/llama.cpp:server` image): this project deliberately
builds llama.cpp with explicit GGML flags so it runs on a chosen CPU baseline. Compiling in-image keeps
that control and gives parity with the native install. The cost is a longer first build — which is what
a powerful build host is for.

## Prerequisites
- **Docker + Compose** on the build host. The image's llama.cpp is built with **all CPU variants** and
  auto-detects the best one for the host at runtime — so it runs on AVX-only and AVX2 machines alike,
  no flag to set. (The 2011 production box has no Docker and stays on the native
  `src/scripts/setup-server.sh` build.)
- A **model** — nothing to do by default: `.env.example` ships a verified `LLM_GGUF_URL` for
  Qwen3.5-2B, so an empty `./models` auto-downloads + SHA-verifies it at build. To use your own instead,
  drop a `.gguf` in `./models` (it takes precedence) or point `LLM_GGUF_URL` elsewhere. See
  [The model](#the-model).
- A **HuggingFace token** for the gated Gemma embedding model — accept its license first
  (<https://huggingface.co/google/embeddinggemma-300m>).

## Run
```bash
cp .env.example .env          # set HF_TOKEN, ADMIN_USER, ADMIN_PASS (model URL is already set)
docker compose up -d --build  # build downloads + bakes the model (~1.3 GB) and compiles llama-server
docker compose logs -f        # watch the banner + first-run embedding download
curl http://localhost:5000/health
```
The model is baked into the image, so anyone running it gets the LLM with **zero config**. First start
also downloads the embedding model into the `hf-cache` volume (needs `HF_TOKEN`); both are cached after.

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

## Admin login & API keys
Nothing to bootstrap at build time — there's no master key or signing secret. The admin account is
created on first start from `ADMIN_USER` (default `admin`) and `ADMIN_PASS`. **Leave `ADMIN_PASS` blank
and the entrypoint generates a strong one and prints it once in the startup banner** (`docker compose
logs orchestrator`) — set it in `.env` to pin your own. Everything else is minted at runtime and stored
(hashed) in the DB volume:

```bash
# a user/device API key (jk-…), printed once:
docker compose exec orchestrator uv run python src/scripts/manage.py mint-key <username> "my laptop"
# bind a key to a device (agents): add the device id as a third arg
docker compose exec orchestrator uv run python src/scripts/manage.py mint-key <username> "camera" cam-01
```

Or mint them from the **admin UI**. The startup banner reprints the `mint-key` command as a reminder.

## How configuration works
Layers — secrets/bootstrap + tunables in env, app settings in a file, data in volumes:

1. **`.env`** (git-ignored, compose reads it) — `HF_TOKEN`, `ADMIN_USER`/`ADMIN_PASS`, `HOST_PORT`,
   `LLM_MODEL`, `LLM_GGUF_URL`/`LLM_GGUF_SHA256`, `LLM_CTX`, `LLAMA_THREADS`, and the build arg
   `LLAMA_CPP_REF`.
2. **`config/jarvis.json`** — app settings. On first run the entrypoint copies it from
   `config/jarvis.docker.json` (relative paths + `fast_brain_url=http://llama:8081`). Edit it on the
   host and restart to change settings. Override the location with `JARVIS_CONFIG` if you prefer.
3. **Volumes** — `jarvis-data` (SQLite DB + ChromaDB vectors), `hf-cache` (embedding model),
   `./models` (GGUF), `./config` (your `jarvis.json`), `./tls` (optional certs for HTTPS).

Why a file and not pure env vars: paths derive from `JARVIS_HOME` (`/app` in the image) and relative
config paths resolve against it, so the same image is portable without rewriting the app to be
12-factor. Env vars cover the few things that are genuinely per-deployment (secrets, the admin seed).

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

## CPU portability
The llama.cpp build uses `GGML_CPU_ALL_VARIANTS` + `GGML_BACKEND_DL`: it compiles one set of CPU backend
plugins (SSE4.2, AVX, AVX2, AVX-512, …) and **ggml loads the best one for the host CPU at runtime**. So a
single `linux/amd64` image runs on AVX-only and AVX2 machines without rebuilding and without
illegal-instruction crashes, while still using AVX2/AVX-512 when the CPU has them. No flag to set.
(ARM hosts — Apple Silicon, Pi — still need an arm64 build via `buildx`.)

## Publishing the image
`docker compose build` produces `jarvis-server:local`; tag and push it from the CLI:
```bash
docker compose build
docker login                       # Docker Hub  (or: docker login ghcr.io)
docker tag jarvis-server:local <you>/jarvis-server:0.1
docker push <you>/jarvis-server:0.1
```
Notes: the image is large (~5–7 GB — CPU torch + baked model), so the push is slow; **GHCR** handles big
images better than Docker Hub. Pushing publicly **redistributes the baked model weights** — fine for
Apache-2.0 models, but confirm the license first. The image is `linux/amd64` + runs on any x86-64 CPU
(see below); ARM needs a separate `buildx` build.

## Build options
Both services share one image (a YAML anchor), built once. Build-time arg (set in `.env`):
- `LLAMA_CPP_REF` — pin the llama.cpp upstream release for a reproducible build (recommended). Unset
  builds upstream HEAD with a warning.

Rebuild after changing it: `docker compose build --no-cache` (or just `up -d --build`).

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
