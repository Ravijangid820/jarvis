# Jarvis Improvement Tasks

## WI-1: Swap & Memory Tuning
- [x] Set vm.swappiness=10 in /etc/sysctl.conf (needs host-side `sysctl vm.swappiness=10`)
- [x] RAM upgraded to 8 GB (confirmed: 8192 MB total)
- [x] Stopped and disabled 4B model server (freed ~3 GB)
- [x] Removed old ai-fast-brain.service and ai-reasoning-brain.service

## WI-2: Whisper.cpp AVX Rebuild + Model Benchmark
- [x] Rebuilt whisper.cpp with GGML_AVX=ON + WHISPER_SDL2=ON
- [x] All binaries built including whisper-command and whisper-stream
- [x] Downloaded ggml-small.en.bin model (88 MB)
- [/] Benchmarking base.en (in progress)
- [ ] Benchmark small.en
- [ ] Document results

## WI-3: Systemd Services
- [x] Created llama-fast.service (2B model, 127.0.0.1:8081, --reasoning off)
- [x] Created jarvis-orchestrator.service (0.0.0.0:5000, depends on llama-fast)
- [x] Enabled and tested both services — both active and running

## WI-4: Orchestrator Security Hardening
- [x] Created /srv/ai/config/jarvis.json with API key (generated via secrets.token_hex)
- [x] API key auth middleware (Bearer token, constant-time comparison)
- [x] Input length validation (max 500 chars via Pydantic)
- [x] Request timeouts on urllib calls (120s)
- [x] Rate limiting (30 req/min in-memory)
- [x] Security headers (X-Content-Type-Options, X-Frame-Options, Cache-Control)
- [x] LLM server bound to 127.0.0.1 only
- [x] Verified: 401 on missing auth, 403 on bad auth, 200 on valid auth

## WI-5: Orchestrator — 2B-Only Mode (Revised)
- [x] Removed 4B routing (2B handles everything)
- [x] Added --reasoning off to llama-server (thinking=0 confirmed)
- [x] Added /no_think to system prompt in config
- [x] Tested: "What is the capital of France?" → "Paris" (fast, accurate)
- [x] Updated run_listener.sh with API key auth

## WI-6: SQLite Memory Integration
- [x] Database initialized from schema.sql on startup (jarvis.db created)
- [x] Store user queries and Jarvis responses (verified in DB)
- [x] Retrieve recent conversation context (last 10 messages)
- [x] FTS5 fact search implemented
- [x] All queries parameterized (no string concatenation)
- [x] Added /history endpoint

## WI-7: Update Documentation
- [ ] Update Jarvis_Project_Documentation.md with all changes
