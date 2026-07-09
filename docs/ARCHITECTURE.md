# Architecture

> Visual companion: **[DIAGRAMS.md](DIAGRAMS.md)** renders every flow on this page as a diagram.

Jarvis is a fully self-hosted, offline voice + text assistant. Everything — the LLM, speech-to-text,
embeddings, memory, and text-to-speech — runs on a single 2011-era laptop in a Proxmox LXC. No cloud.

The defining constraint is the hardware: an **Intel i5-2520M (Sandy Bridge, 2C/4T, AVX but no AVX2),
8 GB RAM, CPU-only**. Every design choice below is downstream of "make an LLM assistant feel
responsive on this box." See [SPECS.md](SPECS.md) for the full hardware/model details.

---

## Component overview

```
            ┌──────────── voice ─────────────┐        ┌──────── web / phone ────────┐
   speech → whisper.cpp (base.en STT)                 React 19 SPA  +  admin panel
            run_listener.sh ──┐                                 │  (HTTPS, Bearer auth)
                              ▼                                  ▼
                  ┌──────────────────────────────────────────────────────┐
                  │            FastAPI Orchestrator  (port 5000)          │
                  │  auth middleware · rate limit · token-budgeted prompt │
                  └───┬──────────────┬───────────────┬───────────────┬────┘
                      ▼              ▼               ▼               ▼
            llama.cpp (Qwen 2B)   SQLite        ChromaDB         Piper TTS
            127.0.0.1:8081        jarvis.db     (cosine RAG,     en_GB-alan
            -c 4096               history/      embeddinggemma   → base64 WAV
                                  users/keys    -300m)
                                                    ▲
                                       idle-time fact extraction (background)
```

| Component | Tech | Where | Role |
|---|---|---|---|
| **LLM** | llama.cpp `llama-server`, Qwen3.5-2B Q4_K_M | `127.0.0.1:8081` | Text generation (chat, titles, fact extraction) |
| **Orchestrator** | FastAPI + uvicorn | `0.0.0.0:5000` | Auth, routing, prompt assembly, memory, the request brain |
| **STT** | whisper.cpp (base.en) | local binary | Voice → text, wake word "Jarvis" |
| **TTS** | Piper (`en_GB-alan-medium`) | local binary | Text → speech (base64 WAV) |
| **Relational store** | SQLite (WAL) | `memory/jarvis.db` | Users, sessions, message history, API keys, knowledge facts |
| **Vector store** | ChromaDB (cosine) | `memory/chroma_db` | Semantic long-term recall (RAG) |
| **Embeddings** | `google/embeddinggemma-300m` (**ONNX Runtime**, torch-free) | in-process | Document/query vectors for RAG + fact dedup |
| **Frontend** | React 19 + Vite | `frontend/` → `dist/` | Chat UI (served at `/`); admin panel at `/admin` |
| **Camera agent** | OpenCV YuNet+SFace, opencv-python | the device (`camera/`) | On-device motion/face/pose/gesture → high-level **events** (no imagery); identity feeds per-user authz |

Two long-lived processes run under systemd: `llama-fast.service` (the model server) and
`jarvis-orchestrator.service` (the FastAPI app, served over **HTTPS** — local CA, see
[setup/tls.md](setup/tls.md)). The **camera agent** runs on each device, **outbound-only** (it POSTs
events + pulls its enrolled set; opens no port). See [DEPLOY.md](DEPLOY.md).

---

## Orchestrator module graph

`main.py` was deliberately split into small, single-responsibility modules with an **acyclic**
import graph (`config → {db, auth, llm, ha} → memory → {chat, intent_router} → main`):

```
config.py   configuration, tunables, logging        (no app deps)
  ├─ db.py      SQLite connection factory + schema init + app_settings get/set
  ├─ auth.py    PBKDF2 password hashing
  ├─ ha.py      Home Assistant REST client + entity-allowlist guardrails (runtime-configurable)
  ├─ onnx_embed.py  torch-free embedder (onnxruntime + tokenizers; used by memory)
  └─ llm.py     LLM HTTP client (blocking/stream) + Piper TTS
        └─ intent_router.py  semantic device-intent router (embeds utterances vs per-device
                             exemplars via memory's embedder; calibrated act/confirm thresholds)
        └─ memory.py   embeddings, ChromaDB, knowledge base, idle fact extraction,
        │              request-activity / in-flight tracking
              └─ chat.py    sessions, message persistence, context-window prompt assembly
                    └─ main.py   FastAPI app, auth middleware, route handlers only
budget.py   pure token-budgeting helpers (no I/O — unit-tested in isolation)
```

Why this shape:
- **`config` has no app dependencies**, so every module can import constants without cycles.
- **`memory` never imports `chat`/`main`** — the cycle that would naturally form (prompt assembly
  needs memory; memory cleanup needs sessions) is broken by keeping vector ops behind small
  functions (`enqueue_embedding`, `delete_vectors`) that `chat` calls into.
- **`budget` is pure** (no globals, no I/O) so the trickiest logic — token budgeting — is unit-tested
  without loading the 300M embedding model.

---

## Key design decisions

| Decision | Rationale |
|---|---|
| 2B model, `--reasoning off` / `/no_think` | Fits 8 GB / no-AVX2; disables hidden thinking chains (5–15 s vs 60 s+) |
| `-c 4096` **+ prompt token-budgeting** | The window is fixed; the app clamps prompt + completion to fit so context is never silently evicted (see [WORKFLOWS.md](WORKFLOWS.md)) |
| Single system message | The Qwen chat template rejects multiple/non-leading system messages, so system prompt + profile + RAG are merged into one |
| Embedding **off the request path** | A 300M model on no-AVX2 takes hundreds of ms; a background worker embeds so chat never blocks |
| Single LLM slot + in-flight guard | `--parallel 1`; the idle fact-extractor must not contend with a live generation for the 2 cores |
| ChromaDB cosine + embedding prefixes | embeddinggemma is asymmetric; correct query/document prefixes + cosine space are required for usable recall |
| Per-user API keys (no master key) | Auth is web-login sessions or revocable `api_keys`; a local CLI (`manage.py`) handles bootstrap/recovery |
| SQLite + ChromaDB, no external services | Zero extra daemons; everything is a file on disk, survives reboots, no network deps |

---

## Security model (summary)

- All inference is **local**; the LLM server binds `127.0.0.1` only (reachable solely via the orchestrator).
- The orchestrator binds `0.0.0.0` (so loopback + the Tailscale interface both work); a host firewall
  restricts the LAN. Served over **HTTPS** (per-deployment local CA — encrypts tokens/keys/events; see
  [setup/tls.md](setup/tls.md)). Runs **non-root** under a hardened systemd unit.
- Auth = web-login **session tokens** or per-user **API keys**; no static admin secret. **Device-bound
  keys never wield admin** (even if minted under an admin account) and can only post events as their
  own device — bounding a stolen camera key. The **last admin** can't be deleted/demoted.
- Device agents (camera, volume) are **outbound-only** — no inbound port; they pull commands / their
  enrolled set and POST events. No imagery leaves the device (except a transient, RAM-only, admin-only
  enroll preview).
- Per-user rate limiting, parameterized SQL everywhere, input validation, security headers (CSP).
- A full self-audit and the fixes are recorded in [AUDIT.md](AUDIT.md).
