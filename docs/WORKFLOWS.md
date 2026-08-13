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
  3. FAST PATHS, in order           → greeting · volume/gesture · reminder · home command
                                      any of these answers here and RETURNS — no LLM  (see §1a)
  4. chat.build_messages            → assemble the prompt within the token budget  (see §2)
  5. chat.clamp_completion_for      → cap max_tokens so prompt + answer fit -c 4096
  6. memory.Inflight()              → mark a generation active (blocks the fact worker)
  7. llm.request_llm[_stream]       → call llama-server at 127.0.0.1:8081
  8. chat.store_message ×2          → persist user + assistant turns (row lands with embedded=0,
                                      which IS the embedding queue — see §3)
  9. _maybe_title                   → on the first turn, chat.title_from_text names the chat
                                      (no model involved)
 10. (optional) llm.synthesize_tts  → Piper renders the answer to base64 WAV when voice_feedback
```

Notes:
- The **user turn is persisted even on failure** so input is never lost; an error is surfaced as a
  real SSE `error` event, never written into history as if it were the assistant's reply.
- Session `"default"` (or a missing id) resolves to a real, per-user session `u<id>-default`
  (`chat.resolve_session`) — there is no shared, unowned bucket.
- **Chat titles do not use the model.** `chat.title_from_text` picks up to four significant words
  from the first message. The old second LLM request cost 5.7 s of the single llama-server slot
  (2.4 s prefill + 3.3 s generating) for four cosmetic words — time the user waits, since it runs
  before the stream's `done` event — and displaced the conversation from that slot. Set
  `JARVIS_LLM_TITLES=1` to restore the model-written titles; that path warms the prefix back
  afterwards so a checkpoint miss cannot land on the next message.

### 1a. The greeting fast path

A **bare address** — "hi", "hey jarvis", "good morning", or contentless noise like "I" or "um" — is
answered by the server from a time-aware set of acknowledgements (`intents.greeting_reply`) and
**never reaches the model**.

This is not an optimisation. Handed a turn with nothing in it, a 2B model reaches for whatever
context is in front of it. Re-measured against the real model on the box: asked nothing but
"Hey Jarvis", with the live device block in context, it answered *"Sir, the lights, tube light, and
fan are all off."* Nobody asked about the lights. With the device block removed it invented "the
lights, temperature, and security systems are running as configured" — hardware that does not
exist. The system prompt forbids exactly that and is ignored, so the fix is to stop asking.

**What it must never do is swallow a question.** `intents.is_greeting` matches by *exact equality*
against `GREETING_PHRASES` — no prefix matching, no decomposition into words, no length
heuristic — because every one of those let it over-reach. A rule that accepted any short utterance
whose words merely *began* a known greeting classified **"how are you"** as a greeting ("how"
begins "howdy") and answered a question with "Yes, sir.". Questions go to the model, which answers
them well: "How are you?" → *"I am functioning as expected."*, "How's it going?" → *"It's running
at 100% efficiency, sir."* — both measured, both worth their ~20 s.

The asymmetry is the design rule: listing a phrase saves one LLM turn; listing one wrongly replaces
a good answer with a worse one. **When in doubt, leave it out and let the model answer.**

The phrase list lives in `config/greeting_phrases.json`, which is the contract between this
classifier and the browser's (`frontend/src/wake-phrases.js`). Both keep their list as a literal —
`intents.py` is pure, and the browser must not fetch a file to answer "hello" — and both test
suites assert against the fixture, so the two cannot drift. `voice_bridge.py` imports
`intents.is_greeting` outright; it used to carry a third copy that treated any utterance merely
*starting* with a greeting as one, so "Jarvis, hit the lights" was answered "Yes, sir?" and the
light never came on.

Both `/inbox` and `/chat/stream` short-circuit identically.

Greeting turns are stored with `kind='greeting'` and **withheld from the model's history**, the
same rule device acknowledgements live under (§8) — a screenful of "Sir." teaches it to answer
everything with "Sir.". They still appear in the transcript the UI shows.

Covered end-to-end over HTTP in `tests/test_greetings.py`.

---

## 2. Prompt assembly & the token budget  (`chat.build_messages`, `budget.py`)

The model server runs with a **fixed context window** (`-c 4096`). The total of
`prompt tokens + generated tokens` must fit, or llama.cpp silently evicts the oldest prompt
tokens — which previously dropped the system prompt or the question itself.

The prompt is built as **exactly one system message + recent history + the current turn**:

```
[ system ]  =  base system prompt        ← STABLE across turns, on purpose
               + voice brevity rule      (only when the turn came from /voice)
               + HOUSEHOLD KNOWLEDGE     (admin-curated, shared; capped at KNOWLEDGE_TOKEN_CAP=512)
               + USER PROFILE block      (this user's stored facts, same cap)
[ history ]  = most-recent turns, newest-first, added only while they fit the remaining budget
               (device and greeting turns excluded; older than HISTORY_MAX_AGE_HOURS=24 excluded)
[ user ]     = [Seen by cameras: …]      ← VOLATILE, so it hangs off the current turn
               + DEVICES IN THIS HOME    (live state; see §8)
                 …or YOUR OWN STATUS     (when the message asks how JARVIS is — see §2a)
               + RECALLED MEMORIES       (RAG hits from past sessions; see §3)
               + [Already done, by the system, for this message: …]
               + the current message     (always kept)
```

**The split is the point.** llama-server keeps a KV cache keyed on the prompt's leading tokens, so
anything that changes every turn must not go at the front. RAG hits, camera presence and live
device state all change constantly; putting them in the system message would alter the very first
token each turn and force a full re-evaluation of the whole context — ~630 tokens, about 57 s on
this box. They are attached to the **current user turn** instead, where they cost nothing to
re-read. (Qwen also rejects multiple or non-leading system messages, so "just add another system
block" is not available either.)

### 2a. "How are you?" — the reference slot changes subject

A 2B model answers from whatever state-like data sits nearest the question. With the live device
block attached — the normal case when Home Assistant is configured — it answered *"How are you?"*
by reporting the house in **6 of 8** measured samples ("I am functioning normally, sir. The lights
remain on, and the fan continues spinning."). It is not disobeying; it is answering from the only
status it was given.

So `chat.build_messages` fills the same slot with a different subject: `intents.is_self_query`
matches a small closed set of phrasings ("how are you", "are you ok", "what is your status") and
the turn carries `sysinfo.self_status_block()` — uptime, load, memory, what is responding —
instead of the devices. Measured the same way: **0 of 8**.

Three things were tried first and are recorded so they are not tried again:

- **Asking the model not to.** Four system-prompt variants. The most promising ("answer about
  yourself and stop — the state of the home is not part of that answer") scored 0/5 on one run and
  **5/8** on a larger one: noise, not an effect. Adding that rule *and* removing the device nouns
  the honesty clause named scored **worse** than the rule alone. Non-monotonic, which is what
  prompt-tuning at this size looks like.
- **Dropping the block for non-device questions.** Worse than either. With nothing to answer from,
  the model invents hardware — "the air is conditioned", "the temperature is set" — for a house
  that has neither. The slot must always hold something true; only its subject may change.
- **Passing real figures in the status block.** It rendered "4 cores" as "a single CPU core" in six
  replies of eight. The block states bands ("load is light") that cannot become a false number.

The set is deliberately tight. Where "you" and "the house" are genuinely ambiguous — "how's
everything going" — the house may well be the subject, so the device block stays.

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

**Embedding** uses `google/embeddinggemma-300m`. It is loaded from an **exported ONNX pipeline**
(`src/scripts/export_embed_onnx.py`, baked into the images at `/opt/jarvis/embed_onnx`), which
removes torch from the runtime entirely; sentence-transformers remains as a fallback, and is used
only when the ONNX bundle's `meta.json` model matches the configured `EMBED_MODEL_NAME` — a
mismatch would silently produce vectors from a different space. The model is *asymmetric*, so
documents and queries need different prompt prefixes:
- documents: `"title: none | text: <content>"`  (`memory._embed_documents`)
- queries:   `"task: search result | query: <text>"`  (`memory._embed_query`)

The collection uses **cosine** space with normalized vectors; `RAG_DISTANCE_THRESHOLD = 0.6`
(cosine distance = 1 − similarity) discards weak matches. Retrieval (`retrieve_long_term_memory`):
queries the user's own past **user-spoken** lines (assistant chatter is excluded — it crowds out
real facts), skips anything already in the recent context window, dedupes, and returns a block.

**Embeddings never run inline, and they are not a queue in memory.** A message row is written with
`conversation_history.embedded = 0`, and *that column is the pending set*. `memory.enqueue_embedding`
still exists and is still called by `chat.store_message`, but it does nothing: the row is the
handoff.

The flush is batched and waits for the box to go quiet (§4). Measured here, embedding on write cost
~1.2 s per message and ~1.9 s per turn, landing exactly while the next message was being typed —
competing with prompt prefill for the same two cores. Batched at idle it is also 64% cheaper per
message (1183 ms → 425 ms over ten), because one ONNX pass and one Chroma write cover the whole
batch.

Deferring to idle stretches the window in which work can be lost from seconds to minutes, which is
why the pending set had to become durable. A vector that is never written is invisible until
someone asks a question that needed it. So:

- `memory.flush_embeddings(limit=EMBED_FLUSH_BATCH=32)` embeds the oldest pending messages in one
  batch and marks them **only after** the Chroma write returns. A failure leaves them pending and
  the next tick retries; re-doing a batch is harmless, since `add()` is keyed on the message id.
- A database upgraded from before this column marks its existing rows **embedded**, not pending —
  their vectors are already in Chroma, and defaulting to 0 would re-embed the entire history on the
  first idle tick (`db.init_db`, guarded so the backfill runs exactly once).

Safe to defer because recall does not need it sooner: an un-embedded message is by definition
recent, and recent turns are already in the verbatim history window. RAG only has to cover what has
aged out.

---

## 4. The idle worker: embeddings, then facts  (`memory._memory_worker`)

One background thread does both jobs, in that order, and never competes with live chat:

```
every IDLE_CHECK_INTERVAL (30 s):
  if a chat request is in flight (memory.is_busy())              → skip
  if idle ≥ EMBED_IDLE_SECONDS (20 s)
     OR the oldest pending message is ≥ EMBED_MAX_DEFER_S (900 s) old:
        → memory.flush_embeddings()   (§3)
        → if it embedded anything, `continue`: re-check activity before the expensive job
  if last activity < IDLE_THRESHOLD_SECONDS (120 s) ago          → skip
  else: pull up to FACT_EXTRACTION_BATCH (6) un-extracted user messages
        → LLM call with FACT_EXTRACTION_PROMPT (JSON array out)
        → store_fact() each, deduped semantically                (see §5)
        → mark exactly those messages processed
        → llm.warm_prefix(chat.last_system_prefix()) to put the conversation back in the KV cache
```

Three things in that order are deliberate:

- **Embeddings first, on a shorter fuse.** They are cheap, bounded and needed for recall;
  extraction is a multi-minute LLM call. The other order would leave vectors queued behind the
  slowest job on the box.
- **Never concurrently.** The embedder and llama.cpp would fight over the same two cores, which is
  the contention this design exists to remove.
- **The age valve.** An unbroken conversation never reaches the idle threshold, so without
  `EMBED_MAX_DEFER_S` "defer to idle" would mean "never embed at all".

The **in-flight guard** matters: at ~5 tok/s a long answer can outlast the 120 s idle threshold,
so idle-time alone isn't enough — `Inflight` (a context manager around every generation) ensures
the extractor waits for the single LLM slot.

Extraction spends that slot on a prompt sharing nothing with any chat, so it finishes by warming
the conversation's system prefix back into the cache. llama.cpp will often restore it from a
context checkpoint by itself; checkpoints are a bounded resource, and a miss costs the user a full
re-evaluation.

---

## 5. Fact dedup  (`memory.store_fact` / `_find_duplicate_fact`)

New facts are merged into an existing one only if they're a true **semantic restatement**:
the new fact and existing facts in the same category are embedded in one batch, and merged when
cosine similarity ≥ `FACT_DEDUP_SIM (0.90)`. (If the embedding model is unavailable, a stricter
word-overlap fallback at `0.85` is used.) This avoids the old bug where "lives in Springfield" and
"lives in Shelbyville" — which share most words — were wrongly treated as the same fact.

---

## 6. The voice loop  (`src/scripts/run_listener.sh` → `src/scripts/voice_bridge.py`)

```
mic → whisper-stream (continuous transcription to stdout)
    → voice_bridge.py reads the transcript and gates on the wake word "jarvis"
    → "jarvis" alone            → GET /greeting        (spoken, no LLM)
      "jarvis, <anything else>" → POST /inbox as JSON  (Bearer = config/voice_listener.key)
    → orchestrator runs the chat workflow (§1)
    → [voice_feedback] Piper WAV in the response → played locally (paplay/aplay/ffplay)
```

Requests are made with **urllib, from Python — there is no shell anywhere in this path**, so
transcribed audio can never be executed as a command. (It replaced a `whisper-command -cmd "curl …
%s"` line that was both unsafe by design and non-functional: `-cmd` takes a *commands file*, not a
shell template.)

The listener authenticates with a **real, revocable API key** (an `api_keys` row, read from
`config/voice_listener.key`) — not a special bypass. The key also identifies *which user* the
voice conversation belongs to, so it lands in that user's history and memory. Mint it
**device-scoped**: an always-on microphone in a room is a principal anyone within earshot can
drive, and the middleware strips admin from a device-scoped key unconditionally, so a
mis-transcription or a stolen key cannot reach `/admin/*`.

This is the *server-side* listener, for a box with a microphone attached. The browser has its own,
independent path — see §6a.

### 6a. Speech in the browser  (since v3.2.0)

The web UI does **not** send audio to the server. Both the wake word and speech-to-text run in the
tab:

- **Wake word** — an openWakeWord ONNX model via onnxruntime-web (`wake-detect.js`,
  `wake-worker.js`), fed 1280-sample chunks. While armed, the only thing done with microphone
  audio is keyword spotting: nothing is buffered, nothing is transcribed, nothing leaves the tab.
  Phrases openWakeWord has no model for ("jarvis, are you there", "wake up jarvis") are caught by
  transcribing the short utterance and matching it against a phrase list (`wake-phrases.js`).
- **STT** — Whisper via transformers.js in a worker (`stt-worker.js`, `whisper-worker.js`).
- **VAD** — `vad.js`, shared by the push-to-talk mic button and `/voice` live mode: an adaptive
  noise floor seeded from the room, then either "has this take ended?" or continuous segmentation
  of the room into utterances.

Models are served by the orchestrator from its own static mounts (`/stt-models`, `/wake-models`,
`/ort`) rather than fetched from a CDN, so the browser side is as offline as the rest.

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

HA settings are runtime-mutable: startup loads them from the **`household_settings`** table for the
primary household (env vars win), and the admin **Smart Home** tab saves + applies them live via
`ha.configure()` — no restart. They used to live in the instance-global `app_settings`; a smart home
belongs to the household that owns it, and leaving the long-lived token in a global table would mean
any admin of any household — including a demo visitor — could reach it. `db._migrate_ha_settings_to_household`
copies the old keys forward on first start (copied, not moved, so a rollback still works).

---

## 9. Outbound HTTP  (`safehttp.py`)

Every URL the server was *told* to fetch — MCP endpoints, Home Assistant — goes through
`safehttp.urlopen` rather than `urllib` directly. The risk it exists for is not the textbook SSRF:

- **Redirects.** A remote endpoint answers `302 Location: …` and picks where the next request goes.
  urllib follows it without asking and **forwards the original `Authorization` header**, so a
  hostile MCP server can harvest the Home Assistant token by bouncing the request somewhere it
  controls. Redirects are capped, re-checked against the same rules as the original URL, and
  stripped of credentials the moment the host changes.
- **Private addresses are deliberately allowed.** Jarvis is a LAN-first box whose purpose is
  talking to services on 192.168.x.x; Home Assistant *is* on the LAN, and an MCP server on
  127.0.0.1 is a normal way to run one. Blocking those would break the product to defend against
  an admin attacking their own machine. Link-local (169.254.0.0/16, cloud-metadata territory) is
  blocked, demo visitors are kept off these endpoints at the route level, and an exposed
  deployment can tighten to the textbook rule with `JARVIS_HTTP_ALLOW_LOCAL=0`.

Stated rather than papered over: the guard resolves the hostname and urllib resolves it again when
it connects, so DNS rebinding between those two moments defeats the address check. Closing that
needs a custom transport; against an admin-only surface it is not worth the machinery.
