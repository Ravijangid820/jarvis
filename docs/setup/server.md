# Server setup (orchestrator + web UI)

The Jarvis server — FastAPI orchestrator, the React chat UI, SQLite + ChromaDB, and the local
LLM (llama.cpp) — runs on the Proxmox LXC. This is the first-time setup; for *re-deploying*
changes and network/TLS, see [DEPLOY.md](../DEPLOY.md).

> Run these in the repo root on the box. Python tooling goes through **`uv`**.

## One-shot bootstrap (fresh clone)

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

```bash
# LLM backend (from your llama.cpp build):
<repo>/llama.cpp/build/bin/llama-server -m <gguf> -c 4096 --host 127.0.0.1 --port 8081
# Orchestrator (dev):
cd src/orchestrator && uv run uvicorn main:app --host 127.0.0.1 --port 5000
# …or install the systemd units (systemd/) and use them — see DEPLOY.md.
curl http://localhost:5000/health
```

## Next

- **Deploying changes / network / TLS / firewall:** [DEPLOY.md](../DEPLOY.md).
- **Edge devices that talk to this server:** the [Raspberry Pi agent](raspberry-pi.md) and the
  [Windows volume agent](volume-agent.md) authenticate with machine API keys
  (`manage.py mint-key <user> <name>`).
