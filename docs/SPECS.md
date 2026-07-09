# Specifications & Reference

## Hardware

| | |
|---|---|
| CPU | Intel Core i5-2520M (Sandy Bridge), 2 cores / 4 threads, ~2.5 GHz |
| SIMD | SSE3/SSSE3/SSE4.1/SSE4.2, **AVX**, POPCNT — **no AVX2, no FMA, no F16C** |
| RAM | 8 GB (≈6 GB usable after OS + services) + 2 GB swap |
| GPU | none — **CPU-only inference** |
| Host | Proxmox LXC container |

The missing AVX2 is the single biggest performance factor: llama.cpp / GGML fall back to slower
kernels, so smaller models and tight prompt budgets matter more than usual.

## Models

| Role | Model | Size | Runtime | Notes |
|---|---|---|---|---|
| LLM | Qwen3.5-2B-Q4_K_M (GGUF) | ~1.3 GB | llama.cpp `llama-server` | `-c 4096 -t 2 --parallel 1 --reasoning off` |
| Embeddings | `google/embeddinggemma-300m` | ~1.2 GB | **ONNX Runtime** (in-process, torch-free; full-pipeline export verified cosine 1.0 vs torch) | 768-dim, cosine, asymmetric prefixes |
| STT | whisper.cpp `base.en` | ~142 MB | whisper-command | built `-DGGML_AVX=ON -DWHISPER_SDL2=ON` |
| TTS | Piper `en_GB-alan-medium` | ~63 MB | piper binary | ONNX voice |

A 4B model is on disk but unused — the orchestrator is single-model by design.

## Performance (measured on this box)

| Metric | Value |
|---|---|
| LLM generation | ~5 tok/s (Q4, `--reasoning off`) |
| LLM prompt eval | ~9–10 tok/s |
| Whisper base.en | ~7.6× realtime (~83 s for 11 s audio) |
| End-to-end voice reply | ~30–90 s depending on answer length |
| Idle RAM | ~1.8 GB; LLM server RSS ~1.3 GB |

Benchmarks live in [benchmarks/](benchmarks/).

---

## Configuration reference (`config/jarvis.json`)

The real file is **gitignored** (it has no secrets after the master-key removal, but is environment-
specific); commit changes to [`config/jarvis.example.json`](../config/jarvis.example.json) instead.

```jsonc
{
  "llm": {
    "fast_brain_url": "http://127.0.0.1:8081/v1/chat/completions",  // the LLM server
    "request_timeout_seconds": 120,    // urllib timeout on LLM calls
    "default_temperature": 0.4,
    "max_context_tokens": 4096         // must match llama-server's -c flag
  },
  "orchestrator": {
    "host": "0.0.0.0",                 // 0.0.0.0 so loopback + Tailscale both work
    "port": 5000,
    "max_input_length": 500,           // non-admin per-message cap
    "rate_limit_requests_per_minute": 30,
    "allowed_origins": []              // CORS allowlist; [] = no cross-origin (most secure)
  },
  "memory": {
    "db_path": "/srv/jarvis/memory/jarvis.db",
    "chroma_db_path": "/srv/jarvis/memory/chroma_db",
    "max_context_messages": 100        // ceiling on history pulled before token-budgeting
  },
  "home_assistant": {                  // optional smart-home control (docs/setup/home-assistant.md)
    "url": "",                         // e.g. http://192.168.0.120:8123 — empty = feature off
    "token": "",                       // long-lived token from a dedicated NON-admin HA user
    "allowed_entities": []             // hard allowlist of entity_ids the LLM tools may touch
  },
  "system_prompt": "You are Jarvis... /no_think"
}
```

Tunables that are **constants in code** (not config) live in `src/orchestrator/config.py`:
`COMPLETION_RESERVE_DEFAULT`, `PROMPT_SAFETY_MARGIN`, `KNOWLEDGE_TOKEN_CAP`, `MIN_COMPLETION_TOKENS`,
`RAG_DISTANCE_THRESHOLD`, `RAG_MAX_RESULTS`, `IDLE_THRESHOLD_SECONDS`, `FACT_DEDUP_SIM`, the embedding
prefixes, and the Piper paths. The semantic intent router's thresholds live in
`src/orchestrator/intent_router.py` (`ACT_SIM=0.80`, `CONFIRM_SIM=0.63`, `AMBIGUITY_MARGIN=0.04`) —
calibrated 2026-07-09 against the real embedder on the box (calibration data in the module docstring).

**Home Assistant precedence:** env (`HA_URL`/`HA_TOKEN`/`HA_ALLOWED_ENTITIES`) → admin-UI values
(stored in the `app_settings` DB table, applied live) → the `home_assistant` block above. Env-set
fields show read-only in the UI.

---

## Database schema (`config/schema.sql`)

SQLite in WAL mode. `schema.sql` is the single source of truth; `db.init_db()` also runs idempotent
safety-net migrations.

| Table | Purpose | Key columns |
|---|---|---|
| `users` | accounts | `id`, `username` (unique), `password_hash` (PBKDF2), `role` |
| `chat_sessions` | conversations | `id` (uuid or `u<id>-default`), `title`, `user_id` |
| `conversation_history` | messages | `id`, `session_id`, `speaker` (`user`/`jarvis`), `content`, `facts_extracted` |
| `auth_sessions` | web-login tokens | `token`, `user_id`, `expires_at` |
| `api_keys` | machine integrations | `key_string`, `user_id`, `description`, `device_id` (binds to one device), `usage_count`, `last_used_at` |
| `user_knowledge` | persistent facts | `id`, `user_id`, `category`, `content`, `source` |
| `vision_events` | camera events | `id`, `device_id`, `type`, `data` (JSON), `user_id`, `created_at` (last 5000 kept) |
| `device_heartbeats` | camera liveness | `device_id` (PK), `last_seen` (powers admin active/inactive) |
| `persons` | recognizable people | `id`, `name` (unique), `user_id` (→ account for authz), `created_at` |
| `face_embeddings` | embeddings per person | `id`, `person_id` (→ persons, cascade), `embedding` (JSON), `source` |
| `enroll_requests` | enroll-from-UI queue | `id`, `device_id`, `name`, `status` (pending/done/failed), `requested_by` |
| `app_settings` | admin-editable runtime settings (Smart Home url/token/allowlist) | `key` (PK), `value`, `updated_at` |

Long-term recall vectors are **not** in SQLite — they live in ChromaDB (`jarvis_memory_cos`,
cosine space), keyed by the `conversation_history.id`.

---

## Toolchain

- **Python** ≥ 3.13, managed with **`uv`** (always `uv run …`, never bare `python3`).
- **Tests**: `uv run pytest` · **Lint**: `uv run ruff check src/orchestrator src/scripts tests`.
- **CI**: `.github/workflows/ci.yml` runs ruff + pytest on every push.
- **Frontend**: Node + Vite (`cd frontend && npm install && npm run build`).
- **Services**: systemd (`llama-fast`, `jarvis-orchestrator`) — see [DEPLOY.md](DEPLOY.md).
