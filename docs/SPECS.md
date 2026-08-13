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
| STT (server) | whisper.cpp `base.en` | ~142 MB | `whisper-stream` → `voice_bridge.py` | built `-DGGML_AVX=ON -DWHISPER_SDL2=ON` |
| STT (browser) | Whisper `base` (quantized ONNX) | ~80 MB | transformers.js + onnxruntime-web | served from `/stt-models`, cached in the browser |
| Wake word (browser) | openWakeWord "hey jarvis" | ~2 MB | onnxruntime-web | served from `/wake-models`; other phrases matched on transcript |
| TTS | Piper `en_GB-alan-medium` | ~63 MB | piper binary | ONNX voice |

**One model is live at a time**, and the process serving it belongs to systemd or Docker, not to
the orchestrator. `GET /models` (admin) lists the GGUFs found under `models/` and reports which one
llama-server says it is actually running; `POST /models/switch` (admin) *stages* a choice in
`config/active_model.json` for the next deployment-managed restart. It deliberately does not
pretend the live process changed — the alternative is a UI that claims one model while the
generation comes from another.

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

The defaults below are exactly those in `config/jarvis.example.json`. Where a deployment differs —
`host` is `0.0.0.0` on the author's box so loopback and Tailscale both work — that is a local
choice, not the shipped default.

```jsonc
{
  "llm": {
    "fast_brain_url": "http://127.0.0.1:8081/v1/chat/completions",  // the LLM server
    "request_timeout_seconds": 300,    // urllib timeout on LLM calls (~5 tok/s needs the room)
    "default_temperature": 0.4,
    "max_context_tokens": 4096,        // must match llama-server's -c flag
    "reasoning": false,                // false appends /no_think · true strips it · null = leave
    "sampling": {}                     // top_k / top_p / repeat_penalty / max_tokens / seed
  },
  "orchestrator": {
    "host": "127.0.0.1",
    "port": 5000,
    "rate_limit_requests_per_minute": 120,
    "require_presence_for_device_control": false,  // a camera must see an authorized person first
    "allowed_origins": [],             // CORS allowlist; [] = no cross-origin (most secure)
    "allowed_origin_regex": ""
  },
  "memory": {
    "db_path": "memory/jarvis.db",     // relative to JARVIS_HOME / the repo root
    "chroma_db_path": "memory/chroma_db",
    "max_context_messages": 100,       // ceiling on history pulled before token-budgeting
    "history_max_age_hours": 24        // older turns are not replayed to the model
  },
  "home_assistant": {                  // optional smart-home control (docs/setup/home-assistant.md)
    "url": "",                         // e.g. http://192.168.0.120:8123 — empty = feature off
    "token": "",                       // long-lived token from a dedicated NON-admin HA user
    "allowed_entities": []             // hard allowlist of entity_ids the LLM tools may touch
  },
  "system_prompt": "You are JARVIS, ... /no_think"
}
```

Note there is **no `max_input_length` key**: the per-message cap is a code constant, and there are
two of them — `REGULAR_MAX_INPUT = 500` and `ADMIN_MAX_INPUT = 10000`. Attachments get a separate
48,000-character allowance so small documents are useful without opening an unbounded context path.

Tunables that are **constants in code** (not config) live in `src/orchestrator/config.py`:
`COMPLETION_RESERVE_DEFAULT`, `PROMPT_SAFETY_MARGIN`, `KNOWLEDGE_TOKEN_CAP`, `MIN_COMPLETION_TOKENS`,
`RAG_DISTANCE_THRESHOLD`, `RAG_MAX_RESULTS`, `IDLE_THRESHOLD_SECONDS` (120 s), `IDLE_CHECK_INTERVAL`
(30 s), `EMBED_IDLE_SECONDS` (20 s), `EMBED_MAX_DEFER_S` (900 s), `EMBED_FLUSH_BATCH` (32),
`FACT_EXTRACTION_BATCH` (6), `FACT_DEDUP_SIM`, the embedding prefixes, and the Piper paths. The
semantic intent router's thresholds live in `src/orchestrator/intent_router.py` (`ACT_SIM=0.80`,
`CONFIRM_SIM=0.63`, `AMBIGUITY_MARGIN=0.04`) — calibrated 2026-07-09 against the real embedder on
the box (calibration data in the module docstring).

**Home Assistant precedence:** env (`HA_URL`/`HA_TOKEN`/`HA_ALLOWED_ENTITIES`) → admin-UI values
(stored in the **`household_settings`** table for the primary household, applied live) → the
`home_assistant` block above. Env-set fields show read-only in the UI. (These moved out of the
instance-global `app_settings`: a smart home belongs to one household, and a global token would be
reachable by an admin of any other — including a demo visitor.)

---

## Database schema (`config/schema.sql`)

SQLite in WAL mode. `schema.sql` is the single source of truth; `db.init_db()` also runs idempotent
safety-net migrations.

| Table | Purpose | Key columns |
|---|---|---|
| `households` | tenants; everything scoped is scoped to one | `id`, `name`, `is_demo`, `expires_at` (NULL = permanent) |
| `users` | accounts | `id`, `username` (unique), `password_hash` (PBKDF2), `role`, `can_control_devices`, `household_id` |
| `chat_sessions` | conversations | `id` (uuid or `u<id>-default`), `title`, `user_id` |
| `conversation_history` | messages | `id`, `session_id`, `speaker` (`user`/`jarvis`), `content`, `facts_extracted`, **`kind`** (`chat`/`device`/`greeting`), **`embedded`** (0 = still owes a vector) |
| `auth_sessions` | web-login tokens | `token`, `user_id`, `expires_at` |
| `api_keys` | machine integrations | `key_string`, `user_id`, `description`, `device_id` (binds to one device), `usage_count`, `last_used_at` |
| `user_knowledge` | persistent facts, per user | `id`, `user_id`, `category`, `content`, `source` |
| `global_knowledge` | household knowledge, admin-curated, shared | `id`, `household_id`, `category`, `content`, `source` |
| `reminders` | timers/reminders; fired when a client polls `/reminders/due` | `id`, `user_id`, `text`, `due_at`, `status` |
| `audit_log` | who did what (device control, admin changes); append-only, capped in code | `id`, `household_id`, `user_id`, `username`, `action`, `detail` |
| `vision_events` | camera events | `id`, `device_id`, `type`, `data` (JSON), `user_id`, `created_at` (last 5000 kept) |
| `device_commands` | outbound queue the volume/device agents pull | `id`, `household_id`, `device_id`, `action`, `params`, `status`, `delivered_at` |
| `device_heartbeats` | camera liveness | `device_id`, `household_id`, `last_seen` — unique per `(household_id, device_id)` via an index in `db.py`, not here (on an upgraded DB the column does not exist yet when `schema.sql` runs) |
| `persons` | recognizable people | `id`, `name` (unique **per household**), `user_id` (→ account for authz) |
| `face_embeddings` | embeddings per person | `id`, `person_id` (→ persons, cascade), `embedding` (JSON), `source` |
| `app_settings` | instance-wide runtime settings | `key` (PK), `value`, `updated_at` |
| `household_settings` | per-household runtime settings — **this is where Smart Home url/token/allowlist live** | `(household_id, key)` PK, `value`, `updated_at` |

Two columns on `conversation_history` carry more weight than their names suggest:

- **`kind`** — `device` and `greeting` turns are shown in the transcript but withheld from the
  model's history. Both are template strings the *system* produced; fed back as assistant prose,
  the model learned to imitate them and began emitting "Okay - the Light is now off." for
  sentences that were not commands.
- **`embedded`** — 0 means this message still owes ChromaDB a vector. The column *is* the pending
  queue (see [WORKFLOWS.md §3](WORKFLOWS.md)); there is no in-memory queue to lose on shutdown.

Long-term recall vectors are **not** in SQLite — they live in ChromaDB (`jarvis_memory_cos`,
cosine space), keyed by the `conversation_history.id`. Note that **facts are never embedded**:
`store_fact` writes to `user_knowledge` in SQLite only, and dedup embeds candidates transiently
without storing the vectors. ChromaDB holds conversation messages, nothing else.

---

## Toolchain

- **Python** ≥ 3.13, managed with **`uv`** (always `uv run …`, never bare `python3`).
- **Tests**: `uv run pytest` · **Lint**: `uv run ruff check src/orchestrator src/scripts tests`.
- **Frontend tests**: `cd frontend && npm test` (`node --test`, no browser needed).
- **CI**: `.github/workflows/ci.yml`, two jobs — ruff + pytest, and npm lint + npm test + a
  production build. One test is deliberately outside CI: `tests/test_face_align.py` cross-checks
  the browser's face alignment against OpenCV's, which needs opencv, 38 MB of models and a
  photograph of a real face. It is manual-only and says so; a "skipped" line is not a pass.
- **Frontend**: Node + Vite (`cd frontend && npm install && npm run build`).
- **Services**: systemd (`llama-fast`, `jarvis-orchestrator`) — see [DEPLOY.md](DEPLOY.md).
