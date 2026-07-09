# Release history — the project, iteration by iteration

The narrative view: what each release *was about* and why. For the granular change-by-change record
see [CHANGELOG.md](CHANGELOG.md); for published image tags see
[setup/image-releases.md](setup/image-releases.md).

| Version | Date | Theme |
|---|---|---|
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

## How releases work
- Bump `pyproject.toml` → tag `vX.Y.Z` → GitHub Actions builds `jarvis-combined` +
  `jarvis-orchestrator` at `X.Y.Z` **and** moves `latest`. **git tag = pyproject = image tags.**
- Published versions are **immutable** — a content change is always a new version (that's why v2.3.1
  exists).
- Test builds: run the workflow manually from any branch with an RC tag (e.g. `2.5.0-rc1`) — manual
  builds **never move `latest`**.
- Every release since v2.3.0 was validated on real deployments (the production box, a clean Actions
  runner, a laptop container) before tagging.
