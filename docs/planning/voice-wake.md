# Voice wake-word mode ("Jarvis, are you there?") — design plan

**Status:** design · **Date:** 2026-08-08

Goal: talk to JARVIS out loud in the lab. Say the wake word, speak, get a spoken reply, and keep
talking without repeating the wake word — with a screen in the room showing what it heard and what
it's doing. No always-on recording, no open microphone acting as an unbounded admin.

---

## 0. Where we actually are

This is not a greenfield feature. Most of it was built and then stalled on hardware.

| Piece | Location | State |
|---|---|---|
| Wake-word bridge | `src/scripts/voice_bridge.py` | Complete. `whisper-stream` → wake gate → `POST /inbox` → play Piper reply. Argv list, never a shell, so transcribed audio can't be executed. |
| Launcher | `src/scripts/run_listener.sh` | Ready |
| Wake-word-only reply | `main.py` `GET /greeting` | Working. Bare "Jarvis" / "Jarvis, are you there" → spoken ack, no LLM. |
| TTS | `main.py` `POST /tts` (Piper, `en_GB-alan-medium`) | Working |
| Listener credential | `config/voice_listener.key` (0600) | Minted |
| Browser push-to-talk | `App.jsx` + `whisper-worker.js` | Working (WASM Whisper, per-user, in-tab) |

**Three things block it, and only one is code.**

1. **No audio device.** The orchestrator runs in an **unprivileged** Proxmox LXC
   (`/proc/self/uid_map` → `0 100000 65536`). `/proc/asound/cards` shows the host's `HDA Intel PCH`
   because `/proc/asound` isn't namespaced, but `/dev/snd` **does not exist in the container**, so
   nothing can open a capture or playback device. `voice_bridge.py` was written for a single-box
   design that assumed the mic was simply there. It isn't. See §1.
2. **Continuous Whisper is unaffordable here.** i5-2520M, 3 cores, **AVX only, no AVX2**.
   `whisper-stream` transcribing the room 24/7 burns roughly a core forever, permanently, while
   competing with llama.cpp — already the system bottleneck (see the perf notes in `docs/AUDIT.md`).
   We would pay full STT cost on silence and on every unrelated conversation in the lab. See §2.
3. **The wake word is a substring match on a full transcription.** `voice_bridge.parse_line` does
   `text.lower().find("jarvis")`, so any mis-transcription containing something jarvis-shaped fires
   it, and the wake word *and* the command must land inside the same 5s window — you cannot pause
   between "Jarvis" and the request. `docs/FUTURE_IDEAS.md` already files this as
   "Wake-Word Optimization". See §2.

---

## 1. Hardware prerequisite (host-side, cannot be done from inside the container)

Two things are needed before any of this runs on the box.

**A microphone.** Two are on hand: a **Boya aux lav** (analog, first choice) and a **USB mic**
(fallback / bring-up). The daemon names its input in `config.json`, so switching is one line —
this is not an architectural fork.

- *Boya, analog:* goes into the onboard `HDA Intel PCH` mic jack. Boya lavs are wired **TRRS**
  (phone convention), so a PC mic jack needs a **TRRS→TRS adapter**; the input then needs
  **jack retasking + gain in `alsamixer`**, and lands as mono, low-gain. That noisier input is
  precisely what drives wake-word false positives, so expect to spend time on `-vth`/threshold.
- *USB:* enumerates as its own ALSA card, no retasking, cleaner levels.

**Suggested bring-up order** (not a decision, a debugging tactic): validate the whole path once on
the USB mic, *then* switch `capture.device` to the Boya. Otherwise a first-run failure is ambiguous
across four layers at once — cable/adapter, jack retasking, `/dev/snd` passthrough, and the wake model.

**Playback: both** (§5). That means passthrough is needed for **capture and playback**, and a
speaker must be wired to the box.

**`/dev/snd` passed into CT.** On the Proxmox host, in `/etc/pve/lxc/<CTID>.conf`:

```
lxc.cgroup2.devices.allow: c 116:* rwm
lxc.mount.entry: /dev/snd dev/snd none bind,optional,create=dir
```

ALSA is char major 116 (`/proc/devices`). Then restart the container — this is not hot-pluggable.

**The unprivileged part is the catch.** The bind mount carries the *host's* ownership: `/dev/snd/*`
is `root:audio` = uid 0, gid 29 on the host. Inside a container mapped `0 → 100000`, uid 0 and gid
29 fall outside the range, so the nodes appear as `nobody:nogroup` and even container-root cannot
open them. Pick one on the host:

- `chown :100029 /dev/snd/*` (host gid 100029 == container gid 29 == `audio`) via a udev rule so it
  survives reboot — **preferred**, keeps the nodes off world-accessible; or
- `chmod 0666 /dev/snd/*` — simpler, but any container/user on the host can then open the mic.

**And a systemd trap:** both orchestrator units set `PrivateDevices=true`, which scrubs `/dev/snd`
from the unit's namespace. That is correct and should stay — the orchestrator has no business
touching audio. The **voice listener must be its own unit** (`jarvis-voice-listener.service`) with
`PrivateDevices=false` and `SupplementaryGroups=audio`. Do not fold it into the orchestrator unit.

> If passthrough turns out to be more trouble than it's worth, the fallback is the Raspberry Pi
> satellite from `docs/FUTURE_IDEAS.md` — the daemon in §2 is written to be host-agnostic precisely
> so that move is a config change, not a rewrite.

---

## 2. The pipeline: two stages, not one

Replace "transcribe everything, grep for jarvis" with a cheap always-on detector that gates an
expensive on-demand transcriber.

```
mic ──▶ [ring buffer, 16kHz mono ]
          │
          ├─▶ stage 1: wake-word model  (openWakeWord "hey_jarvis", ONNX, always on, ~ms/frame)
          │              │ fires
          │              ▼
          └─────────▶ stage 2: VAD-gated capture ──▶ whisper-cli (one shot, on the utterance)
                                                        │
                                                        ▼
                                              POST /inbox ──▶ Piper TTS ──▶ playback
```

**Stage 1 — openWakeWord.** It ships a pretrained **`hey_jarvis`** model as ONNX, which lands
directly on the `onnxruntime` we already depend on (`pyproject.toml`) and already ship native libs
for (`piper/libonnxruntime.so`). Silence costs almost nothing. It also fixes the false-positive
problem structurally: an acoustic wake model scores the wake phrase, it doesn't pattern-match noisy
text. Weights come from the project's GitHub releases — first-party and SHA-256-pinned into
`src/scripts/download_models.sh`, per the project's supply-chain rule.

> **To verify before pinning:** the exact release asset name and hash for the `hey_jarvis` model,
> and whether the bundled melspectrogram/embedding preprocessor models are needed alongside it.

**Stage 2 — one-shot Whisper.** After a wake, capture until VAD says the utterance ended (cap ~10s),
then run `whisper/build/bin/whisper-cli` on that clip. Already built. We pay Whisper only for
speech actually addressed to JARVIS, which on this CPU is the difference between the feature being
viable and not.

**Pre-roll buffer.** Keep ~1.5s of audio *before* the wake fires, so "Jarvis, what's the LLM
queue?" spoken in one breath doesn't lose its first syllables. This is what makes the wake word feel
like part of the sentence rather than a button press.

**Conversation mode.** After a reply finishes, stay open for ~8s with no wake word required. This —
not the wake word — is what makes it feel like talking to someone. Chain the turns into one
`session_id` so context carries.

**Barge-in.** Speech detected during playback kills the playing audio immediately. Non-negotiable
for a room mic; without it, a long reply cannot be interrupted.

**Bare wake word → `/greeting`.** Already implemented and already the right behaviour: "Jarvis" or
"Jarvis, are you there" gets a spoken ack with no LLM round trip, so the fast path stays fast.

### Where the code lives

New `voice/` package at the repo root, mirroring `camera/`'s conventions (its own requirements,
`keyfile.py`-style credential handling, a `config.json`, a run script). `camera/` already solved
device-key storage and the Linux/Windows split; voice should look like its sibling, not like a
script in `src/scripts/`. `voice_bridge.py` becomes the reference implementation we port from and
then delete.

Capture uses `sounddevice` (PortAudio) in the voice package's own requirements — **not** added to
the orchestrator's `pyproject.toml` dependencies, which stay lean and torch-free for the container
image.

---

## 3. Identity: the mic is a device, not a user

Today `run_listener.sh` instructs `manage.py mint-key admin voice-listener`. That makes an
open microphone in a physical room a **full admin principal** — device control, memory, user
management, every household surface — for anyone standing near it. That is the one genuine security
defect in the current design, and it must not survive into an always-on deployment.

The right primitive already exists. `main.py`'s auth middleware reads `api_keys.device_id` and
deliberately strips admin from device-scoped keys:

```python
request.state.is_admin = (row["role"] == "admin") and not row["device_id"]
```

**Decision:** mint a **device-scoped key bound to device `voice-lab`**, in the primary household,
acting as the operator's user.

| Allowed | Denied |
|---|---|
| Chat / RAG / memory | Admin panel writes, user management |
| Timers, reminders | Anything gated by `_require_admin` |
| Device control (lights, volume) via `can_control_devices` on the owning user | Cross-household anything (already impossible post-v3.3) |

The trust boundary is **physical access to the lab**, which is an appropriate boundary for a room
you control. What this buys over the current admin key is that it is separately **revocable**
(kill the mic without touching your own sessions), separately **audited** (`audit_log` rows carry
the device), and **capped** — a bug or a mis-transcription cannot delete users.

**Normal users do not get the lab mic.** They already have per-user push-to-talk in their own
browser tab, on their own device, under their own identity. That is the correct place for
"other people can use voice", and it needs no new work. A shared room mic that impersonates whoever
is nearest is a different and much worse thing.

> Deliberately deferred: binding the voice session to the camera agent's SFace identity ("who is in
> the lab right now"). `POST /events` already ingests device-scoped presence events and its docstring
> already anticipates this — *"face/presence events will drive authorization later"*. Worth doing,
> but it should not gate v1, and it fails awkwardly when the camera can't see you.

---

## 4. Server-side session state

The daemon owns audio; the **server** owns the conversation state, so the screen (§5) is a pure
subscriber and voice works fully headless when no screen is on.

States: `idle → wake → listening → transcribing → thinking → speaking → (follow-up window) → idle`.

The daemon reports transitions through the existing device-scoped `POST /events` — no new ingestion
path, and provenance is already bound to the key. The server keeps the current state in memory
(one lab mic; this does not need a table) and republishes it over SSE, which `main.py` already does
in three places (`/chat/stream`, the volume stream, `/admin/events`).

New endpoints, both thin:

- `POST /voice/state` — daemon reports a transition (or reuse `/events` with a `voice.*` type)
- `GET /voice/stream` — SSE, the kiosk subscribes

---

## 5. The screen: `/voice` kiosk route

A **display**, not a second chat client. The mic stays in the native daemon: putting capture in the
browser would mean a tab that must stay open running WASM Whisper continuously on a no-AVX2 CPU,
which is the problem we just designed our way out of.

Lives in the existing frontend at `/voice`, exactly as the admin console lives at `/admin`
(`App.jsx` routes on `window.location.pathname.endsWith("/admin")`). It inherits auth, HUD styling,
theme and the build with no duplication, and the bundle stays one bundle.

Shows: the current state as an ambient indicator, the live transcript of what it heard, the reply as
it streams, and last-error/mic-health. Read-only, driven entirely by `GET /voice/stream`.

**No CSP change is required** — the kiosk never touches `getUserMedia`, and TTS audio already
arrives as `data:` URIs, which the existing policy permits.

### Playback ownership (decision: play in *both* places)

Replies play on the box's speaker when headless, and through the kiosk when a screen is up. Without
an explicit rule that means the same reply plays twice in the same room, so:

> **The server tracks whether a kiosk is currently subscribed to `GET /voice/stream`. If one is,
> the kiosk owns playback and the daemon suppresses local audio. If none is, the daemon plays
> locally.** Exactly one owner at any moment, decided server-side, not by either client.

Consequences worth stating: the state carries `playback: "kiosk" | "local"` so the daemon knows
without guessing; the kiosk must send an ack when audio finishes so `speaking → idle` (and the
follow-up window in §2) is driven by real playback rather than an estimate; and a kiosk that
disconnects mid-reply hands ownership back to the daemon for the *next* turn, not the current one.

This also gives barge-in a wrinkle: when the kiosk owns playback, the daemon detects the interrupting
speech but the kiosk holds the audio, so cancellation routes through the server rather than being a
local kill. Worth building as an explicit `voice.cancel` event rather than two half-solutions.

---

## 6. Phases

| Phase | Deliverable | Blocked on hardware? |
|---|---|---|
| 0 | Host `/dev/snd` passthrough + mic + `arecord` sanity check (§1) | **Yes** — host access, physical mic |
| 1 | `voice-lab` device key; `mint-key` docs corrected off `admin` (§3) | No |
| 2 | Voice session state + `GET /voice/stream` SSE (§4) | No |
| 3 | `/voice` kiosk route (§5) | No |
| 4 | `voice/` daemon: openWakeWord → VAD → whisper-cli → `/inbox` (§2) | Partly — writable without a mic, untestable without one |
| 5 | Conversation mode, barge-in, pre-roll tuning (§2) | **Yes** |
| 6 | `jarvis-voice-listener.service` (`PrivateDevices=false`), retire `voice_bridge.py` | **Yes** |

Phases 1–3 are pure server/UI work and can land while the mic is still in a box. Phase 4 is
writable but only meaningfully testable with real audio.

---

## 7. Decisions taken

| Question | Decision |
|---|---|
| Where the listener runs | **The server box**, via `/dev/snd` passthrough (§1). Pi satellite stays the documented fallback. |
| Mic | **Boya aux (analog)** first; USB mic as bring-up/fallback. One config line apart (§1). |
| Playback | **Both** — box speaker when headless, kiosk when a screen is up, with server-side ownership (§5). |
| Wake phrase | **"Hey Jarvis"** — openWakeWord's pretrained model, no training step (§2). |
| Mic authorization | **Device-scoped `voice-lab` key**, acting as the operator, admin surfaces denied (§3). |
| Normal users | **No lab-mic access.** They keep per-user browser push-to-talk under their own identity (§3). |
| UI | **`/voice` kiosk route** in the existing frontend, display-only (§5). |

## 8. Still unknown

1. **openWakeWord asset details** — exact release asset name + SHA-256 for the `hey_jarvis` model,
   and whether its melspectrogram/embedding preprocessor models must be pinned alongside it. Needed
   before `download_models.sh` can be touched (§2).
2. **Whether the Boya's analog path yields a usable SNR on this chipset** — unanswerable until §1
   is done and `arecord` produces a file we can look at. Determines how much §5 tuning is needed.
3. **Whether host `/dev/snd` passthrough is acceptable to you at all** — it is a host-level change
   to an unprivileged container's isolation. If not, the Pi satellite becomes the plan.
