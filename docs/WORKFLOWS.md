# Workflows

> Visual companion: **[DIAGRAMS.md](DIAGRAMS.md)** — each workflow below as a flow diagram.

How the moving parts actually behave at runtime. File references point at the orchestrator
modules under `src/orchestrator/`.

---

## 1. A chat request (`/inbox` and `/chat/stream`)

Both endpoints share the same front-matter (`_validate_chat` in `main.py`); `/inbox` returns one
JSON blob, `/chat/stream` streams Server-Sent Events.

```
client → POST /inbox|/chat/stream  (Bearer token)
  1. auth middleware (main.py)      → resolve user from session token or API key
                                      → per-user rate limit (429 if exceeded)
  2. _validate_chat                 → trim text, length check, resolve_session, ownership check
  3. chat.build_messages            → assemble the prompt within the token budget  (see §2)
  4. chat.clamp_completion_for      → cap max_tokens so prompt + answer fit -c 4096
  5. memory.Inflight()              → mark a generation active (blocks the fact worker)
  6. llm.request_llm[_stream]       → call llama-server at 127.0.0.1:8081
  7. chat.store_message ×2          → persist user + assistant turns (enqueues embedding async)
  8. _maybe_title                   → on the first real turn, a 1-line LLM call titles the chat
  9. (optional) llm.synthesize_tts  → Piper renders the answer to base64 WAV when voice_feedback
```

Notes:
- The **user turn is persisted even on failure** so input is never lost; an error is surfaced as a
  real SSE `error` event, never written into history as if it were the assistant's reply.
- Session `"default"` (or a missing id) resolves to a real, per-user session `u<id>-default`
  (`chat.resolve_session`) — there is no shared, unowned bucket.

---

## 2. Prompt assembly & the token budget  (`chat.build_messages`, `budget.py`)

The model server runs with a **fixed context window** (`-c 4096`). The total of
`prompt tokens + generated tokens` must fit, or llama.cpp silently evicts the oldest prompt
tokens — which previously dropped the system prompt or the question itself.

The prompt is built as **exactly one system message + recent history + the current turn**:

```
[ system ]  =  base system prompt
               + USER PROFILE block   (all stored facts, capped at KNOWLEDGE_TOKEN_CAP=512 tok)
               + RECALLED MEMORIES    (RAG hits from past sessions; see §3)
[ history ]  = most-recent turns, newest-first, added only while they fit the remaining budget
[ user ]     = the current message  (always kept)
```

Token counting uses a deliberately conservative **char-based estimate** (`~4 chars/token`,
`budget.estimate_tokens`) — there is no tokenizer in-process. The budget:

```
prompt_budget = MAX_CONTEXT_TOKENS − reserved_completion − PROMPT_SAFETY_MARGIN
```

`clamp_completion` then caps the requested `n_predict` to whatever the window has left after the
assembled prompt, never below `MIN_COMPLETION_TOKENS=64`. These pure functions live in `budget.py`
and are covered by `tests/test_budget.py`.

---

## 3. Long-term memory (RAG)  (`memory.py`)

Two complementary stores:

- **`user_knowledge`** (SQLite) — curated, full-sentence facts ("The user lives in Springfield"), injected
  wholesale into the system prompt (capped). Survives chat deletion.
- **ChromaDB `jarvis_memory_cos`** — every message embedded as a vector for semantic recall.

**Embedding** uses `google/embeddinggemma-300m`, which is *asymmetric* — documents and queries need
different prompt prefixes:
- documents: `"title: none | text: <content>"`  (`memory._embed_documents`)
- queries:   `"task: search result | query: <text>"`  (`memory._embed_query`)

The collection uses **cosine** space with normalized vectors; `RAG_DISTANCE_THRESHOLD = 0.6`
(cosine distance = 1 − similarity) discards weak matches. Retrieval (`retrieve_long_term_memory`):
queries the user's own past **user-spoken** lines (assistant chatter is excluded — it crowds out
real facts), skips anything already in the recent context window, dedupes, and returns a block.

**Embeddings never run inline.** `chat.store_message` calls `memory.enqueue_embedding`, and a
dedicated daemon thread (`memory._embedding_worker`) drains the queue and writes vectors — so a
slow 300M forward pass on a no-AVX2 CPU never blocks the chat response.

---

## 4. Idle-time fact extraction  (`memory.py`)

A background thread (`memory._memory_worker`) builds the user-knowledge base without ever competing
with live chat:

```
every IDLE_CHECK_INTERVAL (30 s):
  if a chat request is in flight (memory.is_busy())            → skip
  if last activity < IDLE_THRESHOLD_SECONDS (120 s) ago        → skip
  else: pull up to 20 un-extracted user messages
        → LLM call with FACT_EXTRACTION_PROMPT (JSON array out)
        → store_fact() each, deduped semantically               (see §5)
        → mark exactly those messages processed
```

The **in-flight guard** matters: at ~5 tok/s a long answer can outlast the 120 s idle threshold,
so idle-time alone isn't enough — `Inflight` (a context manager around every generation) ensures
the extractor waits for the single LLM slot.

---

## 5. Fact dedup  (`memory.store_fact` / `_find_duplicate_fact`)

New facts are merged into an existing one only if they're a true **semantic restatement**:
the new fact and existing facts in the same category are embedded in one batch, and merged when
cosine similarity ≥ `FACT_DEDUP_SIM (0.90)`. (If the embedding model is unavailable, a stricter
word-overlap fallback at `0.85` is used.) This avoids the old bug where "lives in Springfield" and
"lives in Shelbyville" — which share most words — were wrongly treated as the same fact.

---

## 6. The voice loop  (`src/scripts/run_listener.sh`)

```
mic → whisper-command (wake word "Jarvis")
    → transcribe the following sentence
    → curl POST http://localhost:5000/inbox  (Bearer = config/voice_listener.key)
    → orchestrator runs the chat workflow (§1)
    → [voice_feedback] Piper WAV in the response → play on the speaker
```

The listener authenticates with a **real, revocable API key** (an `api_keys` row, read from
`config/voice_listener.key`) — not a special bypass. The key also identifies *which user* the
voice conversation belongs to, so it lands in that user's history and memory.

---

## 7. Auth & sessions  (`main.py` middleware, `auth.py`)

```
POST /auth/login  → verify PBKDF2 hash → issue a 30-day session token (auth_sessions)
every request     → middleware checks: 1) session token  2) per-user API key
                    → 401 (no/malformed header) · 403 (bad token) · 429 (rate limit)
POST /auth/logout → delete the session row server-side (real revocation)
```

There is **no master key**. Bootstrap and lockout recovery use the local CLI
`src/scripts/manage.py` (`create-admin`, `reset-password`, `mint-key`).


## 8. Device control & LLM tools  (`main.py` tools, `ha.py`, `intents.py`)

Two paths lead to a device action; **both end at the same code-side gates** — the LLM is never the
authorization boundary.

1. **Deterministic fast-path**: common phrasings ("volume up", "set a timer for 5 minutes") are
   parsed by `intents.py` and acted on directly — no LLM round-trip, millisecond acks.
2. **LLM tool call**: the model is offered a small tool menu (`TOOLS_SPEC`): `set_volume`,
   `create_reminder`, `get_presence` — plus `home_control` / `home_status` **only when Home
   Assistant is configured**. `_run_tool_calls` executes the first call in the reply.

Every executing tool passes, in order:
- `_can_control_devices` (admin, or the per-user flag) → refusal message if not;
- the optional **presence gate** (`require_presence_for_device_control`: a camera must currently
  recognize an authorized person);
- for Home Assistant: `ha.resolve_entity()` maps the model's words ("kitchen light") onto the
  **entity allowlist** — exact id, else name-word match; a bare domain word ("the switch") only
  resolves when unique; **ambiguity is refused, never guessed**;
- the action itself: volume → a validated command **enqueued** for the pull-agent
  (`device_commands`); HA → `POST /api/services/homeassistant/turn_on|turn_off|toggle` with the
  server-held token (5 s timeout, fail-soft);
- the **audit log** (`device.volume`, `device.home_assistant`, …).

Between the regex fast-path and the clarify guard sits the **semantic router** (`intent_router.py`):
the utterance is embedded (the same ONNX embedder as RAG) and compared by cosine against per-device
exemplar phrases — generic command templates plus function-class paraphrases ("it is hot in here" for
a fan). Confident match (≥0.80) → act; plausible (≥0.63) → propose and ask ("Should I turn on the
fan?" — a per-session pending proposal with a 2-minute TTL consumes the next yes/no); below → normal
chat. Automations/scripts/scenes are never auto-fired from a fuzzy match (always confirm), an
ambiguity margin refuses close calls, and the thresholds were calibrated against the real embedder
(negative ceiling 0.627 vs positive floor 0.656). The exemplar index rebuilds in the background at
startup and whenever the admin saves the allowlist.

HA settings are runtime-mutable: startup loads them from the `app_settings` table (env vars win),
and the admin **Smart Home** tab saves + applies them live via `ha.configure()` — no restart.
