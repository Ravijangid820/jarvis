# Scripts — what to run, what's a helper

Only **three** of these are commands you type; the rest are helpers they call, or separate tools.

## Entry points (you run these)

| Script | When | What it does |
|---|---|---|
| **`setup.sh`** | dev box / Codespace / anything without systemd | Bootstraps everything (env, config, frontend, DB, **admin — default `admin`/`admin`**, native build, models), then **runs both services** (Ctrl-C stops them). `SKIP_RUN=1` to bootstrap only. |
| **`run.sh`** | already set up — just start it | Runs llama-server + the orchestrator together in the foreground (the native counterpart of the container's all-in-one). |
| **`setup-server.sh`** | the real (systemd) box, as root | `setup.sh` bootstrap → installs + starts the **systemd** units → local-CA **HTTPS**. On a no-systemd box it stops after the bootstrap and points you at `run.sh`. |

## Helpers (called by the entry points — run individually only for a redo)

| Script | Called by | Does |
|---|---|---|
| `build_native.sh` | setup.sh | Compiles llama.cpp (required, first) + whisper.cpp (optional; auto-installs SDL2; `SKIP_WHISPER=1`). RAM-aware `-j` (`BUILD_JOBS=<n>` to override). |
| `download_models.sh` | setup.sh | LLM GGUF (pinned Qwen3.5-2B, SHA-verified) · torch-free ONNX embedding bundle (pinned, no token) · Piper · whisper model. |
| `piper_setup.sh` | download_models.sh + the Docker builds | Fetches the Piper TTS binary + voice. |
| `install_services.sh` | setup-server.sh | Installs/starts the systemd units (`llama-fast`, `jarvis-orchestrator`); creates the service user. |
| `setup_tls.sh` | setup-server.sh + the Docker entrypoint | Local CA + server cert for HTTPS. |
| `load_env.sh` | sourced by all three entry points | Loads `./.env` (same file docker compose reads); shell-set variables win. |
| `export_embed_onnx.py` | (manual, one-time) | Exports the embedding model's FULL pipeline to ONNX + verifies cosine vs torch — only needed for a *custom* `EMBED_MODEL` (the default bundle is hosted + pinned). |

## Separate tools (not part of setup)

| Script | Purpose |
|---|---|
| `run_listener.sh` | The voice listener (whisper-stream → wake word → `/inbox`). See [docs/setup/voice.md](../../docs/setup/voice.md). |
| `backup.sh` | Data backup (DB + vectors). See [docs/setup/backup.md](../../docs/setup/backup.md). |
| `manage.py` | Admin CLI: users, API keys, password resets (`uv run python src/scripts/manage.py --help`). |
| `reembed_memory.py` | One-off migration: re-embed stored memories after changing `EMBED_MODEL`. |
| `fetch_fonts.py` | Vendored-font refresh for the frontend. |
