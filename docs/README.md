# Jarvis Documentation

Index of the project documentation. Start with the [main README](../README.md) for the elevator
pitch and quick start; the files here go deep on each area.

## Install the server — pick ONE

| Path | Install doc | One-liner |
|---|---|---|
| **Combined image** — one container, everything baked (simplest; **Proxmox OCI**) | [setup/combined-image.md](setup/combined-image.md) | `docker run … jarvis-combined:latest` |
| **Orchestrator image + official llama** — two containers (production split; GPU-swappable LLM) | [setup/orchestrator-image.md](setup/orchestrator-image.md) | `docker compose up -d` |
| **Repository** — from source (dev, or the systemd box; no Docker needed) | [setup/repository.md](setup/repository.md) | `bash src/scripts/setup.sh` |

All three default everything (login `admin`/`admin`, pinned models) — optionally `cp .env.example .env`
to persist overrides; the same file works for Docker **and** the repo scripts. Deep-dive references:
[setup/docker.md](setup/docker.md) (config layers, HTTPS, publishing, CPU support) ·
[setup/image-releases.md](setup/image-releases.md) (published tags).

## Device / component guides

**Start here:** [setup/quickstart.md](setup/quickstart.md) — the whole path end to end (server → device
→ browser) in one page. The per-component guides below go deeper on each piece (they run on different
machines with independent environments).

| Component | Runs on | Setup doc | Entry point |
|---|---|---|---|
| **Server (native, full detail)** — orchestrator + web UI (+ HTTPS + services) | the Proxmox LXC | [setup/server.md](setup/server.md) (+ [DEPLOY.md](DEPLOY.md) for re-deploys) | `sudo bash src/scripts/setup-server.sh` |
| **TLS / HTTPS** — encrypt the LAN (local CA; included in setup-server) | the server + each device | [setup/tls.md](setup/tls.md) | `bash src/scripts/setup_tls.sh` |
| **Camera vision agent** — motion/face/pose/gestures (laptop webcam or Pi) | the device | [setup/camera.md](setup/camera.md) | `camera/setup.ps1` (Win) · `bash camera/setup.sh` (Pi) |
| **Voice listener** — wake word → `/inbox` (whisper) | the server box | [setup/voice.md](setup/voice.md) | `bash src/scripts/run_listener.sh` |
| **Windows volume agent** — controls the laptop/BT volume | the laptop | [setup/volume-agent.md](setup/volume-agent.md) | `python volume_agent.py` |
| **Home Assistant** — smart-home control via allowlisted LLM tools | wherever HA runs | [setup/home-assistant.md](setup/home-assistant.md) | set `HA_URL` + `HA_TOKEN` + allowlist |

## Core docs

| Doc | What's in it |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, the orchestrator module graph, design decisions, security model |
| [WORKFLOWS.md](WORKFLOWS.md) | Runtime flows: chat request lifecycle, prompt token-budgeting, RAG, fact extraction, the voice loop, auth |
| [API.md](API.md) | Full HTTP endpoint reference (auth, chat, sessions, knowledge, admin, camera/vision events, device control) |
| [SPECS.md](SPECS.md) | Hardware, models, performance, the config reference, and the DB schema |
| [DEPLOY.md](DEPLOY.md) | Deploy runbook: units, re-embed migration, Tailscale/localhost firewall, the admin CLI |
| [AUDIT.md](AUDIT.md) | Security audits: the 81-finding self-audit (2026-06-15), the F1–F24 follow-up (2026-06-17), and the 2026-06-19 review of the camera/admin/role changes (no crit/high) |

## History / reference

| Doc | What's in it |
|---|---|
| [RELEASES.md](RELEASES.md) | **Release history** — the project iteration by iteration (v1.0.0 → today), themes + the release process |
| [KNOWN_ISSUES.md](KNOWN_ISSUES.md) | **Living issue tracker** — open limitations, accepted trade-offs, and what release fixed what |
| [CHANGELOG.md](CHANGELOG.md) | Dated record of notable changes |
| [FUTURE_IDEAS.md](FUTURE_IDEAS.md) | Roadmap: edge voice, spec decoding, HA extensions, … |
| [benchmarks/](benchmarks/) | Whisper + LLM benchmark output |
| [planning/](planning/) | Historical planning notes (implementation plan, walkthrough, tasks) |

## Where things live

```
src/orchestrator/   FastAPI app — see ARCHITECTURE.md for the module graph
src/scripts/        setup.sh + download_models.sh + build_native.sh (bootstrap), run_listener.sh
                    (voice), manage.py (admin CLI), reembed_memory.py (migration), fetch_fonts.py
frontend/           React 19 + Vite chat UI (+ admin console)
camera/             on-device camera/vision agent (laptop webcam or Pi) — own env (setup: docs/setup/camera.md)
clients/            device agents on other machines — e.g. clients/volume-agent/ (Windows volume)
config/             schema.sql + jarvis.example.json (real jarvis.json is gitignored)
systemd/            llama-fast + jarvis-orchestrator service units
Dockerfile.combined     single-container image (built ON the official llama.cpp image)
Dockerfile.orchestrator slim orchestrator image (no LLM) for the two-service split
docker/                 container entrypoints: entrypoint.sh (orchestrator), all-in-one.sh (combined)
docker-compose.yml      the split: official llama.cpp image + jarvis-orchestrator
tests/              pytest suite
```

The per-component setup guides live together under [`docs/setup/`](setup/); each code dir keeps a
short README pointing to its guide.
