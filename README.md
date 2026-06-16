# Jarvis AI Assistant

A fully self-hosted, **offline** voice + text AI assistant — LLM, speech-to-text, memory, and
text-to-speech — running entirely on a 2011-era laptop in a Proxmox LXC. No cloud APIs.

> The interesting constraint: making an LLM assistant feel responsive on a **CPU with no AVX2
> and 8 GB RAM**. That shapes every decision here — model size, quantization, prompt budgeting,
> off-request-path embedding, and a single-slot scheduler.

**Stack:** FastAPI · llama.cpp (Qwen 2B) · whisper.cpp · ChromaDB + embeddinggemma RAG · Piper TTS · React 19

## Demo

> 📸 _Add a screenshot here:_ drop a UI capture at `docs/screenshot.png` (chat UI mid-stream +
> the memory panel makes a great shot) and replace this line with `![Jarvis](docs/screenshot.png)`.
> An optional demo GIF at `docs/demo.gif` sells the voice loop.

## Hardware
- **CPU**: Intel Core i5-2520M (Sandy Bridge, 2C/4T) — **AVX, no AVX2**
- **RAM**: 8 GB · **Inference**: CPU-only
- **Throughput**: Qwen 2B Q4 ≈ 5 tok/s generation

## Quick Start

### From a fresh clone (system or container)

```bash
# One-shot bootstrap: uv env, config, frontend build, DB, native engines, models.
# Idempotent. Toggles: SKIP_NATIVE=1, SKIP_MODELS=1, ADMIN_USER=… ADMIN_PASS=…
bash src/scripts/setup.sh

# Or run the pieces individually:
uv sync                                   # Python env from pyproject + uv.lock
cp config/jarvis.example.json config/jarvis.json   # then review it
bash src/scripts/build_native.sh          # whisper.cpp + llama.cpp (AVX-only)
bash src/scripts/download_models.sh       # embedding (HF) · Piper · whisper base.en · LLM GGUF
(cd frontend && npm ci && npm run build)  # SPA bundle (served at /)
uv run python src/scripts/manage.py create-admin <user> <pass>
```

Paths are repo-relative (data lands under the checkout). The **LLM GGUF source isn't pinned**
in the repo — set `LLM_GGUF_URL=<url>` for the download, or drop the file under `models/`. The
embedding model is gated (Gemma license): accept its terms and `uv run huggingface-cli login`
(or set `HF_TOKEN`).

### On an already-provisioned box

```bash
# Start both services (auto-enabled on boot)
systemctl start llama-fast jarvis-orchestrator
curl http://localhost:5000/health

# Mint an API key (no master key — see the admin CLI), then call the API
KEY=$(uv run python src/scripts/manage.py mint-key admin demo)
curl -X POST http://localhost:5000/inbox \
  -H "Content-Type: application/json" -H "Authorization: Bearer $KEY" \
  -d '{"text":"Hello Jarvis"}'

# Dev checks
uv run pytest -q && uv run ruff check src/orchestrator src/scripts tests
```

## Project Structure

```
/srv/jarvis/
├── src/orchestrator/           ← FastAPI app, split into an acyclic module graph:
│   ├── config.py               ← configuration, tunables, logging
│   ├── db.py                   ← SQLite connections + schema init
│   ├── auth.py                 ← password hashing (PBKDF2)
│   ├── llm.py                  ← LLM client (blocking/streaming) + Piper TTS
│   ├── memory.py               ← embeddings, vector store, knowledge base, fact extraction
│   ├── chat.py                 ← sessions, message persistence, prompt assembly
│   ├── budget.py               ← pure token-budgeting helpers (unit-tested)
│   ├── main.py                 ← app, auth middleware, route handlers
│   └── static/                 ← self-hosted fonts (offline, no Google Fonts)
├── src/scripts/
│   ├── run_listener.sh         ← whisper voice listener → orchestrator bridge
│   ├── manage.py               ← admin CLI (create-admin / reset-password / mint-key)
│   └── reembed_memory.py       ← one-time vector-store migration
├── frontend/                   ← React 19 + Vite chat UI
├── config/
│   ├── jarvis.example.json     ← config template (real jarvis.json is gitignored)
│   └── schema.sql              ← SQLite schema (single source of truth)
├── tests/                      ← pytest suite
├── systemd/                    ← llama-fast + jarvis-orchestrator units
├── docs/                       ← AUDIT.md, DEPLOY.md, benchmarks
└── .github/workflows/ci.yml    ← ruff + pytest on push

# gitignored runtime data: models/ (GGUF), whisper/, piper/, memory/ (DB + vectors), logs/
```

Module dependency graph (acyclic): `config → {db, auth, llm} → memory → chat → main`

## Vendored build dependencies

These live under `/srv/jarvis/` but are **gitignored** (large/built-from-source, not part of
this repo). To reproduce the box, clone and build them yourself:

| Dependency | Version | Source | Build notes |
|---|---|---|---|
| **whisper.cpp** | `v1.8.6` (commit `23ee0350`) | https://github.com/ggerganov/whisper.cpp | Built with `-DGGML_AVX=ON -DWHISPER_SDL2=ON` (Sandy Bridge has AVX but **no** AVX2) |
| **Piper TTS** | `en_GB-alan-medium` voice | https://github.com/rhasspy/piper | Installed via [src/scripts/piper_setup.sh](src/scripts/piper_setup.sh) |
| **llama.cpp** | — | https://github.com/ggml-org/llama.cpp | Built AVX-only (Sandy Bridge); binary at `/root/llama.cpp/build/bin/llama-server` |

```bash
# whisper.cpp (matches the deployed build)
git clone --branch v1.8.6 https://github.com/ggerganov/whisper.cpp /srv/jarvis/whisper
cmake -S /srv/jarvis/whisper -B /srv/jarvis/whisper/build -DGGML_AVX=ON -DWHISPER_SDL2=ON
cmake --build /srv/jarvis/whisper/build -j
```

## Architecture

```
            ┌──────────── voice ────────────┐         ┌──────── web/phone ────────┐
  speech →  whisper.cpp (base.en STT)         React 19 SPA  +  admin panel
            run_listener.sh ─┐                          │
                             ▼                          ▼
                   ┌─────────────────────────────────────────────┐
                   │   FastAPI Orchestrator  (auth · rate limit)  │
                   │   token-budgeted prompt → single LLM slot    │
                   └───────┬───────────────┬───────────────┬──────┘
                           ▼               ▼               ▼
                  llama.cpp (Qwen 2B)   SQLite          ChromaDB (cosine RAG)
                  127.0.0.1, -c 4096    history/users   embeddinggemma-300m
                           │                                  ▲
                           ▼                       idle-time fact extraction
                   Piper TTS → audio  ────────────────────────┘
```

## Key Design Decisions

| Decision | Rationale |
|---|---|
| 2B model, `--reasoning off` | Fits the 8 GB / no-AVX2 budget; disables hidden thinking chains (5–15 s vs 60 s+) |
| `-c 4096` + prompt token-budgeting | Prompt + completion is clamped to the window so the model never silently loses context |
| base.en whisper | Same accuracy as small.en at 4.4× the speed, 2.4× less RAM |
| ChromaDB cosine RAG | Semantic recall with correct embeddinggemma query/document prefixes |
| Embedding off the request path | A 300M model on no-AVX2 is slow → a background worker embeds so chat never blocks |
| Single LLM slot + in-flight guard | The idle fact-extractor never competes with a live generation for the one core pair |
| Per-user API keys (no master key) | Auth is web-login sessions or revocable `api_keys`; a local CLI handles recovery |

## Security

- All inference local (no cloud APIs); LLM server bound to `127.0.0.1`
- Auth = web-login session tokens **or** per-user API keys (no static admin secret)
- Rate limiting per user; parameterized SQL; input validation; security headers
- Reachable over LAN + Tailscale; see [docs/DEPLOY.md](docs/DEPLOY.md) for the firewall posture
- Full self-audit + fixes in **[docs/AUDIT.md](docs/AUDIT.md)**

## Engineering highlights

This project was hardened via a **multi-agent self-audit** that surfaced 81 issues (see
[docs/AUDIT.md](docs/AUDIT.md)); the large majority — every critical/high — were fixed,
deployed, and verified live. Highlights worth a look:

- **Found & fixed a silent context-window bug**: the server ran a 1024-token window while the
  app stuffed in 100 messages + memory, so prompts overflowed and the model lost the question.
  Fixed with a char-based token budget that clamps history and completion to the window.
- **Memory that actually recalls**: cosine space + the embedding model's required asymmetric
  prefixes, semantic fact dedup, and user-scoped retrieval.
- **Eliminated the last ambient admin secret** (a static master key) in favour of revocable
  per-user keys + a local admin CLI.
- **Refactored a 1,300-line `main.py`** into an acyclic module graph, under a pytest + ruff + CI
  safety net.

## Documentation

Full docs live in **[docs/](docs/README.md)**:

| Doc | What's in it |
|---|---|
| [Architecture](docs/ARCHITECTURE.md) | Components, module graph, design decisions, security model |
| [Workflows](docs/WORKFLOWS.md) | Chat lifecycle, prompt token-budgeting, RAG, fact extraction, voice loop |
| [API Reference](docs/API.md) | Every HTTP endpoint, auth, request/response shapes |
| [Specs](docs/SPECS.md) | Hardware, models, performance, config reference, DB schema |
| [Deploy](docs/DEPLOY.md) | Runbook: units, migrations, firewall, the admin CLI |
| [Audit](docs/AUDIT.md) | The 81-finding self-audit and fixes |

## Status

- ✅ LLM Layer (Qwen3.5-2B, llama.cpp, `-c 4096` with prompt token-budgeting)
- ✅ Speech-to-Text (whisper.cpp, base.en)
- ✅ Orchestrator (FastAPI, modular + secured + tested)
- ✅ Memory (SQLite + ChromaDB cosine RAG, embeddinggemma-300m, background embedding)
- ✅ Text-to-Speech (Piper TTS — wired into both `/inbox` and the streaming web UI)
- ⬜ Tool / function calling · ⬜ Home Automation (MQTT + Home Assistant)

Checks: `uv run pytest` · `uv run ruff check src/orchestrator src/scripts tests`.
