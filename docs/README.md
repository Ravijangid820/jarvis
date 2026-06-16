# Jarvis Documentation

Index of the project documentation. Start with the [main README](../README.md) for the elevator
pitch and quick start; the files here go deep on each area.

## Core docs

| Doc | What's in it |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, the orchestrator module graph, design decisions, security model |
| [WORKFLOWS.md](WORKFLOWS.md) | Runtime flows: chat request lifecycle, prompt token-budgeting, RAG, fact extraction, the voice loop, auth |
| [API.md](API.md) | Full HTTP endpoint reference (auth, chat, sessions, knowledge, admin) |
| [SPECS.md](SPECS.md) | Hardware, models, performance, the config reference, and the DB schema |
| [DEPLOY.md](DEPLOY.md) | Deploy runbook: units, re-embed migration, Tailscale/localhost firewall, the admin CLI |
| [AUDIT.md](AUDIT.md) | The 81-finding code/security self-audit and what was fixed |

## History / reference

| Doc | What's in it |
|---|---|
| [CHANGELOG.md](CHANGELOG.md) | Dated record of notable changes |
| [FUTURE_IDEAS.md](FUTURE_IDEAS.md) | Roadmap: tool calling, Home Assistant/MQTT, streaming TTS, … |
| [benchmarks/](benchmarks/) | Whisper + LLM benchmark output |
| [planning/](planning/) | Historical planning notes (implementation plan, walkthrough, tasks) |

## Where things live

```
src/orchestrator/   FastAPI app — see ARCHITECTURE.md for the module graph
src/scripts/        run_listener.sh (voice), manage.py (admin CLI), reembed_memory.py (migration)
frontend/           React 19 + Vite chat UI
config/             schema.sql + jarvis.example.json (real jarvis.json is gitignored)
systemd/            llama-fast + jarvis-orchestrator service units
tests/              pytest suite
```
