# Jarvis AI Project Documentation

## Project Goal

Build a fully self-hosted, secure, offline-capable Jarvis-style AI assistant running inside a Proxmox LXC container on limited hardware.

### Hardware
- CPU: Intel Core i5-2520M (Sandy Bridge)
- Cores/Threads: 2C / 4T
- RAM allocated to container: 8 GB (upgraded from 6 GB on 2026-06-02)
- Swap: 2 GB
- Disk: 49 GB (39 GB free)
- Inference: CPU-only (no GPU)
- CPU Features: SSE3, SSSE3, SSE4.1, SSE4.2, AVX, POPCNT (no AVX2, no FMA, no F16C)

### Core Requirements
- Self-hosted
- Secure
- No cloud inference
- CPU-only
- Proxmox LXC deployment
- Voice interaction
- Home automation integration
- Long-term memory
- Extensible architecture

---

# Current Architecture

## LLM Layer

### Runtime
- Official llama.cpp (ggml-org) — version b9441, commit 3292da09f
- Built from source with Release mode
- AVX enabled, AVX2/FMA/F16C disabled (Sandy Bridge)
- OpenAI-compatible API enabled
- Thinking/reasoning mode disabled (`--reasoning off`) for fast responses

### Active Model — Fast Brain
Model:
- Qwen3.5-2B-Q4_K_M.gguf (1.28 GB on disk)

Purpose:
- All queries (voice commands, questions, conversations)
- Intent classification
- Tool selection
- Memory lookup
- Home automation

Benchmark (with --reasoning off):
- Prompt: ~5.75 tokens/sec
- Generation: ~3.67 tokens/sec

Server:
- Port: 8081
- Host: 127.0.0.1 (localhost only — secured behind orchestrator)
- Context: 1024
- Threads: 2
- Slots: 1
- Reasoning: off
- Systemd: llama-fast.service (enabled, auto-start)

Memory Usage:
- ~1.5 GB (RSS with mmap)

### Inactive Model — Reasoning Brain (Stored, Not Running)
Model:
- Qwen3.5-4B-Q4_K_M.gguf (2.74 GB on disk)

Purpose (future, when re-enabled):
- Coding
- Planning
- Research
- Debugging
- Long-form reasoning
- Complex conversations

Status:
- Model file retained at /srv/ai/models/qwen3.5/
- Server stopped and service disabled to conserve RAM
- Will be re-enabled as on-demand loading when needed

Previous Benchmark:
- Prompt: ~2.7 tokens/sec
- Generation: ~1.4 tokens/sec
- Memory: ~3370 MiB

## Speech-to-Text Layer

### Runtime
- Official whisper.cpp (ggerganov) — version v1.8.6
- Built from source with Release mode
- AVX enabled (rebuilt 2026-06-02 — was previously OFF)
- SDL2 enabled for microphone capture
- Flash attention enabled

### Models Available
- ggml-base.en.bin (142 MB) — **SELECTED** (optimal for this hardware)
- ggml-small.en.bin (487 MB) — available but too slow for real-time

### Benchmark Comparison (JFK 11-second clip, 2 threads, 1 beam)

| Metric | base.en (142 MB) | small.en (487 MB) |
|---|---|---|
| Accuracy | Perfect | Perfect |
| Encode time | 76.0 sec | 336.8 sec |
| Decode time | 7.0 sec (259 ms/run) | 23.4 sec (781 ms/run) |
| Total time | **83.5 sec** | 364.3 sec |
| Realtime factor | **7.6x** | 33.1x |
| Load time | 292 ms | 3866 ms |
| RAM usage | ~300 MB | ~730 MB |

Recommendation: base.en — same accuracy at 4.4x faster speed, 2.4x less RAM.

### Binaries Built
- whisper-cli, whisper-command, whisper-stream, whisper-server
- whisper-bench, whisper-quantize, whisper-talk-llama

## Orchestrator

### Runtime
- FastAPI (Python 3.13.5) with uvicorn
- Runs in venv at /srv/ai/orchestrator/venv/
- Systemd: jarvis-orchestrator.service (enabled, auto-start)
- Port: 5000 (0.0.0.0 — accessible from network devices)

### Features
- API key authentication (Bearer token)
- Rate limiting (30 requests/minute per IP)
- Input validation (max 500 characters)
- Request timeouts (120 seconds)
- Security headers (X-Content-Type-Options, X-Frame-Options, Cache-Control)
- Conversation memory (SQLite with FTS5)
- Context injection (last 10 messages)
- Health check endpoint (/health — no auth required)
- Conversation history endpoint (/history)

### Configuration
- Central config: /srv/ai/config/jarvis.json
- API key generated via secrets.token_hex(32)
- System prompt includes /no_think directive for Qwen3.5

## Memory Layer

### Runtime
- SQLite 3.46.1 with FTS5
- Database: /srv/ai/memory/jarvis.db
- Schema: /srv/ai/memory/schema.sql

### Tables
- conversation_history — stores all user/jarvis messages with timestamps
- conversation_fts — FTS5 virtual table for full-text search (auto-synced via triggers)
- semantic_facts — stores extracted facts with topics

### Integration
- Auto-initialized on orchestrator startup
- Every query and response automatically stored
- Recent context (last 10 messages) injected into LLM prompts
- FTS5 search available for fact retrieval
- All queries use parameterized statements (SQL injection safe)
- WAL journal mode for concurrent read/write

---

# Verified Milestones

## llama.cpp Build
Completed successfully.

Issues encountered:
- Illegal instruction errors
- CPU feature mismatch

Resolution:
- Rebuilt for AVX-only CPU
- Disabled unsupported optimizations (AVX2, FMA, F16C)

Result:
- Stable execution

## Model Deployment
Successfully deployed:

1. Qwen3.5-2B-Q4_K_M — ACTIVE (running as systemd service)
2. Qwen3.5-4B-Q4_K_M — STORED (file on disk, service disabled)

## API Verification

OpenAI-compatible endpoint tested:

/v1/chat/completions

Status:
- Working

Example test:
Question:
- What is the capital of France?

Response:
- Paris (correct, ~6 seconds total)

## Whisper.cpp Rebuild (2026-06-02)
- Rebuilt with explicit GGML_AVX=ON (was OFF due to CMake auto-detection failure in LXC)
- SDL2 enabled for microphone capture
- All binaries compiled successfully

## Orchestrator Security Hardening (2026-06-02)
- API key authentication implemented
- Rate limiting active
- Input validation enforced
- Security headers applied
- LLM endpoints locked to 127.0.0.1
- Verified: 401 on missing auth, 403 on invalid key, 200 on valid auth

## Memory Integration (2026-06-02)
- SQLite database created and initialized
- Conversation storage verified
- Context injection working

---

# Implemented Directory Structure

/srv/ai
├── models/
│   ├── qwen3.5/Qwen3.5-4B-Q4_K_M.gguf (2.74 GB, inactive)
│   └── qwen3.5_2b/Qwen3.5-2B-Q4_K_M.gguf (1.28 GB, active)
├── memory/
│   ├── schema.sql
│   └── jarvis.db
├── logs/
│   └── orchestrator.log
├── config/
│   └── jarvis.json
├── whisper/ (official ggerganov repo, built from source)
│   ├── build/bin/ (all binaries)
│   └── models/ (ggml-base.en.bin, ggml-small.en.bin)
├── piper/ (empty — Phase 5)
├── orchestrator/
│   ├── main.py
│   ├── requirements.txt
│   ├── run_listener.sh
│   └── venv/
└── backups/

---

# Current Target Architecture

User Voice
    ↓
Wake Word via whisper-command ("Jarvis")
    ↓
Whisper.cpp STT (Official ggerganov)
    ↓
run_listener.sh (curl → orchestrator)
    ↓
FastAPI Orchestrator (API key auth, rate limit)
    ↓
Qwen 2B (all queries, thinking disabled)
    ↓
SQLite Memory (store + context)
    ↓
JSON Response
    ↓
[Future] Piper TTS → Speaker

---

# Systemd Services

## llama-fast.service
- Description: Jarvis Fast Brain - Qwen3.5-2B LLM Server
- Status: enabled, active
- Port: 127.0.0.1:8081
- Flags: --reasoning off, --parallel 1
- File: /etc/systemd/system/llama-fast.service

## jarvis-orchestrator.service
- Description: Jarvis AI Orchestrator - FastAPI Service
- Status: enabled, active
- Port: 0.0.0.0:5000
- Depends on: llama-fast.service
- File: /etc/systemd/system/jarvis-orchestrator.service

## Removed Services
- ai-fast-brain.service (replaced by llama-fast.service)
- ai-reasoning-brain.service (disabled, removed — 4B model not in use)

---

# Planned Components

## Phase 1 - LLM Layer
Status: COMPLETE

Tasks:
- Install llama.cpp ✓
- Build from source ✓
- Deploy 2B model ✓
- Deploy 4B model ✓ (stored, not running)
- Benchmark models ✓
- Verify API ✓
- Restructure directories to /srv/ai ✓
- Create API background systemd services ✓
- Disable thinking mode for speed ✓

## Phase 2 - Speech-to-Text
Status: COMPLETE

Component:
- whisper.cpp (Official ggerganov source - NO third party)
- SDL2 for direct ALSA/PulseAudio microphone capture

Tasks:
- Build whisper.cpp from source ✓
- Rebuild with AVX enabled ✓ (2026-06-02)
- Rebuild with SDL2 enabled ✓ (2026-06-02)
- Download base.en model ✓
- Download small.en model ✓
- Benchmark models — IN PROGRESS
- Create wake word listener script ✓

## Phase 3 - Orchestrator
Status: COMPLETE

Component:
- FastAPI (Python 3.13.5, uvicorn)

Responsibilities:
- Route requests ✓
- Manage memory ✓ (SQLite integration)
- API key authentication ✓
- Rate limiting ✓
- Input validation ✓
- Security headers ✓
- Health monitoring ✓

## Phase 4 - Memory
Status: COMPLETE

Component:
- SQLite 3.46.1 (Using native FTS5 for text search)

Capabilities:
- Conversation history ✓
- Context retrieval ✓ (last 10 messages)
- Full-text search ✓ (FTS5)
- Semantic facts table ✓ (schema ready)
- User preferences — TODO
- Task memory — TODO

## Phase 5 - Text-to-Speech
Status: Pending

Component:
- Piper TTS

Capabilities:
- Offline speech synthesis
- Low latency responses

## Phase 6 - Home Automation
Status: Pending

Components:
- MQTT
- Home Assistant

Capabilities:
- Device control
- Sensor monitoring
- Automation workflows

---

# Routing Strategy

Current Mode: Single-model (2B only)

Active Model:
- Qwen3.5-2B (handles all queries)

Stored Model:
- Qwen3.5-4B (on disk, will be re-enabled as on-demand loading in the future)

Future Plan (when RAM allows or on-demand loading is implemented):

Use 2B for:
- Quick questions
- Commands
- Intent detection
- Tool calling

Use 4B for (on-demand, loaded when needed):
- Coding
- Planning
- Analysis
- Complex reasoning

---

# Performance Tuning

## Thinking Mode Disabled
- Qwen3.5 models default to "thinking mode" with hidden `<think>` chains
- This caused 60+ second response times on this CPU
- Disabled via `--reasoning off` flag on llama-server
- Also disabled via `/no_think` in system prompt
- Result: responses now take 5-15 seconds instead of 60+

## Swap Configuration
- vm.swappiness=10 set in /etc/sysctl.conf
- NOTE: Cannot be applied inside LXC — must run on Proxmox host:
  `sysctl vm.swappiness=10`

## Memory Budget (8 GB)
| Component | Memory |
|---|---|
| Qwen3.5-2B server | ~1.5 GB |
| Whisper (base.en) | ~300 MB |
| Orchestrator + Python | ~50 MB |
| OS + systemd | ~200 MB |
| **Total active** | **~2.0 GB** |
| **Available headroom** | **~6.2 GB** |

## Whisper.cpp AVX Fix
- Original build had GGML_AVX=OFF despite CPU support
- CMake auto-detection failed inside LXC container
- Rebuilt with explicit GGML_AVX=ON
- Expected 15-30% speed improvement for transcription

---

# Security Principles

- Local-only inference (LLM endpoints on 127.0.0.1)
- No cloud LLM APIs
- Strictly official source code/repos only (no unofficial third-party wrappers or libraries)
- Source-built software
- API key authentication for orchestrator access
- Rate limiting (30 req/min per IP)
- Input validation and size limits
- Security headers on all responses
- Parameterized SQL queries only (no string concatenation)
- Config file for secrets (not hardcoded)
- Proxmox firewall enabled
- Logging: detailed server-side, generic error messages to clients

---

# API Reference

## Base URL
http://<server-ip>:5000

## Authentication
All endpoints except /health require Bearer token authentication:
```
Authorization: Bearer <api_key>
```
API key is stored in /srv/ai/config/jarvis.json

## Endpoints

### GET /health
No authentication required.
Returns: `{"status": "ok", "model": "qwen3.5-2b"}`

### POST /inbox
Send a text query to Jarvis.
Body: `{"text": "your question here"}`
Returns: `{"response": "jarvis answer"}`
Max input length: 500 characters.

### GET /history
Retrieve recent conversation history.
Returns: `{"messages": [...], "count": N}`

---

# Current Status

Overall Progress:
- LLM Layer: Complete (2B active, 4B stored)
- Speech-to-Text: Complete (rebuilt with AVX + SDL2)
- Orchestrator: Complete (security hardened, memory integrated)
- Memory: Complete (SQLite + FTS5, conversation storage working)
- Text-to-Speech: Pending
- Home Automation: Pending
- RAM upgraded to 8 GB
- Thinking mode disabled for speed
- API key authentication enabled
- Full pipeline tested end-to-end

Next Immediate Tasks:
1. Complete whisper model benchmarks (base.en vs small.en)
2. Set vm.swappiness=10 on Proxmox host
3. Install and configure Piper TTS (Phase 5)

---

# Changelog

## 2026-06-02 — Major Improvement Session
- RAM upgraded from 6 GB to 8 GB
- Stopped 4B model server (saving ~3 GB RAM)
- Rebuilt whisper.cpp with GGML_AVX=ON and WHISPER_SDL2=ON
- Downloaded small.en whisper model for benchmarking
- Rewrote orchestrator with security hardening:
  - API key authentication
  - Rate limiting
  - Input validation
  - Request timeouts
  - Security headers
- Integrated SQLite memory into orchestrator:
  - Conversation storage
  - Context injection
  - FTS5 search
- Created proper systemd services (llama-fast, jarvis-orchestrator)
- Removed old services (ai-fast-brain, ai-reasoning-brain)
- Disabled Qwen3.5 thinking mode (--reasoning off)
- Added /no_think to system prompt
- Updated run_listener.sh with API key auth
- Set vm.swappiness=10 in sysctl.conf
- Full end-to-end pipeline tested and verified
