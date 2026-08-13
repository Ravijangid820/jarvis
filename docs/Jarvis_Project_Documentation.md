# Jarvis AI — Project Documentation

> **This monolithic document has been split into focused files.** See the
> **[documentation index](README.md)**. This page is now a short overview + map.

## What Jarvis is

A fully self-hosted, **offline** voice + text AI assistant — LLM, speech-to-text, semantic memory,
and text-to-speech — running entirely on a single 2011-era laptop (Intel i5-2520M, 8 GB, CPU-only,
no AVX2) inside a Proxmox LXC. No cloud APIs. The engineering theme throughout is making an LLM
assistant feel responsive under that hardware constraint.

## Goals

Self-hosted · secure · no cloud inference · CPU-only · voice + text · long-term memory ·
extensible. Planned next: tool/function calling and Home Assistant / MQTT control
(see [FUTURE_IDEAS.md](FUTURE_IDEAS.md)).

## Read next

| For… | See |
|---|---|
| System design, components, module graph | [ARCHITECTURE.md](ARCHITECTURE.md) |
| How requests, memory, and the voice loop work | [WORKFLOWS.md](WORKFLOWS.md) |
| HTTP endpoint reference | [API.md](API.md) |
| Hardware, models, config + DB schema | [SPECS.md](SPECS.md) |
| Deploying / operating the box | [DEPLOY.md](DEPLOY.md) |
| The code/security self-audit | [AUDIT.md](AUDIT.md) |
| What changed and when | [CHANGELOG.md](CHANGELOG.md) |

## Current status

| Layer | State |
|---|---|
| LLM (Qwen 2B, llama.cpp, `-c 4096` + token budgeting) | ✅ |
| Speech-to-text — whisper.cpp `base.en` on the box, **Whisper + wake word in the browser** (v3.2.0) | ✅ |
| Orchestrator (FastAPI, modular, secured, tested) | ✅ |
| Memory (SQLite + ChromaDB cosine RAG, **idle-time batch embedding**) | ✅ |
| Text-to-speech (Piper, wired into chat + stream) | ✅ |
| Tool / function calling | ✅ shipped — `TOOLS_SPEC` on `/inbox` and in the home-command round-trip; not yet offered during ordinary streamed chat ([KNOWN_ISSUES](KNOWN_ISSUES.md) #2) |
| Home automation (Home Assistant) | ✅ shipped v2.5.0, hardened v2.5.1 — on/off/toggle/run against a hard entity allowlist, plus a semantic intent router for paraphrases (v2.6.0). No MQTT; no brightness/colour yet |
| Multi-tenancy (households) + public demo mode | ✅ |
| Face recognition (browser enrollment, on-device camera agent) | ✅ |
