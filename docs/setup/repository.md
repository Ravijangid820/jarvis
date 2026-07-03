# Install: from the repository (from source, no Docker)

Build and run everything natively: llama.cpp compiled for your CPU, the orchestrator under `uv`,
models downloaded (pinned + SHA-verified). This is how the real deployment box runs.

**Pick this if** you're developing, the machine has no Docker (LXC), or you want the compiled-for-this-CPU
setup with systemd services.

## Prerequisites
- **`uv`** ([install](https://docs.astral.sh/uv/getting-started/installation/)) — required.
- **`git` + `cmake` + a C compiler** (`build-essential`) — required for the native llama.cpp build.
- **node** — optional (web UI build; API works without it).
- **`HF_TOKEN`** — optional, only for memory/RAG (the default embedding model is Gemma-gated: accept its
  license on HuggingFace and use a token that can read gated repos). Chat works without it.
- ~8 GB free disk, ~4 GB free RAM for the build.

## 1. Clone and (optionally) configure
```bash
git clone https://github.com/Ravijangid820/jarvis.git && cd jarvis

# OPTIONAL — persist settings in a file instead of typing env vars each time:
cp .env.example .env      # then edit: HF_TOKEN, ADMIN_USER/ADMIN_PASS, LLM_CTX, …
```
Everything has a default, so `.env` is optional. The repo scripts **and** docker compose read the same
file; variables you set in the shell always win over `.env`.

## 2. Set up and run (one command)
```bash
bash src/scripts/setup.sh
```
This bootstraps everything — Python env, config, web UI, database, **admin login (default
`admin`/`admin`)**, the native llama.cpp/whisper build (RAM-aware parallelism), and the pinned model
downloads — then **starts both services**. Ctrl-C stops them. Open **http://localhost:5000**.

Useful toggles: `SKIP_RUN=1` (bootstrap only) · `SKIP_WHISPER=1` (no voice → no SDL2) ·
`BUILD_JOBS=<n>` (compile parallelism) · `ADMIN_USER=… ADMIN_PASS=…`.

## 3. Day-to-day
```bash
bash src/scripts/run.sh        # start both services again (Ctrl-C to stop)
```

## Run as a boot service instead (systemd box, e.g. the LXC)
```bash
sudo bash src/scripts/setup-server.sh
#  = the same bootstrap + systemd units (llama-fast, jarvis-orchestrator) + local-CA HTTPS.
#  Options: JARVIS_USER=jarvis · SKIP_TLS=1 · ADMIN_USER/ADMIN_PASS/HF_TOKEN (or put them in .env)
```
Details (service user choice, TLS, re-deploys): [server.md](server.md) · [../DEPLOY.md](../DEPLOY.md).

## Verify
Log in → **Admin → System Services** → `N/N operational`, LLM row green with the loaded model. Send a chat.

## Update
```bash
git pull && bash src/scripts/setup.sh     # idempotent — rebuilds/redownloads only what changed
```

## Notes
- Which script does what: [src/scripts/README.md](../../src/scripts/README.md).
- The build targets AVX (no AVX2) by default for the 2011 deployment box — it runs on any newer CPU too;
  adjust the `-DGGML_*` flags in `build_native.sh` to tune for modern hardware.
- Voice listener (mic + wake word) is a separate opt-in: [voice.md](voice.md).
