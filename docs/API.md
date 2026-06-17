# API Reference

Base URL: `http://<host>:5000`. All responses are JSON unless noted.

## Authentication

Every endpoint except `/health`, `/`, `/admin`, `/auth/login`, and the static mounts requires a
**Bearer token** — either a web-login **session token** or a per-user **API key**:

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

## Events (edge devices)

| Method | Path | Body | Returns |
|---|---|---|---|
| `POST` | `/events` | `{ device_id, type, ts?, data? }` | `{ "status": "ok", "id": int }` — ingest an edge/vision event. **A device-scoped API key records the event under its own bound device (the body `device_id` can't spoof another); admins may post as any device; plain users are denied.** `data` ≤ 4 KB; only the last 5000 events are retained. |

Used by the Raspberry Pi camera agent (`edge/`) to report high-level events (`motion`,
`face_seen`, `pose`, `gesture`); `data` is type-specific JSON. No imagery is sent.

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
| `DELETE` | `/admin/users/{id}` | — | `{ "status": "ok" }` (cannot delete self) |
| `POST` | `/admin/api_keys` | `{ user_id, description }` | `{ "key": "jk-…" }` (full key shown once; stored hashed) |
| `GET` | `/admin/api_keys` | — | `{ "keys": [{id, key_string(prefix only), user_id, description, usage_count, last_used_at, created_at}] }` |
| `DELETE` | `/admin/api_keys/{id}` | — | `{ "status": "ok" }` |
| `GET` | `/admin/stats` | — | `{ "users": int, "chats": int, "messages": int }` |
| `GET` | `/admin/events?limit=N` | — | `{ "events": [{id, device_id, type, data, created_at}], "count": int }` (recent edge events, newest first) |

---

## Misc / unauthenticated

| Method | Path | Returns |
|---|---|---|
| `GET` | `/health` | `{ "status": "ok", "model": "qwen3.5-2b" }` |
| `GET` | `/system` | **admin** · live host telemetry: `{ load1, cpus, cpu_pct, mem_used_mb, mem_total_mb, mem_pct, uptime_sec }` (dependency-free, from `/proc` + `os`) |
| `GET` | `/` | React SPA (`frontend/dist/index.html`) |
| `GET` | `/admin` | Serves the React SPA, which renders the admin console (admin-gated client-side + on every `/admin/*` endpoint) |
| `GET` | `/favicon.svg` | App icon (served from the dist root) |
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
