# Jarvis AI Project — Implementation Plan (Approved)

## Resolved Decisions

| Question | Decision |
|---|---|
| RAM upgrade timing | User will notify when done. Proceed with 6 GB optimizations now. |
| Dual-model vs on-demand | **On-demand loading** — even after 8 GB upgrade |
| Orchestrator binding | Keep `0.0.0.0` — accessible from laptop/phone. Add API key auth. |
| Whisper model | **Benchmark both** base.en and small.en, pick optimal |

---

## Execution Order

### WI-1: Swap & memory tuning (immediate)
- Set `vm.swappiness=10`
- This alone will reduce swap thrashing significantly

### WI-2: Rebuild whisper.cpp with AVX + benchmark models
- Rebuild with explicit `GGML_AVX=ON`
- Download `small.en` model
- Benchmark both base.en and small.en
- Document results for user to decide

### WI-3: Create systemd services
- `llama-fast.service` — 2B model (always running)
- ~~`llama-reasoning.service`~~ — 4B model managed by orchestrator on-demand
- `jarvis-orchestrator.service` — FastAPI app

### WI-4: Orchestrator security hardening
- API key authentication (token from config file, not hardcoded)
- Input length validation (max 500 chars)
- Request timeouts on LLM calls
- Simple in-memory rate limiting
- Security headers
- Keep llama-server ports on `127.0.0.1` only

### WI-5: On-demand 4B model loading
- Orchestrator manages 4B llama-server lifecycle via `subprocess`
- Starts on complex query, health-checks until ready
- Auto-shutdown after idle timeout (5 min)
- Only 2B stays loaded permanently (~1.7 GB)

### WI-6: SQLite memory integration
- Create DB using existing schema.sql
- Store all conversations (user + jarvis)
- Inject recent context into system prompt
- FTS5 search for relevant facts
- Parameterized queries only (no string concat)

### WI-7: Update project documentation
- Reflect all changes, actual status, benchmarks, architecture

---

## Security Plan

- API key auth via config file (not hardcoded) — `secrets.token_hex()` fallback with warning
- Input validation: max length, strip dangerous chars
- Parameterized SQL queries only
- Request timeouts on all HTTP calls
- Rate limiting (in-memory, stdlib only)
- LLM backends on 127.0.0.1 only
- Generic error messages to clients, detailed logs server-side
- `TODO(security)`: HTTPS/TLS not implemented — requires reverse proxy (nginx) for production. Document for user.
- `TODO(security)`: CSRF not applicable — API-only service, no browser cookies/sessions. Bearer token auth used instead.
- `TODO(security)`: OAuth/MFA not implemented — single-user self-hosted system. Document for future.
