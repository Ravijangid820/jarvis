# Jarvis AI — Changelog

All notable changes to this project are documented in this file.

---

## 2026-06-20 — Camera: `set-key` helper (no PowerShell quoting pitfalls)

- New **`set-key.ps1` / `set-key.sh`** write `config/agent.key` (or `config/admin.key` with `-Admin` /
  `--admin`) with exact bytes (no BOM, no trailing newline, 0600). Fixes a real footgun: the
  positional `Set-Content -NoNewline config\agent.key 'jk-…'` could bind the args in the wrong order
  (creating a file *named* after the key). Setup scripts, the camera doc, and the admin minted-key
  hint now point at the helper; the doc also shows the explicit `Set-Content -Path … -Value …` form.

## 2026-06-19 — Security review of the session's changes + hardening fixes

Independent 3-part review (server · camera · CI/supply-chain/frontend) of everything added this
session. **No critical/high issues** — the device-key admin-downscope, event provenance, last-admin
guards, hash-verified model downloads, and XSS-safe/server-enforced frontend all verified correct.
Applied the low/medium hardening it surfaced:

- **Bounded reads** of the server's `/faces/enrolled` response in the agent + `facecli` (cap 16 MB) —
  prevents OOM from a compromised/MITM'd server.
- **Atomic last-admin guards** — role demote is now a single conditional `UPDATE`; user-delete wraps
  the check+delete in `BEGIN IMMEDIATE` — closes a TOCTOU lock-out race.
- **`device_id` charset** restricted to `[A-Za-z0-9._:-]` (events + key minting) — no control chars.
- **device_heartbeats pruning** (drop rows >30 days) so the table can't grow unbounded.
- **Key files auto-tightened** to `0600` on POSIX (was warn-only); **plaintext-HTTP warning** when the
  agent would send its key over `http://`.
- **CI least-privilege**: workflow default `contents: read`; `write` only in a tag-gated `release`
  job. Auditable **uv install** alternative (winget / pipx) added to setup; `opencv-python<5` bound.
- Open by design (documented): **TLS** (the LAN is still plaintext — the top remaining item) and a
  hash-pinned dependency lockfile. Tests 57 → 58.

## 2026-06-19 — Camera: one-click install + reproducible Windows .exe (CI-built)

- **One command:** `install.sh` / `install.ps1` chain *setup* + *service* (thin, readable wrappers —
  no opaque binary).
- **Packaged Windows `.exe`** for a no-scripts option: new `app.py` dispatcher
  (`run` / `verify` / `setup` / `install-service` / `uninstall-service`), `models.py` (first-run model
  download + sha256-verify), and a frozen-aware `paths.base_dir()` (config/models live next to the
  .exe). Built **reproducibly in CI** — `.github/workflows/build-camera-exe.yml` builds `jarvis-camera.exe`
  on a Windows runner *from source*, publishes it with a SHA-256 (artifact, or a Release on a
  `camera-v*` tag), so the binary is traceable to a commit. Self-installs a per-user logon Scheduled
  Task (not elevated); unsigned (one-time SmartScreen prompt). Non-frozen behavior unchanged.

## 2026-06-19 — Admin: role management (promote/demote) + last-admin protection

- **Promote/demote users** from the admin Users tab — new `PUT /admin/users/{id}/role`
  (`user`|`admin`). Promotion is live immediately for the user's existing session (role is read
  per-request). UI shows **Make admin / Revoke admin** per user.
- **Last admin can't be removed:** demoting or deleting the only remaining admin → **400** (so you
  can never lock yourself out). The delete path also re-raises `HTTPException` so the 400/404 isn't
  masked as a 500. Tests 53 → 57.

## 2026-06-19 — Camera: split task scripts (setup · run · service), Linux + Windows

- Three purpose-specific scripts, each with a Linux/macOS/Pi `.sh` and a Windows `.ps1`:
  **`setup`** (deps + model download), **`run`** (foreground test — nothing installed), **`service`**
  (`install|uninstall|status` — least-privilege autostart).
- **`service`** persistence is least-privilege by design: Linux = a **systemd *user* service** (runs
  as you, never root; hardened; `loginctl enable-linger` for pre-login boot on a Pi); Windows = a
  **Scheduled Task at logon** (your account, *not* elevated, no admin, no third-party wrapper). The
  agent stays **outbound-only** — no listening port either way.

## 2026-06-19 — Camera: YuNet+SFace face stack + smart setup with verified model download

- **Face stack → OpenCV YuNet (detect) + SFace (recognize).** YuNet returns 5 landmarks → SFace
  **aligns** each face before embedding (128-D), which is more accurate than the old center-crop. Runs
  through **opencv-python only** — drops MediaPipe + onnxruntime from the face path (MediaPipe now
  only for optional pose/gestures). `recognize_threshold` is SFace cosine (default 0.363).
- **Models auto-downloaded by setup, from the official OpenCV Zoo, SHA-256-verified** (hashes taken
  from OpenCV's own git-LFS pointers; mismatch aborts the install — supply-chain check). Stored in
  `camera/models/` (gitignored). Verified end-to-end here: download+verify, model load, detect, SFace
  align/embed → 128-D unit vector, cosine match.
- **Smart `setup.sh`** auto-detects **Raspberry Pi vs Linux vs macOS** and installs accordingly (apt
  OpenCV+picamera2 on Pi; pip opencv on desktop), then fetches + verifies the models. `setup.ps1`
  does the same on Windows. Faces need no extra deps; `--with-pose` / `-WithPose` adds mediapipe.
- Note: switching the embedding model invalidates any previously-enrolled faces (different vector
  space) — re-enroll. (None were enrolled in production.)

## 2026-06-19 — Camera: face-management CLI + device-key privilege hardening

- **`jarvis_camera.facecli`** — one CLI to `list` / `verify` / `add` / `delete` faces from the device
  with the camera. `verify` is **fully local** (capture → recognize on-device → print who's there,
  sends nothing). `list`/`verify` use the low-privilege **device** key; `add`/`delete` use a separate
  **admin** key (`config/admin.key`) the always-on agent never loads. New `keyfile.py` centralizes
  key loading + the device/admin split + perm warnings; `enroll` now uses the admin key file.
- **[SECURITY] A device-scoped API key can no longer wield admin**, even if minted under an admin
  account (`is_admin = role==admin AND not device_id`). Found by testing — an admin-owned device key
  could hit `/admin/*` and `/faces/enroll`. Now `/admin/*` + enroll → **403** for device keys, while
  `/faces/enrolled` (read) + `/events` still work. Bounds a stolen camera key's blast radius. Tests
  52 → 53. Added a "Security & attack surface" section to the camera doc (outbound-only, no listener,
  no imagery leaves the device, mint the camera key under a non-admin user).

## 2026-06-19 — Rename `edge/` → `camera/` (one canonical, Windows-first camera module)

- The vision module is now **`camera/`** (package **`jarvis_camera`**), reflecting that it's a
  general on-device camera agent — laptop webcam *or* Pi — not a Pi-only "edge" thing. `git mv`
  preserved history; logger namespace `edge.*` → `camera.*`; the device key file is now
  `camera/config/agent.key`. No code duplication (the whole point — fewer copies to keep secure).
- Run commands are now `python -m jarvis_camera.{agent,enroll,bench}`. Docs renamed
  `setup/raspberry-pi.md` → `setup/camera.md` (Windows-first, Pi still covered); README/admin-UI
  hints updated. Server endpoints, schema, and the event contract are unchanged.

## 2026-06-19 — Admin: mint device-bound (camera/edge) keys from the UI

- The **Keys** tab now has a **Device ID** field — minting with one creates a **device-bound** key,
  the kind a camera/edge agent needs (a plain unbound non-admin key is rejected by `/events`). No
  more dropping to `manage.py mint-key` on the box for this. The keys table gained a **Device**
  column; the minted-key callout reminds you to save it to `edge/config/edge.key` and run the agent.
  The Faces → Camera Agents panel links to the Keys tab to start the flow. `POST /admin/api_keys`
  accepts + returns `device_id`; `GET /admin/api_keys` returns it. Tests 50 → 52.
- (Inherent limits, by design: the agent **runs on the device**, so the server can't launch it; and
  saving the key to a file is a device-side step — the outbound-only agent holds its own credential.)

## 2026-06-19 — Edge: Windows laptop setup + correct sandboxed run command

- **`edge/setup.ps1`** — one-shot Windows bootstrap (uv-managed **Python 3.12** venv — MediaPipe has
  no 3.13 wheels yet — + desktop deps; `-WithFaces` adds mediapipe + onnxruntime). Nothing global.
- **Run-command fix:** `uv run --no-project python -m jarvis_edge.…` actually used the **system**
  Python (or the server's env), not `edge/.venv`, so sandboxed deps were invisible. All edge docs +
  scripts now run via the venv's python directly — `.venv/bin/python …` (Unix) / `.venv\Scripts\python …`
  (Windows). Added a Windows section + camera/firewall gotchas to the edge setup doc.

## 2026-06-19 — Admin console: tabbed layout + live service status

- The admin page is now **tabbed** (Overview · Users · Keys · Faces) instead of one long scroll —
  each area is its own section, with counts on the Users/Keys/Faces tabs.
- **System Services board** (Overview): live **green ACTIVE / red INACTIVE** status for each
  subsystem — Orchestrator, LLM (pings llama `/health`), Embeddings/RAG, Voice/TTS (Piper), and one
  row **per camera agent** (Pi / laptop). Camera liveness is the answer to "is the model running on
  the hardware". The Faces tab shows the camera-agent status above the enrolled list. Auto-refreshes.
- **Device liveness:** new `GET /admin/services`, a `device_heartbeats` table, and a `heartbeat`
  event type — the edge agent pings every 30s so an idle-but-running camera still reports active
  (heartbeats update liveness only; they're not stored in the `vision_events` feed). Tests 47 → 50.

## 2026-06-19 — Face management: server store + admin API (foundation)

- New `faces` table + endpoints: `POST /faces/enroll` (admin; stores an embedding computed on the
  edge), `GET /faces/enrolled` (the edge pulls the known set to match locally), and admin
  `GET`/`PUT`/`DELETE /admin/faces` to list, rename, **link a face → a user account** (for per-user
  authz), and remove. Recognition stays on the edge; only vectors are stored. Tests 45 → 47.
- **Admin "Faces" sub-page** — lists enrolled people, **links a face → a user account** (dropdown,
  powers per-user device authz), and deletes; shows the enroll command.
- **`jarvis_edge.enroll` CLI** — captures ~7 frames on the device, averages the face embedding, and
  registers it (`POST /faces/enroll`). The **agent pulls `/faces/enrolled`** at startup so
  recognition matches the centrally-managed identities. (`FaceDetector.embed()`/`set_known()`
  factored out for reuse.) Enrollment needs an ONNX embedding model + an admin key.

## 2026-06-19 — Edge faces: MediaPipe BlazeFace detector (with Haar fallback)

- Face **detection** now prefers **MediaPipe BlazeFace** (more robust than the Haar cascade —
  fewer false positives, better with angle/lighting, CPU-friendly), automatically falling back to
  DNN/Haar when MediaPipe isn't installed (so it still works on any device). **Identity** is
  unchanged — the optional ONNX embedding still answers *who* (MediaPipe finds *where*). New
  `min_confidence` config. Unifies the detector stack (pose/gestures already use MediaPipe).

## 2026-06-18 — Edge camera: testable on a laptop (no Pi needed)

- The edge vision agent runs on a **laptop webcam** (the camera layer already falls back to OpenCV
  when picamera2 is absent). Added **`edge/requirements-desktop.txt`** (opencv-python + numpy +
  requests; mediapipe/onnxruntime optional) and a **"Test on a laptop"** guide in the edge setup
  doc — `bench` + `--dry-run` work with no server. Windows-safe signal handling in the agent.
- **Decision:** the edge code stays in the monorepo (the clone is small — models/builds are
  gitignored; the event contract stays in sync with the server; `git sparse-checkout set edge`
  gives an edge-only checkout if ever needed).

## 2026-06-18 — JARVIS speaks: spoken greeting + voice-triggered "Yes, sir?"

- **`POST /tts`** (synthesize any text → WAV) and **`GET /greeting`** (a time-aware JARVIS
  acknowledgement + audio), both auth-gated.
- **Web UI:** the welcome greeting is now **spoken** via Piper TTS when the sound toggle is on —
  on login or the first page gesture (browsers block autoplay until a gesture).
- **Voice bridge:** hearing just the wake word ("Jarvis") → a spoken greeting (`GET /greeting`);
  any other command → `/inbox` with `voice_feedback`, and the reply is **spoken back** (played with
  `paplay`/`aplay`/`ffplay`). Needs an audio player + output device on the box — on-hardware tuning
  required. Tests 42 → 45.
- **One toggle now governs all audio.** Consolidated the old separate "Sound" + "Voice Output"
  switches into a single **JARVIS Voice** toggle (Parameters panel / ⌘K) — when on, you get UI
  blips, the spoken greeting, **and spoken chat replies** (`voice_feedback` follows it).

## 2026-06-18 — JARVIS-style greeting on the welcome screen

- The empty-chat welcome screen now greets the user in JARVIS's voice, **typed out** character by
  character: time-aware ("Good morning/afternoon/evening"), addressed to the logged-in user **by
  name** (or **"sir"**, à la JARVIS, when no name), with a rotating tagline ("At your service.",
  "Welcome home.", "Ready when you are.", …). The username is persisted at login (`jarvis_user`).
  Grounded in the films' JARVIS dialogue. Next: a **spoken** greeting via Piper TTS and a
  **voice-triggered** "when it hears you" greeting through the listener.

## 2026-06-18 — Realistic Mark I arc reactor (replaces the plain concentric circles)

- New reusable **`<ArcReactor>`** modelled on the iconic **Mark I "PROOF THAT TONY STARK HAS A
  HEART"** reactor: a brushed-steel ring with **engraved text**, **copper wound coils alternating
  with blue-glow panels**, a bolt ring, concentric inner rings, and a **layered blue core**.
  Recreated as vector art (iterated via headless render against a reference photo), **static**, with
  per-instance gradient/textPath ids via `useId()`. Replaces the radar-like concentric circles in
  the sidebar logo, login, welcome screen, boot overlay, and chat backdrop (the engraved text
  auto-hides at the tiny logo/login sizes).

## 2026-06-18 — Calmer, static UI (no cursor parallax / idle motion)

- **Removed cursor parallax** — the arc reactor, particles, and messages no longer tilt to the
  pointer (and the per-pointer-move handler is gone). Calmer, prettier, zero pointer-driven repaints.
- **Idle decorative animations made static**: ambient reactor "breath", HUD corner-bracket pulse,
  title holo-flicker, status-dot pulse, and the oscilloscope scroll. The **idle UI is now fully
  static**; the brief feedback animations (typing dots, streaming scan, thinking pulses) remain but
  only play *during* an active request.
- Arc reactor is now a static, centered focal point (a touch more present). Depth comes from static
  gradients + glows, not motion.

## 2026-06-18 — Smoother chat scroll (render + GPU-paint fixes)

- **Streaming no longer re-parses every message each token.** Messages render through a `memo()`'d
  `<MessageItem>`, so a token re-renders only the last message (was re-parsing the whole list).
- **Removed the per-frame GPU work behind the scroller.** The scanline overlay and the ambient
  particles are now **static** — they were continuously changing the backdrop and forcing the
  frosted-glass panels to re-blur every frame (the "laggy even when idle" cause). Glass + reactor
  kept; the panels now blur once and cache.
- Auto-scroll coalesced into one `requestAnimationFrame`; `overflow-anchor: none`.
- New **"Reduce effects"** toggle (Theme panel / ⌘K, persisted) + a `prefers-reduced-motion`
  fallback that drops particles + glass blur entirely for the lightest mode.

## 2026-06-18 — Project documentation: CONTRIBUTING, SECURITY, templates; README polish

- Added the standard OSS document set: **CONTRIBUTING.md** (dev setup, tests/lint, conventions, PR
  style), **SECURITY.md** (report via GitHub private advisories + the security model), and
  `.github/` PR + bug/feature issue templates.
- README front-door polish: links the setup guides + CONTRIBUTING/SECURITY/CHANGELOG; refreshed
  Project Structure (portable `<repo>` root, `edge/` + `clients/`, `install_services.sh`); the
  Security section now reflects the non-root services + CSP.
- **Licensed under Apache-2.0**: added the canonical `LICENSE` (copyright Ravi Jangid) + `NOTICE`,
  `license = "Apache-2.0"` in `pyproject.toml`, a README badge + License section.

## 2026-06-18 — Portable install: one installer, run as root OR a dedicated user

- **`src/scripts/install_services.sh`** — single installer that works from any checkout path and
  installs both services either as **root** (`install_services.sh`, simplest) or a **dedicated
  non-root user** (`JARVIS_USER=jarvis …`, hardened). Auto-detects repo/`uv`/`llama-server`/GGUF,
  generates both units for the chosen mode, and (non-root) creates the user, moves the HF cache
  under the repo, narrows write access, relocates a `/root` llama build to `/opt`. `DRY_RUN=1`
  previews; `JARVIS_GGUF`/`JARVIS_HOST`/`JARVIS_PORT` override. Replaces the box-specific
  `harden_service.sh`/`harden_llama.sh` (removed).
- **Portability fixes:** `manage.py`, `reembed_memory.py`, `fetch_fonts.py`, `run_listener.sh` no
  longer hardcode `/srv/jarvis` — they resolve config/paths repo-relative (`JARVIS_HOME`/
  `JARVIS_CONFIG`), so a fresh clone works at any path, as any user. README/server.md/DEPLOY updated.

## 2026-06-18 — Least-privilege follow-ups: narrowed writable scope + non-root llama-fast

- **Orchestrator writable scope narrowed:** the hardened unit's `ReadWritePaths` is now just
  `memory`/`logs`/`.cache`/`.venv` (was the whole tree). `harden_service.sh` keeps source + `.git`
  root-owned (read-only to the service — an RCE can't rewrite its own code/history); `uv run
  --no-sync` so boot never writes the venv/lock. `config/` is readable but not writable.
- **`llama-fast` is now non-root:** new `src/scripts/harden_llama.sh` copies the build out of
  `/root` to `/opt/llama.cpp` (with `LD_LIBRARY_PATH`, since its RUNPATH was absolute) and installs
  a non-root unit (`User=jarvis`, `ProtectSystem=strict`, no writable paths).
- Both applied live and verified (health + a real inference round-trip). No remaining root service.

## 2026-06-17 — Hardening round 3 + adversarial recheck (F8/F17 + breakout sweep)

Independent verifier attacked the new auth/device/voice/migration code — **no critical/high** (no
bypass, privesc, injection, or breakout). Acted on its findings + closed remaining supply-chain RCE:
- **F8:** `trust_remote_code=False` on the embedding model (closes model-repo RCE) + `EMBED_MODEL_REVISION`
  pin; Piper `PIPER_VERSION` pin + `PIPER_SHA256`/`VOICE_SHA256` verify hooks.
- **F17:** voice/edge/volume agents warn if the key file is group/other-readable; `mint-key` coerces
  an empty device arg to NULL.
- **Race fix:** device-command claim is now a single atomic `UPDATE…RETURNING` (no double-delivery).
- **llama-fast.service** hardened (`RestrictSUIDSGID`/`LockPersonality`/`ProtectKernelModules`/`UMask`).
- **F22** accepted (CSP verified effective; HttpOnly-cookie migration intentionally skipped — adds CSRF
  surface for negligible gain here). Tests still 42; see AUDIT.md Resolution table.

## 2026-06-17 — Hardening follow-up: non-root service (F3) + voice listener rewrite (F24)

- **F3 (non-root):** added `systemd/jarvis-orchestrator.hardened.service` (`User=jarvis`,
  `ProtectSystem=strict` + `ReadWritePaths` + `ProtectHome`, `HF_HOME` under the owned tree) and
  an idempotent `src/scripts/harden_service.sh` that creates the user, copies `uv` + the HF cache,
  chowns `/srv/jarvis`, installs the unit, restarts and health-checks (with a rollback hint).
  Operator runs it once; `llama-fast` left as root (loopback-only, follow-up). DEPLOY.md section added.
- **F24 (voice listener):** rewrote the broken/unsafe `whisper-command -cmd "curl … %s"` line.
  New `src/scripts/voice_bridge.py` runs `whisper-stream`, gates on the wake word, and POSTs the
  command as JSON via urllib — **no shell** (transcripts can't be executed) and it actually
  transcribes. `run_listener.sh` now just launches the bridge. Needs on-box mic/flag tuning.

## 2026-06-17 — Security hardening pass (resolves most of F1–F24)

Acted on the 2026-06-17 review ([AUDIT.md](AUDIT.md) — see the Resolution status table). Fully
fixed (15): **F1** device↔API-key binding (`api_keys.device_id`; `mint-key … <device_id>`;
`/devices/commands` requires the key be bound to that device; `/events` provenance from the key,
plain users denied), **F2** login throttle keyed per-username (no shared-IP global lockout), **F4**
async long-poll (no thread-pool exhaustion, concurrency-capped), **F5** CSP + Referrer-Policy,
**F6** DB chmod 0600 + `UMask=0077`, **F7** event `data` cap + retention on events/commands, **F11**
403/404 on cross-user/missing mutations, **F12** `/system` admin-only, **F13** generic 500, **F14**
auth length bounds, **F15** role enum, **F18** volume agent client-side validation, **F20** PBKDF2
600k (legacy hashes still verify), **F21** tighter `_safe_exec`, **F23** rate-limit bucket eviction.
Partial: **F3** `UMask` added (non-root `User=` still an operator step), **F8** GGUF https/checksum
+ `LLAMA_CPP_REF` pin + dep upper-bounds, **F10** added `/auth/logout-all`, **F19** dep bounds.
Deferred: F9/F16 (accepted), F17, F22 (CSP mitigates), F24 (functional). Tests: 29 → **42**.

## 2026-06-17 — Security review (whole project) logged

- Ran a follow-up multi-reviewer security review across the newer surface (device `/events` +
  `/devices/*` endpoints, the Pi edge agent, the Windows volume agent, install/supply-chain
  scripts, frontend, infra) and recorded all findings in [docs/AUDIT.md](AUDIT.md) as F1–F24
  (all OPEN for review — no fixes applied yet). Headline: the device command/event queue
  self-asserts `device_id` (no API-key binding) and the login limiter is IP-keyed (global
  lockout behind the subnet router). Also documented a corrected false positive (whisper `-cmd`
  is a commands-file, not a shell template — no RCE) and the verified-clean list.

## 2026-06-16 — Docs: grouped setup guides under docs/setup/

- Per-component setup docs now live together under **`docs/setup/`**: `server.md` (new,
  consolidated), `camera.md` (moved from `edge/README.md`), `volume-agent.md` (moved from
  `clients/volume-agent/README.md`). The `edge/` and `clients/volume-agent/` dirs keep a short
  pointer README. The docs index links all three under "Setup guides (by component)".

## 2026-06-16 — Secure device control: volume (pull-model agent)

- **Authorized volume control**, security-first: `POST /devices/volume` enqueues a validated
  command (`set`/`step`/`mute`/`unmute`) — **authorized server-side** (admins, or users with the
  new `can_control_devices` flag); the LLM is never the authz boundary. Device agents **pull**
  their commands via `GET /devices/commands` (long-poll), so the device opens **no inbound port**.
- **`clients/volume-agent/`** — outbound-only Windows agent: polls the orchestrator and sets the
  master/Bluetooth volume via `pycaw` (no shell-out → no injection; runs as a normal user). New
  `device_commands` table + `users.can_control_devices`. Tests added (suite: 29). Untested on the
  Windows laptop. Lights via Home Assistant REST + an LLM `set_volume` tool are the next steps.

## 2026-06-16 — Edge vision: /events endpoint + detector implementations

- **Server:** `POST /events` ingests edge events (auth via the existing middleware; stored in a
  new `vision_events` table), and `GET /admin/events` lists recent ones. Tests added (suite: 26).
- **Edge detectors implemented** (were stubs): faces (OpenCV Haar/DNN detection + optional ONNX
  identity), pose (MediaPipe → presence/zone/posture), gestures (MediaPipe Hands →
  open_palm/fist/thumb_up/down/point). All lazy-import + degrade gracefully if a dep/model is missing.
- **`bench.py`** — per-detector FPS/latency benchmark to run on the Pi and decide what's feasible.
- Still untested on the Pi hardware (it's offline); the benchmark is the way to validate capability.

## 2026-06-16 — Edge vision agent (Raspberry Pi) — scaffold

- New `edge/` subdirectory: an on-device camera agent for a Raspberry Pi that runs recognition
  **locally** and POSTs small JSON **events** (no video) to the orchestrator via a machine API
  key — keeping the server light and imagery on-device. Its own isolated env (separate from the
  server's pyproject).
- Foundation implemented: camera abstraction (picamera2/OpenCV), motion detection (MOG2),
  event client (background POST + retry), and a **motion-gated, one-heavy-task scheduler**
  tuned for the Pi 3 B+ (1 GB RAM) reality. Heavy detectors (faces/pose/gestures via MediaPipe
  + ONNX) are stubs with interfaces + on-Pi tuning notes. Setup script + README + config example.
- Untested on hardware (Pi not yet connected). Pending server side: a `POST /events` endpoint.

---

## 2026-06-16 — Frontend lint cleanup

- `npm run lint` is clean (0 errors/warnings). Removed the unused `React` import, fixed the
  empty `catch` blocks (dropped unused bindings), and silenced the intentional
  init-effect dep warning. Turned off the React-Compiler-only rules (`react-hooks/immutability`,
  `purity`, `set-state-in-effect`) — they target builds using `babel-plugin-react-compiler`
  (which this app doesn't) and flagged legitimate patterns; kept rules-of-hooks + exhaustive-deps.
- Logged a **Dockerfile + Compose** containerization task in `docs/FUTURE_IDEAS.md`.

---

## 2026-06-16 — Setup scripts (fresh-clone / container bootstrap)

- `src/scripts/setup.sh` — one-shot, idempotent bootstrap: `uv sync`, config from example,
  frontend build, DB init, admin user, native builds, model downloads. Toggles: `SKIP_NATIVE`,
  `SKIP_MODELS`, `ADMIN_USER`/`ADMIN_PASS`.
- `src/scripts/download_models.sh` — embedding model (HF cache), Piper, whisper `base.en`, and
  the LLM GGUF (`LLM_GGUF_URL`, since the source isn't pinned). Non-fatal per step.
- `src/scripts/build_native.sh` — clone + build whisper.cpp (v1.8.6) and llama.cpp (AVX-only)
  into the repo.
- `piper_setup.sh` made repo-relative (was hardcoded to /srv/jarvis).
- Data paths are now portable: relative `db_path`/`chroma_db_path` resolve against `BASE_DIR`
  (example config switched to relative paths); `init_db()` creates the data dir. Absolute paths
  on the deployed box are unchanged. README gains a "From a fresh clone" Quick Start.

---

## 2026-06-16 — Lazy model load, offline HF, end-to-end auth tests

- **Embedding model loads lazily at startup, not at import.** `memory.init_embeddings()`
  (called from the app lifespan) loads `embeddinggemma-300m` from the local HF cache;
  `import memory`/`import main` no longer pull torch (imports drop from ~35s to <1s). Set
  `JARVIS_NO_EMBED=1` to skip it (RAG disabled) — used by tests. *The model is not
  re-downloaded each start — it's cached (~1.2 GB in `~/.cache/huggingface`).*
- **Offline HuggingFace** in the systemd unit (`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`)
  so startup is strictly cache-only — no network call to check for model updates. (The model
  must be present in the cache; it already is. Fresh installs download it once, online.)
- **`tests/test_api.py`**: end-to-end auth-middleware tests via FastAPI TestClient (missing/
  invalid token → 401/403, login + authed, wrong password, admin gating, session-ownership
  IDOR over HTTP, login throttling → 429, tokens stored hashed). Now possible thanks to the
  config refactor + lazy load; runs without the model. Suite is now **24 tests**. Added
  `httpx` as a dev dependency.

---

## 2026-06-16 — Config path portability

- `config.py` now derives every on-disk path from a `BASE_DIR` (the repo root via
  `__file__`, overridable with `JARVIS_HOME`) instead of hardcoding `/srv/jarvis`, and
  falls back to `jarvis.example.json` when `jarvis.json` is absent. On the deployed box
  this resolves to the identical `/srv/jarvis` paths (no behavior change); off-box (CI,
  a fresh checkout, tests) the app/config can now be imported and run. `schema.sql` path
  moved into config as `SCHEMA_PATH`. Enables future HTTP/middleware tests (which also
  need the embedding model to load lazily — separate follow-up).

---

## 2026-06-16 — Session-ownership tests + TLS guide

- Added `tests/test_sessions.py`: CI-safe authorization tests for session ownership —
  `require_owned_session`, the cross-user `delete_session` IDOR (must 403 and preserve the
  victim's data + vectors), and `rename_session` ownership. (Stubs `config`/`memory` so it
  runs without the `/srv/jarvis` config or the embedding model. Suite is now 16 tests.)
- DEPLOY.md: added an **"Adding TLS (HTTPS)"** section — bind the orchestrator to loopback
  and terminate TLS with Tailscale Serve (recommended for this setup) or Caddy, so bearer
  tokens never travel as plaintext. Notes the login-limiter behavior behind a proxy.

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
