# Install: orchestrator image + official llama.cpp (two containers)

**`ghcr.io/ravijangid820/jarvis-orchestrator`** — the slim app image (FastAPI + UI + embeddings + TTS,
**no LLM inside**), paired with the **official** `ghcr.io/ggml-org/llama.cpp:server` image as a separate
service. This is the production split: each side updates, restarts, and scales independently, and the
llama side can be swapped for a GPU build or moved to another machine without touching the app.

**Pick this if** you want the production topology — or plan to run the LLM on different hardware
(e.g. the official `:server-cuda` image on a GPU box).

> ⚠ Not standalone: this image has **no LLM** — run it with a llama backend, as below.

## Prerequisites
- Docker with compose, ~3–4 GB RAM free, ~2 GB disk for the model.

## Install (compose — recommended)
```bash
git clone https://github.com/Ravijangid820/jarvis.git && cd jarvis

# optional: persist overrides in a file (both Docker and the repo scripts read it)
cp .env.example .env         # then edit — e.g. set ADMIN_PASS

bash src/scripts/download_models.sh    # puts the pinned GGUF in ./models (llama service reads it)
docker compose pull                    # official llama.cpp:server + jarvis-orchestrator
docker compose up -d
docker compose logs -f
```
Open **http://localhost:5000** — login `admin`/`admin` unless you set `ADMIN_PASS`.

The orchestrator waits for the llama service's `/health` before starting (compose gate), so first
startup is ordered automatically.

## Configuration
Everything defaults; override in `.env` or inline (`ADMIN_PASS=secret docker compose up -d`):
`ADMIN_USER`/`ADMIN_PASS`, `LLM_MODEL` (GGUF filename in `./models`), `LLM_CTX`, `LLAMA_THREADS`,
`LLAMA_IMAGE` (pin the llama tag, or use `:server-cuda` on a GPU host), `EMBED_MODEL`, `HOST_PORT`.
Details: [docker.md](docker.md).

### If your UI is served from somewhere else — set `ALLOWED_ORIGINS`

Only relevant when the page and the API are on **different origins**: a GitHub Pages site, a
separate nginx, a Vite dev server. The bundled UI is served by this same container with relative
URLs, so a normal install needs nothing here.

```bash
-e ALLOWED_ORIGINS=https://your-site.example      # exact origin: scheme + host, no path, no slash
```

**As of 3.4.0 an empty value means "allow nothing".** Up to 3.3.0 it fell back to `*`, so
cross-origin front ends worked by accident and most people never set it — upgrading without setting
it blocks every call, and the browser reports it as a CORS error. Comma-separate several origins.

Verify (a stranger's origin must print nothing, yours must echo back):

```bash
curl -sD - -o /dev/null -H "Origin: https://your-site.example" http://<host>:5000/health \
  | grep -i access-control-allow-origin
```

The variable is read at process start, so it needs the container **recreated**, not just restarted.
Full reference and a triage table for "the page cannot reach the API":
[docker.md](docker.md#environment-variables).

### Pointing at the LLM by hand

The default `JARVIS_FAST_BRAIN_URL` names the **Compose service** (`http://llama:8081`). That
hostname does not resolve on a network you built yourself, so `docker run`, Portainer and Proxmox
OCI containers must set it explicitly, or `/health` reports `"status":"offline"`:

```bash
-e JARVIS_FAST_BRAIN_URL=http://<llama-container-ip>:8081
```

## Verify
Log in → **Admin → System Services** → `N/N operational`, LLM row green with the loaded model. Send a chat.

## Update
```bash
docker compose pull && docker compose up -d     # each image updates independently
```

## Notes
- A new llama.cpp release = `LLAMA_IMAGE` bump — nothing to rebuild.
- Build the orchestrator locally instead of pulling: `docker compose up -d --build`.
- Manual `docker run` equivalent (no compose): [docker.md → Running without Compose](docker.md#running-without-compose-optional).
- Published tags: [image-releases.md](image-releases.md).
