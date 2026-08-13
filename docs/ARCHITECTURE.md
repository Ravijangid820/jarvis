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
        ┌─────── voice, on the box ───────┐    ┌──────── web / phone ────────┐
   speech → whisper.cpp (whisper-stream)        React 19 SPA  +  admin panel
            voice_bridge.py ──┐                 wake word + Whisper STT + VAD
                              │                 run IN THE BROWSER (onnxruntime-web)
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
                                       ▲             ▲
                                       └── one idle worker: batch embedding,
                                           then fact extraction (background)
```

| Component | Tech | Where | Role |
|---|---|---|---|
| **LLM** | llama.cpp `llama-server`, Qwen3.5-2B Q4_K_M | `127.0.0.1:8081` | Text generation (chat, fact extraction) |
| **Orchestrator** | FastAPI + uvicorn | `0.0.0.0:5000` | Auth, routing, prompt assembly, memory, the request brain |
| **STT (browser)** | Whisper via transformers.js + onnxruntime-web | the tab | Voice → text for the web UI; audio never leaves the browser |
| **Wake word (browser)** | openWakeWord ONNX + phrase matching | the tab | "Hey Jarvis" in the web UI; while armed, audio is only ever keyword-spotted |
| **STT (server)** | whisper.cpp (base.en), `whisper-stream` | local binary | The always-on listener for a box with a mic attached |
| **TTS** | Piper (`en_GB-alan-medium`) | local binary | Text → speech (base64 WAV) |
| **Relational store** | SQLite (WAL) | `memory/jarvis.db` | Users, sessions, message history, API keys, knowledge facts |
| **Vector store** | ChromaDB (cosine) | `memory/chroma_db` | Semantic long-term recall (RAG) |
| **Embeddings** | `google/embeddinggemma-300m` (**ONNX Runtime**, torch-free) | in-process | Document/query vectors for RAG + fact dedup |
| **Frontend** | React 19 + Vite | `frontend/` → `dist/` | Chat UI (served at `/`); admin panel at `/admin`; `/voice` live mode |
| **Camera agent** | OpenCV YuNet+SFace, opencv-python | the device (`camera/`) | On-device motion/face/pose/gesture → high-level **events** (no imagery); identity feeds per-user authz |

Two long-lived processes run under systemd: `llama-fast.service` (the model server) and
`jarvis-orchestrator.service` (the FastAPI app, served over **HTTPS** — local CA, see
[setup/tls.md](setup/tls.md)). The **camera agent** runs on each device, **outbound-only** (it POSTs
events + pulls its enrolled set; opens no port). See [DEPLOY.md](DEPLOY.md).

---

## Orchestrator module graph

The orchestrator is split into small, single-responsibility modules with an **acyclic** import
graph (`config → {db, auth, llm, ha} → memory → {chat, intent_router} → deps → routes → main`).
`main.py` itself is now the app, the middleware, start-up and the static mounts — the 81 route
handlers live under `routes/`:

```
config.py   configuration, tunables, logging        (no app deps)
  ├─ db.py      SQLite connection factory + schema init + migrations,
  │             app_settings / household_settings get/set
  ├─ auth.py    PBKDF2 password hashing
  ├─ ha.py      Home Assistant REST client + entity-allowlist guardrails (runtime-configurable)
  ├─ onnx_embed.py  torch-free embedder (onnxruntime + tokenizers; used by memory)
  ├─ safehttp.py    outbound HTTP for URLs we were told to fetch: redirect + credential guard
  ├─ purge.py       deleting an account or a household (admin routes + the demo sweeper)
  └─ llm.py     LLM HTTP client (blocking/stream) + Piper TTS
        └─ intent_router.py  semantic device-intent router (embeds utterances vs per-device
                             exemplars via memory's embedder; calibrated act/confirm thresholds)
        └─ memory.py   embeddings, ChromaDB, knowledge base, the idle worker
        │              (batch embedding + fact extraction), in-flight tracking
              └─ chat.py    sessions, message persistence, context-window prompt assembly
                    ├─ deps.py       the request guards every router shares
                    ├─ ha_config.py  apply HA settings + rebuild the intent index
                    └─ routes/       admin · chat · devices · faces · mcp · voice
                          └─ main.py   the app, middleware, start-up, static mounts
budget.py   pure token-budgeting helpers (no I/O — unit-tested in isolation)
intents.py  pure phrase parsing: greetings, volume, reminders, home commands (no I/O)
mcp.py      MCP client
```

Why this shape:
- **`config` has no app dependencies**, so every module can import constants without cycles.
- **`memory` never imports `chat` or `main`** — in any form, including inside a function. The cycle
  that would naturally form (prompt assembly needs memory; memory cleanup needs sessions) is broken
  by keeping vector ops behind small functions (`enqueue_embedding`, `delete_vectors`) that `chat`
  calls into.

  The one place this used to be bent is worth recording. `memory.extract_facts_batch` ends by
  warming the conversation's system prefix back into llama-server's KV cache — and what that prefix
  *is*, only `chat` knows. It used to reach for it with a function-local `import chat`. That
  worked, and that is precisely the problem: nothing failed, the invariant simply stopped being
  true, and one you cannot rely on buys nothing. The dependency is inverted now — `chat` registers
  a callback via `memory.on_llm_displaced`, and `memory` calls whatever it was handed without
  knowing what it is. `tests/test_kv_cache.py` parses memory.py's AST and fails if `chat` appears
  in *any* import, since a local import inside a function is invisible to every other kind of
  review.
- **Nothing under `routes/` imports `main`**, so the graph stays a tree. Two things that both a
  router and start-up need (`purge`, `ha_config`) are modules for exactly that reason.
- **`budget` is pure** (no globals, no I/O) so the trickiest logic — token budgeting — is unit-tested
  without loading the 300M embedding model.

---

## Key design decisions

| Decision | Rationale |
|---|---|
| 2B model, `--reasoning off` / `/no_think` | Fits 8 GB / no-AVX2; disables hidden thinking chains (5–15 s vs 60 s+) |
| `-c 4096` **+ prompt token-budgeting** | The window is fixed; the app clamps prompt + completion to fit so context is never silently evicted (see [WORKFLOWS.md](WORKFLOWS.md)) |
| Single system message, kept **stable** | The Qwen chat template rejects multiple/non-leading system messages. The system prompt + knowledge blocks go there; anything that changes per turn (RAG hits, live device state, camera presence) hangs off the current user turn instead, so llama-server's KV-cache prefix stays valid |
| Titles without the model | A second LLM call for four cosmetic words cost 5.7 s of the single slot and displaced the conversation from it. `chat.title_from_text` is model-free; `JARVIS_LLM_TITLES=1` restores the old path |
| Greetings answered by the server | A 2B model handed a contentless turn invents household state. `intents.is_greeting` → a canned reply, never the model |
| Embedding **off the request path**, batched at idle | A 300M model on no-AVX2 costs ~1.2 s per message, landing while the next message is typed. Messages are flushed in batches once the box is quiet — also 64% cheaper per message |
| The pending set is a **column**, not a queue | `conversation_history.embedded = 0`. An in-memory queue was fine draining in seconds; at idle-batch timescales anything queued at shutdown vanished silently |
| Single LLM slot + in-flight guard | `--parallel 1`; the idle fact-extractor must not contend with a live generation for the 2 cores |
| Speech in the browser (v3.2.0+) | Wake word and STT run in the tab via onnxruntime-web, so the two cores are left for the LLM and no audio crosses the wire |
| ChromaDB cosine + embedding prefixes | embeddinggemma is asymmetric; correct query/document prefixes + cosine space are required for usable recall |
| Per-user API keys (no master key) | Auth is web-login sessions or revocable `api_keys`; a local CLI (`manage.py`) handles bootstrap/recovery |
| SQLite + ChromaDB, no external services | Zero extra daemons; everything is a file on disk, survives reboots, no network deps |

---

## Security model (summary)

- All inference is **local**; the LLM server binds `127.0.0.1` only (reachable solely via the orchestrator).
  Browser-side speech is local in the stronger sense: the wake word and Whisper run in the tab, and
  their models are served by the orchestrator itself (`/stt-models`, `/wake-models`, `/ort`), never
  a CDN.
- The orchestrator binds `0.0.0.0` (so loopback + the Tailscale interface both work); a host firewall
  restricts the LAN. Served over **HTTPS** (per-deployment local CA — encrypts tokens/keys/events; see
  [setup/tls.md](setup/tls.md)). Runs **non-root** under a hardened systemd unit.
- Auth = web-login **session tokens** or per-user **API keys**; no static admin secret. **Device-bound
  keys never wield admin** (even if minted under an admin account) and can only post events as their
  own device — bounding a stolen camera key. The **last admin** can't be deleted/demoted.
- Device agents (camera, volume) are **outbound-only** — no inbound port; they pull commands / their
  enrolled set and POST events. **No imagery leaves the device, ever** — the agent has no code path
  that puts a frame on the wire. Face enrollment happens in the browser of the device holding the
  camera (detect + align + embed in a Worker; only the vector is sent), and recognition for browsers
  goes through `/faces/identify`, so face **templates** are never distributed either — `/faces/enrolled`
  is device-key/admin only.
- Per-user rate limiting, parameterized SQL everywhere, input validation, security headers (CSP).
- A full self-audit and the fixes are recorded in [AUDIT.md](AUDIT.md).
