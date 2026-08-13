# API Reference

Base URL: `http://<host>:5000`. All responses are JSON unless noted.

## Authentication

Every endpoint except the unauthenticated set below requires a **Bearer token** — either a web-login **session token** or a per-user
**API key**:

```
Authorization: Bearer <token>
```

Responses: `401` missing/malformed header · `403` invalid/expired token or non-admin on an admin
route · `429` rate limit exceeded (per user, `rate_limit_requests_per_minute`; every shipped
config sets **120/min**).

**Unauthenticated** (`PUBLIC_PATHS` / `PUBLIC_PREFIXES` in `main.py` — the authoritative list):
`/health`, `/`, `/admin`, `/voice`, `/auth/login`, `/demo/session`, `/ca.crt`,
`/favicon.svg|.png|.ico`, and the prefixes `/assets/`, `/static/`, `/stt-models/`, `/face-models/`,
`/wake-models/`, `/ort/`. Note `/voice` and `/demo/session` in particular: they are public.

Input is capped at 500 characters per message for non-admins and 10000 for admins. The cap keys on
`is_admin` alone, so a NON-admin user's API key also gets 500, and a device-scoped key never counts
as admin. Attachments have a separate 48000-character total budget.

---

## Auth

### `POST /auth/login`
Body: `{ "username": str, "password": str }` → `{ "token": str, "role": "user"|"admin" }`
(token valid 30 days; expired tokens are purged opportunistically).

Login is throttled per-username (brute-force guard). Username ≤ 64, password ≤ 256 chars.

### `POST /auth/logout`
Revokes the caller's current session token server-side. → `{ "status": "ok" }`

### `POST /auth/logout-all`
Revokes **every** session for the caller ("log out everywhere"). → `{ "status": "ok", "revoked": int }`

### `POST /auth/password`
Body: `{ "current_password": str, "new_password": str }`
→ `{ "status": "ok", "other_sessions_revoked": int }`

Verifies the **current password** rather than trusting the session: a stolen token must not be
enough to take over an account. On success every *other* session is revoked and the caller stays
signed in where they are — so if the reason for changing it was a leak, the same action closes it.
`400` if the new password equals the old one; `403` (not `401`) if the current password is wrong.

A successful login also opportunistically re-hashes any legacy `<salt>:<hex>` password to PBKDF2 —
the plaintext is in hand exactly there — so old accounts self-heal without user action.

### `POST /demo/session`
Mints a throwaway demo household and returns a session for it. `404` on any runtime that is not the
public demo (`demo_public_signup`). **Public — no token required.** Logging out of a demo session
destroys the household and everything in it.

### `GET /demo/status`
→ `{ "demo": false }`, or
`{ "demo": true, "expires_at": str, "seconds_remaining": int, "ttl_minutes": int }`
for the countdown banner. Deliberately a *passive* path: polling it does not count as activity, or
the countdown would top itself up simply by being watched.

---

## Chat

Both chat endpoints accept the **QueryRequest** body:

| Field | Type | Default | Notes |
|---|---|---|---|
| `text` | str | — | 1–10000 chars (≤500 for non-admins) |
| `session_id` | str | `"default"` | `"default"`/missing → the user's personal session |
| `temperature` | float? | config | sampling |
| `top_k`,`top_p`,`min_p` | num? | — | sampling |
| `repeat_penalty`,`presence_penalty`,`frequency_penalty` | float? | — | sampling |
| `n_predict` | int? | — | 1–8192; clamped to fit the context window |
| `seed` | int? | — | reproducibility |
| `system_prompt` | str? | config | overrides the system prompt (≤2000 chars) |
| `voice_feedback` | bool | `false` | if true, response includes Piper TTS audio |
| `reasoning` | bool? | config | `true` strips `/no_think` from the system prompt (thinking on), `false` ensures it, `null` leaves the configured default alone |
| `voice` | bool | `false` | set by the `/voice` page. Adds a server-side brevity instruction — a reply that scans fine on screen is half a minute of Piper talking, and there is no skimming audio. Deliberately not a client-supplied `system_prompt`, so the persona stays defined in one place |
| `attachments` | list | `[]` | ≤3 items of `{ "name": str, "content": str }`, ≤48,000 chars in total. Text extracted **in the browser**; nothing is written to the server. Wrapped in `<attachment>` tags and labelled as untrusted reference material, not instructions |

### `POST /inbox`  (non-streaming)
→ `{ "response": str, "speed": str, "new_title": str|null, "audio": base64|null }`

Several things are answered **without reaching the LLM**, on both `/inbox` and `/chat/stream`:

- **A bare address.** "hi", "hey jarvis", "good morning", or contentless noise like "I" or "um" get
  a time-aware acknowledgement ("At your service, sir."). Handed a turn with nothing in it, a 2B
  model reaches for whatever context is in front of it and starts reciting household state, so it
  is never asked. Matched by **exact equality** against `config/greeting_phrases.json` — anything
  else reaches the model, including questions *about* Jarvis ("how are you", "what's up", "are you
  ok") and of course commands ("hey jarvis, turn off the fan").
- **Volume commands** ("set volume to 50%", "volume up", "mute") — authorized via
  `deps.can_control_devices`, enqueued to the device agent, acknowledged with a short spoken reply.
- **Reminders** ("remind me to … in 20 minutes").
- **Home commands** — see the intent ladder in [DIAGRAMS.md](DIAGRAMS.md) §2. If the utterance says
  more than the command ("I'm freezing, turn the fan off"), the action still happens immediately and
  the LLM words the reply instead.

`new_title` is set on the first turn of a conversation and comes from `chat.title_from_text` — **no
model call**. `JARVIS_LLM_TITLES=1` restores the old model-written titles.

### `POST /chat/token-estimate`
Same **QueryRequest** body. Assembles the prompt exactly as a real turn would and reports its size
**without generating anything or persisting a turn** — this is what the composer's live counter uses.
→ `{ "tokens": int, "context_tokens": int, "available_tokens": int, … }`

### `POST /chat/stream`  (Server-Sent Events)
`Content-Type: text/event-stream`. Each line is `data: <json>`:
```
data: {"content": "<token chunk>"}        # repeated as the answer streams
data: {"error": "AI backend error"}       # only on backend failure
data: {"done": true, "new_title": "...", "audio": "<base64>"}   # final event (fields optional)
```

---

## Sessions

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET` | `/sessions` | — | `{ "sessions": [{id, title, created_at}] }` |
| `POST` | `/sessions` | — | `{ "id": str, "title": "New Chat" }` |
| `PUT` | `/sessions/{id}` | `{ "title": str }` | `{ "status": "ok" }` |
| `DELETE` | `/sessions/{id}` | — | `{ "status": "ok" }` (also cleans vectors) |
| `GET` | `/history/{id}` | — | `{ "messages": [{role, content}], "count": int }` |

Ownership is enforced: acting on another user's session returns `403`.

---

## Knowledge (Memory Core)

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET` | `/knowledge` | — | `{ "facts": [{id, category, content, source, created_at, updated_at}], "count": int }` |
| `POST` | `/knowledge` | `{ content, category? }` | `{ "id": int, "status": "ok" }` |
| `PUT` | `/knowledge/{id}` | `{ content, category? }` | `{ "status": "ok" }` |
| `DELETE` | `/knowledge/{id}` | — | `{ "status": "ok" }` |
| `POST` | `/knowledge/extract-now` | — | **admin** · `{ "status": "ok", "processed": int }` |
| `GET` | `/admin/knowledge/global` | — | **admin** · household facts shared by all users `{ "facts": [...], "count": int }` |
| `POST` | `/admin/knowledge/global` | `{ content, category? }` | **admin** · add a household fact (a loader/Claude Code can call this). `{ "id": int, "status": "ok" }` |
| `PUT` | `/admin/knowledge/global/{id}` | `{ content, category? }` | **admin** · `{ "status": "ok" }` |
| `DELETE` | `/admin/knowledge/global/{id}` | — | **admin** · `{ "status": "ok" }` |
| `POST` | `/admin/knowledge/global/chat` | `{ text }` | **admin** · "global chat" — each non-empty line becomes a household fact. `{ "reply", "saved": [...], "count" }` |
| `GET` | `/presence` | — | any authed user · `{ "present": [name, …] }` — people the cameras recognized in the last ~3 min. |
| `GET` | `/arrivals?since_id=N` | — | any authed user · `{ "arrivals": [{id, name, created_at}] }` — recent "someone arrived" events for the UI to greet. |
| `GET` | `/reminders` | — | `{ "reminders": [{id, text, due_at, status, created_at}] }` — your pending reminders. (Create them by chatting: "remind me … in 20 min".) |
| `GET` | `/reminders/due` | — | your pending reminders whose time has arrived `{ "due": [{id, text, due_at}] }`. |
| `POST` | `/reminders/{id}/ack` | — | mark a fired reminder done. `{ "status": "ok" }` |
| `DELETE` | `/reminders/{id}` | — | cancel a pending reminder. `{ "status": "ok" }` |
| `GET` | `/admin/audit?limit=N` | — | **admin** · `{ "entries": [{id, created_at, user_id, username, action, detail}] }` — recent audit entries (device + admin actions), newest first, scoped to the caller's household. `limit` is clamped to 1–1000. |
| `POST` | `/admin/backup` | — | **admin** · create a backup now. `{ "status", "name", "size" }` |
| `GET` | `/admin/backups` | — | **admin** · `{ "backups": [{name, size, created_at}] }`. |
| `GET` | `/admin/backups/{name}` | — | **admin** · download the `.tar.gz`. |
| `DELETE` | `/admin/backups/{name}` | — | **admin** · `{ "status": "ok" }` |

Valid categories: `personal, family, preferences, location, work, education, interests, technical, other`.

---

## Events (camera devices)

| Method | Path | Body | Returns |
|---|---|---|---|
| `POST` | `/events` | `{ device_id, type, ts?, data? }` | `{ "status": "ok", "id": int }` — ingest a camera/vision event. **A device-scoped API key records the event under its own bound device (the body `device_id` can't spoof another); admins may post as any device; plain users are denied.** `device_id` is `[A-Za-z0-9._:-]`; `data` ≤ 4 KB; only the last 5000 events are retained. |

Used by the camera agent (`camera/`) to report high-level events (`motion`, `face_seen`, `pose`,
`gesture`); `data` is type-specific JSON. No imagery is sent. A special `type:"heartbeat"` is **not**
stored in the events feed — it upserts the device's `last_seen` in `device_heartbeats` (powers the
admin "Camera · …" active/inactive status); the agent pings it ~every 30s.

---

## Devices (control)

| Method | Path | Body | Returns |
|---|---|---|---|
| `POST` | `/devices/volume` | `{ action: set\|step\|mute\|unmute, value?, device? }` | `{ "status": "ok", "id": int }` — enqueue a volume command. **Authorized** (admin, or user with `can_control_devices`); `set` needs `value` 0–100, `step` a signed delta. **`403` in demo mode** (`deps.require_not_demo`). |
| `POST` | `/devices/gesture` | `{ y: float }` | `{ "active": bool, "expires_in"?: int }` — the camera reports normalized hand height while a gesture mode is running, and the server maps movement to volume steps for that mode's target. **Device-scoped key required** (or admin + `?device=`), and only while a voice-authorized gesture mode is live for *that* camera — so the camera key itself needs no device-control permission. `active:false` tells the camera to stop reporting. **`403` in demo mode.** |
| `GET` | `/devices/commands?device=&wait=` | — | `{ "commands": [{id, action, params}] }` — device agents **pull** their pending commands (long-poll up to `wait`s; delivered commands aren't re-served). **The API key must be bound to that `device` (or be an admin)** — a key for one device can't drain another's queue. Claimed with a single `UPDATE … RETURNING`, so two concurrent pollers cannot double-deliver. Concurrent long-polls are capped at 16. **`403` in demo mode.** |

The Windows volume agent (`clients/volume-agent/`) pulls + applies these. The orchestrator only
ever enqueues — the agent opens no inbound port. Authorization is enforced server-side, never by
the LLM.

**LLM tools** (`set_volume`, `create_reminder`, `get_presence`, and — when Home Assistant is
configured — `home_control`/`home_status`) execute through the same server-side gates: the model
only *proposes*; `deps.can_control_devices`, the optional presence gate, the HA entity **allowlist**,
and the audit log decide. Ambiguous device names are refused, never guessed.

---

## Admin  (all require an admin token)

| Method | Path | Body | Returns |
|---|---|---|---|
| `POST` | `/admin/users` | `{ username, password, role? }` | `{ "status": "ok" }` (`400` if username exists) |
| `GET` | `/admin/users` | — | `{ "users": [{id, username, role, created_at, total_chats, total_messages}] }` |
| `PUT` | `/admin/users/{id}/role` | `{ role: "user"\|"admin" }` | `{ "status": "ok", "role" }` — promote/demote; **`400` if it would demote the last admin**. Live for the user's existing session. |
| `DELETE` | `/admin/users/{id}` | — | `{ "status": "ok" }` (cannot delete self; **`400` on the last admin**) |
| `POST` | `/admin/api_keys` | `{ user_id, description, device_id? }` | `{ "key": "jk-…", "device_id" }` — full key shown once (hashed at rest). A `device_id` (`[A-Za-z0-9._:-]`) mints a **device-bound** key (required for a camera/edge agent; such keys can never wield admin even if the user is admin). |
| `GET` | `/admin/api_keys` | — | `{ "keys": [{id, key_string(prefix only), user_id, description, device_id, usage_count, last_used_at, created_at}] }` |
| `DELETE` | `/admin/api_keys/{id}` | — | `{ "status": "ok" }` |
| `GET` | `/admin/stats` | — | `{ "users": int, "chats": int, "messages": int }` |
| `GET` | `/admin/services` | — | `{ "services": [{name, status: active\|inactive, detail}] }` — live subsystem health (orchestrator, LLM, embeddings, TTS, **Home Assistant** when configured, + one row per camera agent from `device_heartbeats`). |
| `GET` | `/admin/home-assistant` | — | `{ owned, configured, url, token_set, allowed_entities, env_managed, connected }` — Smart-Home config for the admin UI. **The token itself is never returned** (only `token_set`). An admin of a household that does not own the smart home gets `owned:false` and empty values rather than a `403` — reporting "unconfigured" avoids confirming that some *other* household has one. |
| `PUT` | `/admin/home-assistant` | `{ url, token?, allowed_entities? }` | Save to the DB (`household_settings`) + apply **live** (no restart). Blank/omitted `token` keeps the stored one. `409` when env-managed. Audited (`ha.config`). **`403` unless the caller's household owns the smart home** (`_require_smart_home`). |
| `POST` | `/admin/home-assistant/test` | `{ url?, token? }` | `{ ok, detail }` — probe a URL/token **before** saving (blank token = use stored). **`403` unless the caller's household owns the smart home.** |
| `GET` | `/admin/home-assistant/entities` | — | `{ entities: [{entity_id, name, state, domain, allowed}] }` — controllable devices (lights/switches/…) for the allowlist picker. **`403` unless the caller's household owns the smart home.** |

`_require_smart_home` is the authorization check the whole household boundary exists to support: a
household that does not own the HA connection cannot read its config, enumerate its entities, or
actuate anything in it. Demo households never own one, so **the demo has no smart home by
construction** — not by a mode flag someone could forget to check on a new route.
| `GET` | `/admin/events?limit=N&type=&since_id=` | — | `{ "events": [{id, device_id, type, data, created_at}], "count": int }` (recent camera events, newest first). `type` filters (e.g. `face_seen` for the recognitions feed / verify); `since_id` returns only events newer than an id. |

---

## MCP servers  (admin; **`403` in demo mode**)

Discovery and review only. **Nothing here executes an MCP tool**, and nothing wires one into the
model's tool menu — that waits on a per-tool allowlist and an answer to whose authority a remote
tool would run under.

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET` | `/mcp/servers` | — | `{ "servers": [{name, url, type, description}] }` |
| `POST` | `/mcp/servers` | `{ name, url, type?, description? }` | `{ "status": "ok", "server": {…} }` · `400` on a rejected URL |
| `DELETE` | `/mcp/servers/{name}` | — | `{ "status": "ok" }` · `404` if unknown |
| `POST` | `/mcp/test` | `{ url }` | `{ "ok": bool, "detail": str }` — a **real MCP protocol handshake**. It used to be an HTTP ping that counted 401/404/405 as success, so any web server on the internet passed |
| `GET` | `/mcp/servers/{name}/tools` | — | `{ "server": name, "tools": [...] }` — discover a configured server's tools for review · `404` unknown server · `502` if discovery fails |

The mutating routes are `deps.require_not_demo` **as well as** `deps.require_admin`, and that combination is
the actual fix: in demo mode every visitor is an admin of their own household, while the MCP server
list is process-wide — so admin alone would have let a visitor add an entry everyone sees and make
the box fetch a URL of their choosing. All outbound fetches go through `safehttp` (redirect cap,
per-hop re-validation, credentials dropped on host change).

---

## Models  (admin)

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET` | `/models` | — | `{ "models": [{id, name, size_mb, active, requested}], "active": str, "requested": str\|null }` — the GGUFs under `models/`, with `active` read from llama-server's own `/props`. Paths are **never** returned: they are a server implementation detail |
| `POST` | `/models/switch` | `{ model }` | stages the choice in `config/active_model.json` and reports `restart_required` |

`POST /models/switch` deliberately **does not restart anything**. The llama-server process belongs
to systemd or Docker, not to the web app, so the honest answer is "noted, it takes effect on the
next restart" — the alternative is a UI claiming one model while the generations come from another.

---

## Voice / TTS

| Method | Path | Body | Returns |
|---|---|---|---|
| `POST` | `/tts` | `{ text }` (≤600) | `{ "audio": "<base64 WAV>" }` — synthesize speech (Piper); `503` if TTS unavailable. The web UI uses this to **speak the greeting**. |
| `GET` | `/greeting` | — | `{ "text", "audio" }` — a time-aware JARVIS acknowledgement + spoken audio. The voice bridge calls this when it hears just the wake word ("Jarvis" → "Yes, sir?"). |
| `GET` | `/voice/mics` | — | **admin** · `{ mics: [{id, name, active}], driver, error, selected, stale, listener_restart_required }` — capture devices attached to the **server**, enumerated through SDL so the ids match `whisper-stream -c`. `stale:true` means the chosen device isn't currently attached. An empty list with no error usually means the container has no `/dev/snd`. |
| `POST` | `/voice/mics/select` | `{ capture_id }` (`-1` = system default) | **admin** · stage the listener's microphone; `404` if that device isn't present. Stores the device **name** alongside the index, and returns `{status:"restart_required", selected, message}` — the listener holds its stream open, so it applies on restart. |
| `GET` | `/voice/inputs` | — | **any member** · `{ inputs: [{device, name, card}], error, busy }` — server microphones offered as a source for a *user's own* voice input. ALSA PCM ids from `/proc/asound` (`plughw:<card>,<dev>`), which are stable across replug. `403` in a demo household. |
| `GET` | `/voice/server-mic/stream?device=…` | — | **any member** · raw 16 kHz mono PCM (`s16le`) from that device, for the browser to wrap back into a MediaStream. `device` must be one `/voice/inputs` listed (it becomes an ffmpeg argument). `404` unknown device · `409` already in use · `503` with ffmpeg's own reason if it won't open. One session at a time, 15-minute cap, audit-logged, refused in demo households. |

`/inbox` and `/chat/stream` also return `audio` when the request sets `voice_feedback: true` (the
voice bridge uses this to speak replies).

---

## Faces (recognition data)

Detection/recognition run on the device; the server **stores embeddings** only (never imagery). Data
model: a **person** (`persons`) has many **embeddings** (`face_embeddings`) — recognition matches the
best of them. A person can be **linked to a user account** so identity drives per-user authorization.

**Manage (admin):**

| Method | Path | Body | Returns |
|---|---|---|---|
| `POST` | `/faces/enroll` | `{ name, embedding[8..2048], source?, replace? }` | **admin** · add an embedding to a person (creating them if new); `replace:true` clears their set first. |
| `POST` | `/faces/identify` | `{ embedding[8..2048] }` | `{ name, score }` — who this vector belongs to. `name` is the person, `"unknown"` below the 0.363 cosine threshold, or `null` if nobody is enrolled. Nothing is stored. Any household member. |
| `GET` | `/faces/enrolled` | — | **device key or admin** · `{ "enrolled": { name: [embedding, …] } }` — every template in the household, for an always-on agent that matches locally. Not readable by ordinary members: use `/faces/identify`. |
| `GET` | `/admin/faces` | — | **admin** · `{ "faces": [{id, name, user_id, username, embedding_count, last_seen, created_at}] }`. |
| `GET` | `/admin/faces/{id}/embeddings` | — | **admin** · `{ "embeddings": [{id, source, created_at}] }` for a person. |
| `PUT` | `/admin/faces/{id}` | `{ name?, user_id? }` | **admin** · rename (UNIQUE) and/or link a user (only fields sent change; `user_id:null` clears). |
| `DELETE` | `/admin/faces/{id}` | — | **admin** · delete a person + all their embeddings. |
| `DELETE` | `/admin/faces/embeddings/{id}` | — | **admin** · delete one embedding (person stays). |

**Enroll from the web UI** — the browser that has the camera does the whole job: it detects, aligns
and embeds frames locally (YuNet + SFace in a worker) and `POST`s only the resulting vector to
`/faces/enroll`. No imagery reaches the server, so there is no capture to queue on a remote device
and no preview to relay — the endpoints that did that (`/admin/faces/enroll-request`,
`/faces/enroll-request`, `/faces/enroll-result`, `/faces/enroll-preview[-stream]`) are **gone**.
Recognition then goes through `/faces/identify`, so the household's templates are never handed to a
client to compare against. A headless device with no browser enrolls via
`jarvis_camera.facecli add` (admin key), which posts to the same `/faces/enroll`.


---

## Misc / unauthenticated

| Method | Path | Returns |
|---|---|---|
| `GET` | `/health` | `{ "status": "ok\|offline", "model": "Qwen3.5-2B-Q4_K_M", "detail": "…", "n_ctx": 4096, "mode": "production", "demo_signup": false, "demo_ttl_minutes": null }` — `status` is the LLM's reachability, and `model` falls back to a display name when llama-server cannot be read |
| `GET` | `/system` | **admin** · live host telemetry: `{ load1, cpus, cpu_pct, mem_used_mb, mem_total_mb, mem_pct, uptime_sec }` (dependency-free, from `/proc` + `os`) |
| `GET` | `/` | React SPA (`frontend/dist/index.html`) |
| `GET` | `/admin` | Serves the React SPA, which renders the admin console (admin-gated client-side + on every `/admin/*` endpoint) |
| `GET` | `/voice` | Serves the same SPA, which renders live voice mode. **Public**, like `/` and `/admin` — the page itself is just the bundle; everything it calls is authenticated |
| `GET` | `/favicon.{ext}` | App icon. `ext` must be one of `svg`, `png`, `ico` — anything else is a `404`, as is a missing file |
| `GET` | `/ca.crt` | This deployment's **public** local-CA certificate, so devices/browsers can trust the HTTPS server (`404` if TLS isn't set up). Only the public cert — the CA key never leaves the box. See [setup/tls.md](setup/tls.md). |

### Static mounts (public prefixes)

| Mount | Serves | Notes |
|---|---|---|
| `/assets/*` | the SPA's hashed bundles | cached immutably |
| `/static/*` | admin/static assets | |
| `/stt-models/*` | the browser Whisper bundle, laid out as transformers.js expects | **failsafe copy only** — the worker prefers the official source. Absent unless `download_models.sh` fetched it; the mount is then skipped rather than erroring |
| `/face-models/*` | YuNet + SFace ONNX | |
| `/wake-models/*` | the openWakeWord model | |
| `/ort/*` | the ONNX Runtime WASM backend, vendored by `frontend/scripts/copy-ort.mjs` | served from our own origin so the runtime never reaches for a CDN |

These four model/runtime mounts are public for a concrete reason: the weights are public,
SHA-256-pinned upstream artifacts containing no secret, and the **Web Worker that fetches them
cannot attach a Bearer token**. Authenticating them would break browser-side speech and face
recognition without protecting anything. The ONNX Runtime is self-hosted outright rather than
CDN-loaded because, unlike model weights, it is executable code.

---

## Examples

```bash
# Mint a key (on the box) and chat
KEY=$(uv run python src/scripts/manage.py mint-key admin demo)
curl -s -X POST localhost:5000/inbox -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" -d '{"text":"What is the capital of France?"}'

# Stream
curl -N -X POST localhost:5000/chat/stream -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" -d '{"text":"Tell me a joke","session_id":"<uuid>"}'

# Web login → token
curl -s -X POST localhost:5000/auth/login \
  -H "Content-Type: application/json" -d '{"username":"admin","password":"…"}'
```
