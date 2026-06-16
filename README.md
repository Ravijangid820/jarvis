# Jarvis AI Assistant

A fully self-hosted, secure, offline-capable AI assistant running inside a Proxmox LXC container.

## Hardware
- **CPU**: Intel Core i5-2520M (Sandy Bridge, 2C/4T)
- **RAM**: 8 GB
- **Inference**: CPU-only (AVX, no AVX2)

## Quick Start

```bash
# Start both services (auto-enabled on boot)
systemctl start llama-fast jarvis-orchestrator

# Test health
curl http://localhost:5000/health

# Send a query (replace <API_KEY> with your key from config/jarvis.json)
curl -X POST http://localhost:5000/inbox \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <API_KEY>" \
  -d '{"text":"Hello Jarvis"}'
```

## Project Structure

```
/srv/jarvis/                    ← This repository (source code + docs)
├── README.md                   ← You are here
├── docs/
│   ├── Jarvis_Project_Documentation.md  ← Full project documentation
│   └── benchmarks/             ← Whisper & LLM benchmark results
├── src/
│   ├── orchestrator/
│   │   ├── main.py             ← FastAPI orchestrator (auth, memory, LLM proxy)
│   │   └── requirements.txt    ← Python dependencies
│   └── scripts/
│       └── run_listener.sh     ← Whisper voice listener → orchestrator bridge
├── config/
│   ├── jarvis.json             ← API key, model config, system prompt
│   └── schema.sql              ← SQLite database schema (FTS5)
└── systemd/
    ├── llama-fast.service      ← 2B LLM server (127.0.0.1:8081)
    └── jarvis-orchestrator.service  ← FastAPI (0.0.0.0:5000)

/srv/ai/                        ← Runtime data (models, DB, logs, whisper binary)
├── models/                     ← GGUF model files
├── memory/                     ← SQLite database (jarvis.db)
├── logs/                       ← Runtime logs
├── whisper/                    ← whisper.cpp (built from source)
└── orchestrator/               ← Deployed orchestrator (venv + code)
```

## Architecture

```
Voice → whisper-command ("Jarvis") → whisper.cpp STT
    → run_listener.sh → FastAPI Orchestrator (API key auth)
    → Qwen3.5-2B (--reasoning off) → SQLite Memory
    → JSON Response → [Future: Piper TTS → Speaker]
```

## Key Design Decisions

| Decision | Rationale |
|---|---|
| 2B model only (4B stored) | 8 GB RAM budget; 2B is fast enough for voice commands |
| `--reasoning off` | Disables hidden thinking chains; 5-15s vs 60+ seconds |
| base.en whisper | Same accuracy as small.en, 4.4x faster, 2.4x less RAM |
| API key auth | Orchestrator accessible from network (laptop, phone) |
| SQLite FTS5 | Full-text search for memory; no external dependencies |
| 127.0.0.1 for LLM | LLM server only reachable through orchestrator proxy |

## Security

- All inference is local (no cloud APIs)
- LLM server bound to localhost only
- API key authentication (Bearer token)
- Rate limiting (30 req/min per IP)
- Input validation (500 char max)
- Parameterized SQL queries
- Only official upstream source code

## Status

- ✅ LLM Layer (Qwen3.5-2B, llama.cpp, `-c 4096` with prompt token-budgeting)
- ✅ Speech-to-Text (whisper.cpp, base.en)
- ✅ Orchestrator (FastAPI, secured — see [docs/AUDIT.md](docs/AUDIT.md))
- ✅ Memory (SQLite + ChromaDB cosine RAG, embeddinggemma-300m, background embedding)
- ✅ Text-to-Speech (Piper TTS — wired into both `/inbox` and the streaming web UI)
- ⬜ Home Automation (MQTT + Home Assistant)

See [docs/AUDIT.md](docs/AUDIT.md) for the full code/security audit and
[docs/DEPLOY.md](docs/DEPLOY.md) for the deploy runbook.
Checks: `uv run pytest` · `uv run ruff check src/orchestrator src/scripts tests`.
