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

### `POST /inbox`  (non-streaming)
→ `{ "response": str, "speed": str, "new_title": str|null, "audio": base64|null }`

A recognized **volume command** ("set volume to 50%", "volume up", "mute", …) is handled directly —
authorized via `_can_control_devices`, enqueued to the device agent, and acknowledged with a short
spoken reply — instead of going to the LLM. Anything not recognized falls through to the LLM as usual.
This fast-path applies to **both** `/inbox` and `/chat/stream` (so it works by voice or by typing in
the web chat).

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
| `GET` | `/reminders` | — | your pending reminders `[{id, text, due_at, status, created_at}]`. (Create them by chatting: "remind me … in 20 min".) |
| `GET` | `/reminders/due` | — | your pending reminders whose time has arrived `{ "due": [{id, text, due_at}] }`. |
| `POST` | `/reminders/{id}/ack` | — | mark a fired reminder done. `{ "status": "ok" }` |
| `DELETE` | `/reminders/{id}` | — | cancel a pending reminder. `{ "status": "ok" }` |
| `GET` | `/admin/audit?limit=N` | — | **admin** · recent audit entries `[{id, created_at, user_id, username, action, detail}]` (device + admin actions). |
| `POST` | `/admin/backup` | — | **admin** · create a backup now. `{ "status", "name", "size" }` |
| `GET` | `/admin/backups` | — | **admin** · list backups `[{name, size, created_at}]`. |
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
| `POST` | `/devices/volume` | `{ action: set\|step\|mute\|unmute, value?, device? }` | `{ "status": "ok", "id": int }` — enqueue a volume command. **Authorized** (admin, or user with `can_control_devices`); `set` needs `value` 0–100, `step` a signed delta. |
| `GET` | `/devices/commands?device=&wait=` | — | `{ "commands": [{id, action, params}] }` — device agents **pull** their pending commands (long-poll up to `wait`s; delivered commands aren't re-served). **The API key must be bound to that `device` (or be an admin)** — a key for one device can't drain another's queue. |

The Windows volume agent (`clients/volume-agent/`) pulls + applies these. The orchestrator only
ever enqueues — the agent opens no inbound port. Authorization is enforced server-side, never by
the LLM.

**LLM tools** (`set_volume`, `create_reminder`, `get_presence`, and — when Home Assistant is
configured — `home_control`/`home_status`) execute through the same server-side gates: the model
only *proposes*; `_can_control_devices`, the optional presence gate, the HA entity **allowlist**,
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
| `GET` | `/admin/home-assistant` | — | `{ configured, url, token_set, allowed_entities, env_managed, connected }` — Smart-Home config for the admin UI. **The token itself is never returned** (only `token_set`). |
| `PUT` | `/admin/home-assistant` | `{ url, token?, allowed_entities? }` | Save to the DB + apply **live** (no restart). Blank/omitted `token` keeps the stored one. `409` when env-managed. Audited (`ha.config`). |
| `POST` | `/admin/home-assistant/test` | `{ url?, token? }` | `{ ok, detail }` — probe a URL/token **before** saving (blank token = use stored). |
| `GET` | `/admin/home-assistant/entities` | — | `{ entities: [{entity_id, name, state, domain, allowed}] }` — controllable devices (lights/switches/…) for the allowlist picker. |
| `GET` | `/admin/events?limit=N&type=&since_id=` | — | `{ "events": [{id, device_id, type, data, created_at}], "count": int }` (recent camera events, newest first). `type` filters (e.g. `face_seen` for the recognitions feed / verify); `since_id` returns only events newer than an id. |

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
| `GET` | `/favicon.svg` | App icon (served from the dist root) |
| `GET` | `/ca.crt` | This deployment's **public** local-CA certificate, so devices/browsers can trust the HTTPS server (`404` if TLS isn't set up). Only the public cert — the CA key never leaves the box. See [setup/tls.md](setup/tls.md). |
| — | `/assets/*`, `/static/*` | Static frontend + admin assets (`/assets/*` cached immutably) |

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
