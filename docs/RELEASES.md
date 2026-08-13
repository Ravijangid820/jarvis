# Release history — the project, iteration by iteration

The narrative view: what each release *was about* and why. For the granular change-by-change record
see [CHANGELOG.md](CHANGELOG.md); for published image tags see
[setup/image-releases.md](setup/image-releases.md).

| Version | Date | Theme |
|---|---|---|
| **v3.4.0** | 2026-08-12 | **Grounding and the wait before the first word** — greetings answered without the model, live device state on the turn, model-free titles, idle-batch embedding, a twelve-finding security pass |
| **v3.3.0** | 2026-08-10 | **Live voice, end to end** — wake word, reactor HUD, mic selection, warm models, browser face enrolment — and the container fixes that finally ship them |
| v3.2.1 | 2026-08-03 | Patch: two silent-failure fixes (CSP-blocked inline script, ORT path 404 on Pages) + build-time guards |
| **v3.2.0** | 2026-08-03 | **Speech-to-text on the device** — in-browser Whisper (WASM); the server spends no CPU on STT |
| **v3.1.0** | 2026-08-03 | **MCP tool discovery** — real protocol handshake, review-only; frontend CI gate |
| v3.0.1 | 2026-07-29 | MCP registry, multi-model discovery + staged switching, live composer token estimate |
| **v3.0.0** | 2026-07-27 | **Three-tier deploy + Llama Clean UI** — frontend container, run modes (prod/dev/demo), attachments |
| v2.6.1 | 2026-07-10 | Patch: camera on OpenCV 5.0; full doc refresh + DIAGRAMS.md |
| **v2.6.0** | 2026-07-09 | **Semantic understanding + mobile** — the intent router (meaning, not phrasings) + a phone-calibrated UI |
| v2.5.1 | 2026-07-09 | HA hardening round — eight live-testing fixes (fast-paths, pronouns, stop/enable semantics, anti-bluff, honest replies) |
| **v2.5.0** | 2026-07-08 | **Home Assistant** — smart-home control via allowlisted LLM tools + Smart Home admin UI |
| **v2.4.0** | 2026-07-07 | **Torch-free embeddings** — ONNX runtime everywhere; no HF token; −2 GB images |
| v2.3.1 | 2026-07-03 | Patch: `.env` everywhere, per-artifact install guides, embedding-override fix |
| **v2.3.0** | 2026-07-02 | **Official llama.cpp image** — stop compiling llama.cpp; ride upstream |
| **v2.2.0** | 2026-07-01 | **Containerization** — Docker images on GHCR, Proxmox OCI deploy |
| v2.1.0 | 2026-06-26 | Identity era: presence, reminders, tool-calling (voice), backups, audit log, locked installer |
| v2.0.0 | 2026-06-23 | The perf generation: KV-cache prefix reuse, TTS cache + streaming, household knowledge |
| v1.0.0 | 2026-06-23 | First complete assistant: chat + RAG memory + voice + camera vision + multi-round security hardening |

## The arc

**v1.0.0 — a working assistant (June 1–23).** Built up from an empty repo: FastAPI orchestrator +
llama.cpp on a 2011 no-AVX2 laptop, ChromaDB semantic memory with idle-time fact extraction, wake-word
voice in / Piper voice out, the on-device camera agent (YuNet+SFace, events-only), real multi-user auth,
and three rounds of security audit + hardening (81-finding self-audit → F1–F24 fixes → adversarial
recheck). The defining constraint was always the hardware.

**v2.0.0 — make it feel fast.** Same hardware, ~20× better multi-turn latency via llama.cpp KV-cache
prefix reuse (~35 s → 1.5 s follow-ups), disk-cached + streaming TTS, and data-safety work (full purge
on delete, safe id reuse).

**v2.1.0 — give it identity and reliability.** Presence awareness from the cameras (greet-on-arrival,
presence-gated device control), reminders/timers, the first LLM tool-calling (voice path), backups,
the audit log, and a locked (`uv.lock`) preflighted installer.

**v2.2.0 → v2.3.x — make it deployable anywhere.** The containerization era: first a fat image, then
the architectural insight that we should **ride the official `llama.cpp:server` image** instead of
compiling our own (v2.3.0 dropped the from-source Docker build entirely). Zero-config everywhere
(admin/admin seeded, pinned SHA-verified model downloads, `.env` honored by Docker *and* the repo
scripts), one release number across repo + images, deployed on Proxmox VE 9.1 as an OCI container.

**v2.4.0 — earn back the hardware.** Torch existed only to run the 300M embedder, so the full
sentence-transformers pipeline was exported to a single ONNX graph (verified cosine 1.000000 vs torch
— zero re-indexing), hosted public + SHA-pinned. Result: −2 GB images, service RAM 1.7 GB → ~600 MB,
~35 % faster query embeds, **no HuggingFace token needed anywhere**, secret-free CI.

**v2.5.0 — reach into the home.** Home Assistant control through narrow, allowlisted LLM tools —
token held server-side (from a dedicated non-admin HA user), entity allowlist enforced in code,
ambiguity refused, every action audited — plus the Smart Home admin tab (URL/token + Test connection +
a device picker pulled live from HA, saved to the DB, applied without a restart).

**v2.5.1 — harden it like a user.** One live testing session surfaced eight real defects — frozen
tool menus, a toolless streaming path that let the 2B model *invent* acks, missing pronouns and verbs,
wrong stop-vs-disable semantics, terse ambiguous replies. Each became a regression test; the last fix
(the anti-bluff guard) closed the failure *class*, not just the instance.

**v2.6.0 — understand, and fit in a pocket.** The semantic intent router: utterances are embedded with
the same local ONNX embedder RAG uses and compared against per-device exemplar phrases, so "i'm
melting in here" turns on the fan — confident matches act, plausible ones ask first, routines always
confirm, and the thresholds were calibrated against the real embedder on the production box. Plus a
full mobile calibration of the web UI (dvh viewport, 16px inputs, touch targets, containment). Test
suite: 74 → 118 across the v2.5–v2.6 arc.

**v3.0.0 — make it presentable, and shippable in tiers (July 10–27).** Two threads. The deploy story
grew a third tier — a dedicated Nginx frontend image alongside the orchestrator and llama, with
path-filtered CI so an untouched tier is not rebuilt — and the app learned **run modes**: `demo` mode
disables Home Assistant and hardware control outright and keeps sessions ephemeral, so the UI can be
shown publicly without exposing a house or retaining strangers' conversations. Meanwhile the interface
was rebuilt: the pixelated HUD styling gave way to **Llama Clean**, plus a Deep Reasoning toggle with a
thinking accordion, inline editing, branch/regenerate, auto-titling, and browser-parsed text
attachments (deliberately text-only — nothing pretends a text-only model can read a PDF).

**v3.0.1 → v3.1.0 — reach toward other tools.** An MCP server registry landed first (v3.0.1) together
with multi-model discovery and a *staged* model switch that honestly reports `restart_required`
instead of pretending the running llama process changed. v3.1.0 then replaced the registry's
connection test — an HTTP ping that counted 401/404/405 as success, so any web server passed — with a
real protocol handshake, and added admin-only tool discovery for review. Discovery stays read-only:
nothing executes MCP tools until there is a per-tool allowlist and an answer to whose authority a
remote tool runs under. v3.1.0 also put the frontend under CI for the first time, and reconciled a
version drift where `pyproject` had stayed at 2.6.0 across two tagged releases.

**v3.2.0 — give the hardware back its CPU (August 3).** Speech-to-text moved off the server and into
the browser: Whisper ONNX in a WASM Web Worker on the user's own device, so the 2011 box spends
nothing on transcription and the microphone no longer has to be attached to it. Model sourcing is
official-first (huggingface.co) with a SHA-256-pinned local copy as failsafe; the ONNX Runtime itself
is self-hosted outright, because unlike model weights it is executable code. Measured on the box, the
server-side alternative costs ~30 s of all-core work per utterance — Whisper pads every input to a
fixed 30 s window, so short commands get no discount — which is why this one goes to the edge.
Getting there took four load-time fixes, every one of which failed *silently*: a CSP-blocked model
fetch, a runtime loader reaching for a CDN, `immutable` caching freezing stale headers and then a
stale binary into browsers, and a missing wasm variant that hung instead of erroring. v3.2.1 added two
more of the same family and the build-time guards that now catch them.

**v3.3.0 — the live voice page, and images that actually contain it (August 10).** `/voice` became a
real thing to talk to rather than a demo: an openWakeWord spotter running in the tab, so "hey Jarvis"
needs no server and no audio on the wire; a reactor HUD that shows what it is doing; a microphone
picker, including the *server's* microphone streamed to the browser; models kept warm so "Preparing
model…" is a first-run event rather than a per-utterance one; and face enrolment moved into the
browser of the device holding the camera — detect, align and embed in a Worker, with only the vector
sent, so the camera agent lost its last code path that could put a frame on the wire.

The other half of the release was the images not containing any of it. Four images publish, and any
of them that serves the SPA has to bake the face, wake-word and STT browser bundles; several did
not, so the feature existed in the repo and not in the artifact. Publishing is now `v*`-tag-only —
merges to main stopped triggering builds, which had been quietly moving `latest`.

**v3.4.0 — grounding, and the wait before the first word (August 12).** Two threads, both starting
from a real transcript.

*Grounding.* The first human to type into the chat page said "I" and was told the state of every
light in the house. Three generations against the real model showed the device block was not the
cause — removing it made the model invent hardware that does not exist — so greetings stop reaching
the model at all, answered from four dry templates and withheld from the history for the same
reason device acknowledgements are. Separately, the prompt had never mentioned the devices: 989
characters of persona and nothing else, which is why it had been claiming both that it could not
control anything and that the lights were on. It now sees the allowlisted devices and their live
state, on the user turn rather than the system prefix so the KV cache survives.

*The wait.* Chatting through Jarvis felt far slower than llama.cpp's own UI, and generation was
identical either way — the whole gap was time-to-first-token, at ~90 ms per prompt token on a CPU
without AVX2. A chat title was costing a second LLM request per conversation (5.7 s of the single
slot, for four cosmetic words, before the stream's done event); it is now derived from the first
message with no model involved. Embedding was running ~1.2 s per message moments after each reply,
landing exactly while the next message was being typed; it is now flushed in batches once the box
is quiet, which is also 64% cheaper per message. Deferring cost durability, so the pending set moved
out of an in-memory queue and into the schema — closing a hole the old design had too.

Also: a twelve-finding security pass, more than one wake phrase, and the fact extractor no longer
losing what it learned.

## How releases work
- Bump `pyproject.toml` → tag `vX.Y.Z` → GitHub Actions builds **four** images —
  `jarvis-combined`, `jarvis-orchestrator`, `jarvis-llama` and `jarvis-frontend` — at `X.Y.Z`
  **and** moves `latest`. **git tag = pyproject = image tags.**
- Published versions are **immutable** — a content change is always a new version (that's why v2.3.1
  exists).
- Test builds: run the workflow manually from any branch with an RC tag (e.g. `2.5.0-rc1`) — manual
  builds **never move `latest`**.
- Every release since v2.3.0 was validated on real deployments (the production box, a clean Actions
  runner, a laptop container) before tagging.
