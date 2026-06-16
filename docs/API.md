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

### `POST /auth/logout`
Revokes the caller's current session token server-side. → `{ "status": "ok" }`

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

## Admin  (all require an admin token)

| Method | Path | Body | Returns |
|---|---|---|---|
| `POST` | `/admin/users` | `{ username, password, role? }` | `{ "status": "ok" }` (`400` if username exists) |
| `GET` | `/admin/users` | — | `{ "users": [{id, username, role, created_at, total_chats, total_messages}] }` |
| `DELETE` | `/admin/users/{id}` | — | `{ "status": "ok" }` (cannot delete self) |
| `POST` | `/admin/api_keys` | `{ user_id, description }` | `{ "key": "jk-…" }` (shown once) |
| `GET` | `/admin/api_keys` | — | `{ "keys": [{key_string(masked), full_key, user_id, description, usage_count, last_used_at, created_at}] }` |
| `DELETE` | `/admin/api_keys/{key_string}` | — | `{ "status": "ok" }` |
| `GET` | `/admin/stats` | — | `{ "users": int, "chats": int, "messages": int }` |

---

## Misc / unauthenticated

| Method | Path | Returns |
|---|---|---|
| `GET` | `/health` | `{ "status": "ok", "model": "qwen3.5-2b" }` |
| `GET` | `/` | React SPA (`frontend/dist/index.html`) |
| `GET` | `/admin` | Admin panel HTML (its data endpoints are still auth-gated) |
| — | `/assets/*`, `/static/*` | Static frontend + admin assets |

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
