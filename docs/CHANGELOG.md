# Jarvis AI — Changelog

All notable changes to this project are documented in this file.

---

## 2026-06-16 — Reliability polish

- `POST /knowledge` no longer runs the 300M embedding model inline on the request
  thread — manual fact-dedup uses the cheap word-overlap path; the background fact
  worker keeps the embedding-based semantic dedup. (Avoids CPU contention with the LLM.)
- `_safe_exec` now only swallows benign "already applied" migration errors and re-raises
  genuine failures (syntax/locked/etc.) instead of masking them.
- Log the embedding model dimension + vector count at startup so a future model/dimension
  change (which would silently break indexing) is diagnosable.

---

## 2026-06-16 — API-key hashing & React admin console

### Security
- **API keys are now hashed at rest** (SHA-256 + a short display prefix), like session
  tokens. Existing keys keep working — a one-time migration hashes them in place, and
  holders still authenticate by presenting the plaintext (we hash and match), so the
  voice listener doesn't break. `manage.py mint-key` and the admin create-key path store
  only the hash; the plaintext is shown once. `admin_create_key` now validates the user.

### Admin console
- **Ported the admin panel from standalone `admin.html` into the React SPA.** It now
  inherits the HUD styling, fonts, and theme switcher (Cyberpunk/Emerald/Ember all apply),
  reuses the SPA auth, and is XSS-safe via React. `/admin` serves the SPA and renders the
  admin view client-side. Removed `admin.html` and the now-unused `static/style.css`.
- API-key list returns a display prefix + row `id` (no recoverable full key); deletion is
  by `id`.

---

## 2026-06-16 — Security & reliability hardening

### Security
- **Session tokens are now hashed at rest** (SHA-256). The plaintext is returned to
  the client once at login; the DB stores only the hash, so a DB/backup leak no longer
  yields usable live tokens. *One-time effect: existing sessions are invalidated, so
  everyone re-logs-in once after this deploy.* (API keys: still plaintext — follow-up.)
- **`/auth/login` is rate-limited** by client IP (8/min). Login is unauthenticated and
  bypasses the per-user limiter, so this closes an unbounded password-guessing oracle.
- Fixed a cross-user **IDOR**: `DELETE /sessions/{id}` now authorizes ownership first.

### Reliability
- SQLite `busy_timeout` raised 5s → 30s (three writer sources can overlap; 5s was
  occasionally too short under load).
- `init_db()` now fails loudly if `schema.sql` is missing instead of silently leaving
  every query to fail with "no such table".

### Tests
- Added `tests/test_auth.py` (password + token hashing); CI-safe (no config/model needed).

---

## 2026-06-16 — UI overhaul & follow-up fixes

### UI / frontend
- Cinematic Stark/JARVIS HUD: boot sequence (click-to-skip), arc-reactor motifs, holographic
  panels, login/welcome/chat states.
- **Admin panel now renders in the HUD theme.** It referenced CSS variables (`--accent-cyan`,
  `--font-mono`, …) that `style.css` never defined, so it fell back to unstyled browser
  defaults; aliased those names onto the holographic palette.
- **Self-hosted fonts** (Rajdhani + JetBrains Mono, SIL OFL, Latin subset) under
  `static/fonts/`, served same-origin. The UI no longer fetches from Google Fonts — it renders
  correctly fully offline with zero third-party requests. Generator: `src/scripts/fetch_fonts.py`.
- Faster first paint (non-blocking font `<link>` instead of CSS `@import`), instant-while-
  streaming auto-scroll, page `<title>` set to J.A.R.V.I.S.
- **Working sidebar toggle** — collapse/expand on desktop, drawer on mobile (was wired only to
  open, dead on desktop).
- **Non-fighting chat scroll** — sticks to the bottom only when you're already near it, so
  scrolling up mid-reply no longer yanks you back down; new messages/session loads snap.
- **Functional Stop button** — cancels an in-flight generation via `AbortController`, keeps the
  partial reply, and frees the server's LLM slot (the upstream stream is closed).

### Security / correctness
- **Fixed a cross-user IDOR**: `DELETE /sessions/{id}` deleted conversation history and vectors
  by `session_id` with no ownership check; now authorizes via `require_owned_session` first.
- Serve `/favicon.svg` (previously 404 on every page load); cache content-hashed `/assets`
  bundles immutably instead of `no-store`.

---

## 2026-06-16 — Hardening, refactor & documentation

### Security
- **Removed the master API key entirely.** Auth is now web-login sessions or per-user, revocable
  `api_keys`. The voice listener uses a real key (`config/voice_listener.key`); added
  `src/scripts/manage.py` (create-admin / reset-password / mint-key) for bootstrap & recovery.
- Real server-side logout (`/auth/logout`), expired-session purge, per-user rate limiting
  (admins included), admin-panel XSS escaping, configurable CORS, `system_prompt` length cap.

### Correctness / memory
- **Fixed the critical context-window bug**: `-c 4096` + char-based prompt token-budgeting that
  clamps history and completion so context is never silently evicted.
- Merged system prompt + profile + RAG into a single system message (Qwen template requirement).
- Wired Piper TTS into the streaming endpoint; surfaced stream errors instead of storing them as
  replies; never lose the user's turn on failure.
- Reworked the shared `default` session into per-user sessions with strict ownership checks.
- RAG: cosine space + correct embeddinggemma query/document prefixes, semantic fact dedup,
  user-scoped recall, background (off-request-path) embedding; one-time `reembed_memory.py` migration.

### Engineering
- **Split the 1,300-line `main.py`** into an acyclic module graph
  (`config → {db, auth, llm} → memory → chat → main`).
- Added pytest + ruff + GitHub Actions CI; reconciled `schema.sql` (dropped unused FTS5 tables);
  lifespan handler; safe LLM-response parsing; SQLite `busy_timeout`; log rotation; systemd hardening.
- First clean git history (secrets + multi-GB binaries gitignored); full self-audit in `AUDIT.md`.
- Rewrote the documentation set: `ARCHITECTURE`, `WORKFLOWS`, `API`, `SPECS`, `DEPLOY`, docs index.

---

## 2026-06-02 — Major Improvement Session

### Added
- API key authentication for orchestrator (Bearer token, constant-time comparison)
- Rate limiting (30 req/min per IP, in-memory tracking)
- Input validation (max 500 characters, Pydantic model)
- Request timeouts (120s on LLM calls)
- Security headers (X-Content-Type-Options, X-Frame-Options, Cache-Control)
- SQLite memory integration (conversation history + FTS5 full-text search)
- Context injection (last 10 messages sent to LLM with each query)
- `/health` endpoint (no auth required)
- `/history` endpoint (retrieve conversation history)
- `llama-fast.service` systemd unit (auto-start 2B model)
- `jarvis-orchestrator.service` systemd unit (auto-start orchestrator)
- `run_listener.sh` with API key auth for voice bridge
- `--reasoning off` flag on llama-server to disable Qwen3.5 thinking mode
- `/no_think` directive in system prompt
- `vm.swappiness=10` in sysctl.conf
- Whisper small.en model downloaded for benchmarking
- `/srv/jarvis/` structured project directory

### Changed
- RAM upgraded from 6 GB to 8 GB
- Whisper.cpp rebuilt with explicit `GGML_AVX=ON` (was OFF due to LXC auto-detection failure)
- Whisper.cpp rebuilt with `WHISPER_SDL2=ON` for microphone capture
- Orchestrator rewritten from scratch with security hardening
- LLM response time reduced from 60+ seconds to 5-15 seconds (thinking mode disabled)

### Removed
- `ai-fast-brain.service` (replaced by `llama-fast.service`)
- `ai-reasoning-brain.service` (4B model disabled to conserve RAM)
- 4B model server (model file retained on disk for future use)

### Benchmark Results
- **Qwen3.5-2B**: 5.75 t/s prompt, 3.67 t/s generation (with --reasoning off)
- **Whisper base.en**: 83.5s for 11s audio (7.6x realtime) — SELECTED
- **Whisper small.en**: 364.3s for 11s audio (33.1x realtime) — too slow

---

## 2026-06-01 — Initial Setup

### Added
- llama.cpp built from source (AVX-only, Sandy Bridge compatible)
- Qwen3.5-2B-Q4_K_M model deployed (Fast Brain, port 8081)
- Qwen3.5-4B-Q4_K_M model deployed (Reasoning Brain, port 8080)
- `/srv/ai/` directory structure created
- Basic FastAPI orchestrator
- whisper.cpp built from source
- Whisper base.en model downloaded
- `ai-fast-brain.service` and `ai-reasoning-brain.service` created
- SQLite schema with FTS5 designed (`schema.sql`)
