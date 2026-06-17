# Jarvis Documentation

Index of the project documentation. Start with the [main README](../README.md) for the elevator
pitch and quick start; the files here go deep on each area.

## Setup guides (by component)

Each runnable piece has its own setup doc — pick the one you're deploying. They run on
different machines and have independent environments.

| Component | Runs on | Setup doc | Entry point |
|---|---|---|---|
| **Server** — orchestrator + web UI | the Proxmox LXC | [main README → Quick Start](../README.md#quick-start), then [DEPLOY.md](DEPLOY.md) | `bash src/scripts/setup.sh` |
| **Raspberry Pi vision agent** — camera, motion/face/pose/gestures | the Pi | [edge/README.md](../edge/README.md) | `bash edge/setup.sh` → `python -m jarvis_edge.bench` |
| **Windows volume agent** — controls the laptop/BT volume | the laptop | [clients/volume-agent/README.md](../clients/volume-agent/README.md) | `python volume_agent.py` |

## Core docs

| Doc | What's in it |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, the orchestrator module graph, design decisions, security model |
| [WORKFLOWS.md](WORKFLOWS.md) | Runtime flows: chat request lifecycle, prompt token-budgeting, RAG, fact extraction, the voice loop, auth |
| [API.md](API.md) | Full HTTP endpoint reference (auth, chat, sessions, knowledge, admin, edge events, device control) |
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
src/scripts/        setup.sh + download_models.sh + build_native.sh (bootstrap), run_listener.sh
                    (voice), manage.py (admin CLI), reembed_memory.py (migration), fetch_fonts.py
frontend/           React 19 + Vite chat UI (+ admin console)
edge/               Raspberry Pi camera/vision agent — own env; see edge/README.md
clients/            device agents on other machines — e.g. clients/volume-agent/ (Windows volume)
config/             schema.sql + jarvis.example.json (real jarvis.json is gitignored)
systemd/            llama-fast + jarvis-orchestrator service units
tests/              pytest suite
```

Setup/runtime docs co-locate with their code (`edge/README.md`, `clients/volume-agent/README.md`)
so they stay in sync; this index links them under "Setup guides" above.
