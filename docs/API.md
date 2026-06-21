# API Reference

Base URL: `http://<host>:5000`. All responses are JSON unless noted.

## Authentication

Every endpoint except `/health`, `/`, `/admin`, `/auth/login`, `/ca.crt`, `/favicon.svg`, and the
static mounts requires a **Bearer token** — either a web-login **session token** or a per-user
**API key**:

```
Authorization: Bearer <token>
```

Responses: `401` missing/malformed header · `403` invalid/expired token or non-admin on an admin
route · `429` rate limit exceeded (per user, `rate_limit_requests_per_minute`, default 30/min).
Non-admin users are capped at 500 characters per message (admins/API keys: 10000).

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
| `GET` | `/admin/services` | — | `{ "services": [{name, status: active\|inactive, detail}] }` — live subsystem health (orchestrator, LLM, embeddings, TTS, + one row per camera agent from `device_heartbeats`). |
| `GET` | `/admin/events?limit=N&type=&since_id=` | — | `{ "events": [{id, device_id, type, data, created_at}], "count": int }` (recent camera events, newest first). `type` filters (e.g. `face_seen` for the recognitions feed / verify); `since_id` returns only events newer than an id. |

---

## Voice / TTS

| Method | Path | Body | Returns |
|---|---|---|---|
| `POST` | `/tts` | `{ text }` (≤600) | `{ "audio": "<base64 WAV>" }` — synthesize speech (Piper); `503` if TTS unavailable. The web UI uses this to **speak the greeting**. |
| `GET` | `/greeting` | — | `{ "text", "audio" }` — a time-aware JARVIS acknowledgement + spoken audio. The voice bridge calls this when it hears just the wake word ("Jarvis" → "Yes, sir?"). |

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
| `GET` | `/faces/enrolled` | — | `{ "enrolled": { name: [embedding, …] } }` — list per person; the set the agent matches against (auth required). |
| `GET` | `/admin/faces` | — | **admin** · `{ "faces": [{id, name, user_id, username, embedding_count, last_seen, created_at}] }`. |
| `GET` | `/admin/faces/{id}/embeddings` | — | **admin** · `{ "embeddings": [{id, source, created_at}] }` for a person. |
| `PUT` | `/admin/faces/{id}` | `{ name?, user_id? }` | **admin** · rename (UNIQUE) and/or link a user (only fields sent change; `user_id:null` clears). |
| `DELETE` | `/admin/faces/{id}` | — | **admin** · delete a person + all their embeddings. |
| `DELETE` | `/admin/faces/embeddings/{id}` | — | **admin** · delete one embedding (person stays). |

**Enroll from the web UI** — an admin queues a request for a camera; that device's agent captures +
submits on-device (the device key can only *fulfill* a request made for it — it can't enroll arbitrarily):

| Method | Path | Body | Returns |
|---|---|---|---|
| `POST` | `/admin/faces/enroll-request` | `{ user_id, device_id }` (or `{ name, device_id }`) | **admin** · queue a pending enroll for a device. With `user_id` the face is enrolled for that account and the person is auto-linked to it; `name` defaults to the username. |
| `GET` | `/admin/faces/enroll-requests` | — | **admin** · recent requests `[{id, device_id, name, status, detail, …}]`. |
| `GET` | `/faces/enroll-request` | — | **device key** · the pending request for THIS device (`{request:{id,name}}` or null). |
| `POST` | `/faces/enroll-result` | `{ request_id, embedding?, error? }` | **device key** (own request) · submit the captured embedding (creates the person/embedding) or report failure. |
| `POST` | `/faces/enroll-preview` | `{ request_id, image(b64 jpeg), captured, total }` | **device key** (own request) · relay a live preview frame (RAM-only, ~30s TTL). |
| `GET` | `/faces/enroll-preview?request_id=N` | — | **admin** · latest preview frame `{preview:{image,captured,total}}` (single-shot fallback). |
| `GET` | `/faces/enroll-preview-stream?request_id=N` | — | **admin** · NDJSON stream pushing each new preview frame `{image,captured,total}` as it arrives (~10 fps smooth live view). One connection; ends on disconnect / stale frames / 90s cap. |

---

## Misc / unauthenticated

| Method | Path | Returns |
|---|---|---|
| `GET` | `/health` | `{ "status": "ok", "model": "qwen3.5-2b" }` |
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
