# Server setup (orchestrator + web UI)

The Jarvis server — FastAPI orchestrator, the React chat UI, SQLite + ChromaDB, and the local
LLM (llama.cpp) — runs on the Proxmox LXC. This is the first-time setup; for *re-deploying*
changes and network/TLS, see [DEPLOY.md](../DEPLOY.md).

> Run these in the repo root on the box. Python tooling goes through **`uv`**.

## One command (fresh box → running over HTTPS)

```bash
git clone https://github.com/Ravijangid820/jarvis.git /srv/jarvis && cd /srv/jarvis
sudo bash src/scripts/setup-server.sh
#   bootstrap + systemd services + local-CA HTTPS, in order.
#   Options: JARVIS_USER=jarvis (default) · SKIP_TLS=1 · ADMIN_USER=… ADMIN_PASS=… LLM_GGUF_URL=… HF_TOKEN=…
curl --cacert tls/ca.crt https://127.0.0.1:5000/health        # verify
```

That's the whole server. The scripts below are what it runs — use them if you want to do a step
piecemeal.

## Or the bootstrap only (no services / TLS)

```bash
bash src/scripts/setup.sh
#   uv sync · config from example · frontend build · DB init · admin user · native builds · models
#   Toggles: SKIP_NATIVE=1  SKIP_MODELS=1  ADMIN_USER=… ADMIN_PASS=…
```

## Or the individual steps

```bash
uv sync                                            # Python env from pyproject + uv.lock
cp config/jarvis.example.json config/jarvis.json   # then review it (host, model paths)
bash src/scripts/build_native.sh                   # whisper.cpp + llama.cpp (AVX-only; see notes)
bash src/scripts/download_models.sh                # embedding (HF) · Piper · whisper base.en · LLM GGUF
(cd frontend && npm ci && npm run build)           # SPA bundle served at /
uv run python src/scripts/manage.py create-admin <user> <pass>
```

- The **LLM GGUF source isn't pinned** — set `LLM_GGUF_URL=<url>` for the download, or drop the
  file under `models/`. The embedding model is gated (Gemma license): accept its terms and
  `uv run huggingface-cli login` (or set `HF_TOKEN`).
- Data paths are repo-relative by default (DB/vectors land under the checkout); absolute paths in
  `jarvis.json` are used as-is. See [SPECS.md](../SPECS.md) for the full config + schema reference.

## Run it

Pick one:

**A. Quick / dev — any user, no systemd.** Run the two processes directly:
```bash
# LLM backend:
llama.cpp/build/bin/llama-server -m models/<your-model>.gguf -c 4096 --host 127.0.0.1 --port 8081 &
# Orchestrator:
(cd src/orchestrator && uv run uvicorn main:app --host 127.0.0.1 --port 5000)
curl http://localhost:5000/health
```

**B. Install as systemd services — your choice of user:**
```bash
sudo bash src/scripts/install_services.sh                      # run as ROOT (simplest)
sudo JARVIS_USER=jarvis bash src/scripts/install_services.sh   # dedicated NON-ROOT user (hardened — recommended)
```
The installer works from any checkout path: it auto-detects the repo, `uv`, the `llama-server`
binary and the GGUF, generates both unit files for the chosen mode, then enables + starts +
health-checks. The non-root mode also creates the user, moves the model cache under the repo,
narrows write access to the data dirs (source + `.git` stay read-only to the service), and runs
`llama-server` non-root too. Useful env vars:
- `DRY_RUN=1` — write the units to `systemd/generated/` and stop (preview, no root needed).
- `JARVIS_GGUF=<path>` — pick the model if you have more than one under `models/`.
- `JARVIS_HOST=` / `JARVIS_PORT=` — bind address / port (default `0.0.0.0:5000`).

## Next

- **Deploying changes / network / TLS / firewall:** [DEPLOY.md](../DEPLOY.md).
- **Edge devices that talk to this server:** the [Raspberry Pi agent](camera.md) and the
  [Windows volume agent](volume-agent.md) authenticate with machine API keys. **Bind each key to
  its device** so it can only pull/post for that device:
  `manage.py mint-key <user> <description> <device_id>` (e.g. `… volume-agent laptop`).
