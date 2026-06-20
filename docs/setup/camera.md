# Jarvis Camera (on-device vision agent)

An on-device agent that watches a camera and sends **high-level events** (not video) to the
Jarvis orchestrator. All recognition runs **on the device** so the server isn't loaded, and no
imagery ever leaves the device — only small JSON events over the LAN. The **same code** runs on a
**Windows/macOS/Linux laptop webcam** (start here — see *Test on a laptop* below) and a **Raspberry
Pi** camera; the capture layer picks the backend automatically.

> **Code:** [`camera/`](../../camera/) in the repo. Run the commands below **on the device with the
> camera, from the `camera/` directory** (the laptop you're testing on, or the Pi).

> Status: **runs on a laptop webcam today; untested on Pi hardware.** The foundation (capture,
> motion, event client, agent loop) is standard. The heavy detectors (faces/pose/gestures) are
> **implemented**; on a Pi, use `bench.py` (below) to measure real FPS and decide what to enable.

## Hardware reality (Raspberry Pi 3 B+, 1 GB RAM)

The Pi 3 B+ (4×Cortex-A53 @1.4 GHz, 1 GB RAM, no ML accelerator) is the binding constraint:

| Capability | Feasibility on Pi 3 B+ | Approach |
|---|---|---|
| Motion detection | ✅ easy | OpenCV frame-diff / MOG2 at low res, always-on |
| Face recognition | ⚠️ tight | **triggered** by motion+face, low res, throttled (not every frame) |
| Body/pose tracking | ⚠️⚠️ slow (~2–4 FPS) | MediaPipe Pose, **one at a time**, off by default |
| Hand gestures | ⚠️⚠️ slow | MediaPipe Hands, on-demand |

**Do not run pose + gestures + face-recog concurrently on 1 GB — it will thrash/OOM.** The agent
uses a **motion-gated, single-heavy-task scheduler** (below). MediaPipe needs a **64-bit OS**;
add a **swapfile**. The code is hardware-portable: a Pi 4/5 just runs more, faster, no rewrite.

## Architecture

```
camera ─► capture.py ─► agent.py (loop)
                          │  motion.py  (cheap, every frame — the gate)
                          │     └─ on sustained motion, escalate to ONE heavy detector,
                          │        round-robin + throttled by per-detector interval:
                          │           faces.py · pose.py · gestures.py
                          ▼
                       events.py ──HTTP POST (machine API key)──► orchestrator /events
```

- **Only events cross the network** (e.g. `{"type":"face_seen","name":"Ravi"}`), so the server
  stays light and no imagery leaves the Pi.
- Auth reuses the existing **machine API key** (mint with `manage.py mint-key` on the server).

## Event contract (Pi → orchestrator)

`POST {server.url}{server.events_endpoint}` with `Authorization: Bearer <device key>`:

```json
{ "device_id": "pi-livingroom", "type": "motion|face_seen|pose|gesture",
  "ts": "2026-06-16T20:00:00Z", "data": { "...": "type-specific" } }
```

The server `POST /events` endpoint **exists** (auth via the orchestrator's middleware/API key);
admins can review recent events at `GET /admin/events`. Run the agent with `--dry-run` to log
events locally without sending.

## Check Pi capability (do this first on the Pi)

```bash
# motion + faces need only opencv + the models (setup installs both). For pose/gestures, first:
#   bash setup.sh --with-pose      (installs mediapipe)
cd camera && .venv/bin/python -m jarvis_camera.bench --frames 60
#   motion   :  28.0 FPS  (  35.7 ms/frame)
#   faces    :   ~?  FPS  (YuNet detect + SFace recognize — measure on your Pi)
#   pose     :   2.3 FPS  ( 435.0 ms/frame)   ← decide if this is usable for you
#   gestures :   3.1 FPS  ( 322.0 ms/frame)
```

> **Always run via the venv's python** (`.venv/bin/python …`, or `.venv\Scripts\python …` on
> Windows). `uv run` is for the *server* project; from `camera/` it would pick the system Python (or
> the server's env), not `camera/.venv` — so the sandboxed deps wouldn't be found.

Use the numbers to set each detector's `interval_s` (and whether to enable it) in config.

## Setup (on the Pi)

```bash
bash camera/setup.sh          # 64-bit check, apt deps, uv venv + uv pip install, config
# then on the SERVER, mint a key for this device and copy it to the Pi:
#   uv run python src/scripts/manage.py mint-key <user> pi-vision <device_id>  →  camera/config/agent.key
#   (the last arg binds the key to that device — it may then only post events as that device)
cp camera/config.example.json camera/config/config.json   # review server.url / camera / detectors
cd camera && .venv/bin/python -m jarvis_camera.agent       # or add --dry-run
```

## Test on a laptop (no Pi) — uses your webcam

The same code runs on a laptop: the camera layer falls back to **OpenCV/webcam** when picamera2
isn't present, so you can test the whole pipeline before you have the Pi. **One setup script does it
all** — detects the platform, installs deps into a uv-managed venv (nothing global), and downloads +
**sha256-verifies** the face models (YuNet + SFace) from the official OpenCV Zoo.

> Faces (YuNet detect + SFace recognize) run through **opencv-python only** — no MediaPipe, no
> onnxruntime. MediaPipe is needed **only** for the optional pose/gestures (and has no Python 3.13
> wheels yet, which is why the venv pins 3.12). uv fetches a managed CPython — not a system Python.

### Windows (PowerShell)

```powershell
# 0. Install uv once if you don't have it (user-level binary, not a global Python package):
#    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
# 1. Get the code (small; venv/models/config are gitignored):
git clone <repo> jarvis ; cd jarvis\camera
# 2. One script: venv + opencv + download/verify the face models  (add -WithPose for pose/gestures):
powershell -ExecutionPolicy Bypass -File setup.ps1
# 3. Edit config\config.json: device_id, server.url="http://192.168.0.101:5000".
# 4. Test with NO key/server first (run via the venv's python — fully sandboxed):
.venv\Scripts\python -m jarvis_camera.agent --dry-run    # webcam on, events logged, nothing sent
# 5. Save the device key (helper avoids PowerShell quoting pitfalls), then go live:
powershell -ExecutionPolicy Bypass -File set-key.ps1 jk-yourkey
.venv\Scripts\python -m jarvis_camera.agent              # events POST → admin shows the camera green
```

### macOS / Linux / Raspberry Pi

```bash
git clone <repo> jarvis && cd jarvis/camera
bash setup.sh                 # auto-detects Pi vs Linux vs macOS; installs deps + downloads models
#   bash setup.sh --with-pose # also install mediapipe (pose + hand gestures)
# edit config/config.json (server.url, camera.device); test, save the key, go live:
.venv/bin/python -m jarvis_camera.agent --dry-run
bash set-key.sh jk-yourkey
.venv/bin/python -m jarvis_camera.agent
```

> Mint the device key **on the server**: admin → **Keys** → set a Device ID (e.g. `laptop-cam`),
> **under a non-admin user**, then save it with **`set-key.ps1 jk-…`** (Windows) / **`set-key.sh jk-…`**
> (Unix) — these write `config/agent.key` exactly (no quoting/encoding pitfalls). To write it by hand
> instead, use the explicit form `Set-Content -Path config\agent.key -Value 'jk-…' -NoNewline` (the
> positional form can bind the args in the wrong order). Enrolling faces needs an **admin** key:
> `set-key.ps1 jk-… -Admin` (Unix: `set-key.sh jk-… --admin`) → `config/admin.key`; remove it after.

**Windows gotchas:** allow camera access (Settings → Privacy & security → Camera → *Let desktop apps
access your camera*); close Teams/Zoom/etc. so the webcam is free; the server's `:5000` must be
reachable from the laptop (it already is if you open the Jarvis web UI in the laptop's browser).

## The three scripts (setup · run · service)

Each task is its own script, with a Linux/macOS/Pi `.sh` and a Windows `.ps1`:

| Task | Linux / macOS / Pi | Windows |
|---|---|---|
| **Install** deps + download/verify models | `bash setup.sh` (`--with-pose`) | `setup.ps1` (`-WithPose`) |
| **Run once** (foreground test, no service) | `bash run.sh` (`--dry-run`) | `run.ps1` (`--dry-run`) |
| **Make persistent** (autostart) | `bash service.sh install\|uninstall\|status` | `service.ps1 install\|uninstall\|status` |

- **`run`** is just a foreground launch (Ctrl-C to stop) — nothing is installed, so it's the safe way
  to test on a laptop without leaving anything behind.
- **`service`** installs a **least-privilege autostart**: on Linux a **systemd *user* service** (runs
  as you, never root; `loginctl enable-linger` to start before login on a headless Pi); on Windows a
  **Scheduled Task at your logon** (runs as you, *not* elevated, no third-party wrapper, no admin
  needed). Neither opens any listening port — the agent stays outbound-only.

### One command (setup + service)

`install.sh` / `install.ps1` chain *setup* then *service* so it's one step:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1     # Windows  (Linux/Pi: bash install.sh)
```

These are tiny readable wrappers (no opaque binary) — open them to see they only call the other two.

### Or a packaged Windows .exe (built reproducibly in CI)

If you'd rather not run scripts at all, the GitHub Actions workflow **`build-camera-exe`** builds a
single **`jarvis-camera.exe`** on a Windows runner *from this source* and publishes it (with a
SHA-256) as a downloadable artifact — so the binary is **traceable to a commit**, not handed over
out-of-band. Run it from the **Actions** tab (or push a `camera-v*` tag for a Release). Then:

```
jarvis-camera.exe install-service   # downloads+verifies models, writes config\, starts at logon
jarvis-camera.exe verify            # one-shot: who's at the camera
jarvis-camera.exe uninstall-service
```

It's **unsigned**, so Windows SmartScreen shows a one-time "More info → Run anyway" on first launch
(verify the SHA-256 against the workflow output first). Everything lives next to the .exe
(`config\`, `models\`); no admin, no listening port.

## Manage faces (verify · list · add · delete)

Detection (**YuNet**) and identity (**SFace**) both come from `setup`, so there's nothing to
configure — the models are already in `camera/models/`. One small CLI does the lot, from the device
with the camera:

```bash
cd camera                                   # Windows: use  .venv\Scripts\python  below
.venv/bin/python -m jarvis_camera.facecli list                   # who is enrolled
.venv/bin/python -m jarvis_camera.facecli verify --seconds 5     # who is at the camera NOW (local)
.venv/bin/python -m jarvis_camera.facecli add --name "Ravi"      # enroll (~7 frames, averaged)
.venv/bin/python -m jarvis_camera.facecli delete --name "Ravi"   # remove
```

**Two keys, on purpose** (least privilege):

| Command | Key used | Why |
|---|---|---|
| `list`, `verify` | **device** key (`config/agent.key`) | read-only; `verify` is 100% local (sends nothing) |
| `add`, `delete`  | **admin** key (`config/admin.key`) | enrolling/removing faces changes who's authorized |

Mint the **device** key in the **admin → Keys** tab (set a Device ID like `laptop-cam`). The **admin**
key is only needed for the *CLI* `add`/`delete` — put it in `config/admin.key`, and **delete that file
when you're done** so the running device never holds a privileged credential.

## HTTPS / TLS (per-deployment local CA)

The server runs over **HTTPS** using a **local CA that each deployment generates for itself** — so
certs are **never committed** (a cert is unique to one install; another developer running this in
their home makes their own). On the **server**, once: `bash src/scripts/setup_tls.sh` (creates the CA
+ cert, prints the CA fingerprint), then enable the systemd TLS drop-in. The server then publishes its
**public** CA at `https://<server>:5000/ca.crt` (private key never leaves the box).

Each device fetches *that* server's CA and verifies against it:

- **Camera agent:** `bash get-ca.sh` (Windows: `get-ca.ps1`) downloads the CA into `config/ca.crt`
  and prints its SHA-256 — **compare it to the server's `setup_tls.sh` output** before trusting (the
  bootstrap fetch is over an untrusted connection). Config defaults to `server.url: https://…` +
  `ca_cert: config/ca.crt`, so the agent then verifies (MITM-safe; fails closed without the CA).
- **Browser (desktop):** open `https://<server>:5000/ca.crt`, then import it into **Trusted Root
  Certification Authorities** (Windows) / Keychain (macOS) to get a clean padlock.
- **Phone (Android):** open `https://<server>:5000/ca.crt` in the browser to download it, then
  **Settings → Security → Encryption & credentials → Install a certificate → CA certificate**, pick
  the file (Android warns "your network may be monitored" — expected for a private CA). Chrome then
  trusts it for browsing the web UI. **iOS:** open the URL → install the profile → **Settings →
  General → About → Certificate Trust Settings** → enable full trust for the Jarvis CA.

(There's no agent on a phone — it only browses the web UI, so it just needs the browser to trust the
CA.) If you regenerate the CA, every device re-runs `get-ca` / re-imports.

**Enroll from the web UI (no CLI / no admin key on the device):** in **admin → Faces → “Enroll a face
(from a camera)”**, pick the camera + a name and click *Request Enrollment*. A **live preview** (the
camera frame with the detected face boxed) appears so you can see what's being captured. The server
queues a request; the running agent on that device picks it up, captures + embeds on-camera, and
registers it —
so the camera key never gains general enroll rights (it can only fulfill a request an admin created
for it). Each person can hold **multiple embeddings**; enroll again (any angle) to add more, and
**view/delete individual embeddings** or **rename**/link people in the same page. The agent pulls the
enrolled set from `/faces/enrolled` and recognizes locally (best match across a person's embeddings).

## Security & attack surface

This module is built to be hard to abuse even if the laptop/Pi it runs on is compromised:

- **Nothing listens.** The agent is **outbound-only** — it POSTs events to the server and never opens
  a port. There is no inbound network surface to attack on the camera device.
- **No imagery leaves the device** — with one scoped exception: the **live enroll preview**. While an
  admin-initiated enrollment is active, the agent relays annotated frames so the admin can see the
  capture. Those frames are held **in server RAM only** (never written to disk/DB), expire in ~30s,
  and are **admin-only** to view. Normal operation still sends only JSON events.
- **Least-privilege credential.** ⚠️ **Mint the camera's device key under a *non-admin* user.** A
  device-bound key can post events as its device and read the enrolled set — nothing more. (The
  server also *enforces* this: a device-scoped key is denied admin even if it was minted under an
  admin account — defense-in-depth.) So a stolen `agent.key`'s blast radius is bounded.
- **Privileged ops are separate and transient.** `add`/`delete` use a different file (`admin.key`)
  that the always-on agent never loads; remove it after use.
- **Keys are files, never committed.** `config/` is gitignored; the loader warns if a key file is
  group/other-readable (POSIX). On Windows keep it under your user profile.
- **LAN trust today.** Traffic is plaintext HTTP on the LAN (the agent's key + face names are sent in
  clear) — acceptable on a trusted home LAN; move to HTTPS when TLS is enabled on the orchestrator.

## Layout

```
camera/
  README.md            this file
  requirements.txt          Pi-side deps (separate from the server's pyproject)
  requirements-desktop.txt  laptop deps (opencv + numpy + requests; mediapipe only for pose/gestures)
  config.example.json       server URL, camera, per-detector toggles/thresholds
  setup.sh / setup.ps1      install: auto-detect platform, deps + model download (Linux/Pi · Windows)
  run.sh   / run.ps1        run once in the foreground (testing; no service)
  service.sh / service.ps1  make persistent: systemd user service (Linux) · Scheduled Task (Windows)
  install.sh / install.ps1  one command: setup + service (thin readable wrappers)
  set-key.sh / set-key.ps1  write config/agent.key (or admin.key) safely — no quoting pitfalls
  get-ca.sh / get-ca.ps1    download + verify the server's TLS CA into config/ca.crt
  run_exe.py                PyInstaller entry → jarvis-camera.exe (built in CI)
  models/                   YuNet + SFace ONNX (downloaded + sha256-verified by setup; gitignored)
  jarvis_camera/
    capture.py         camera abstraction (picamera2 for CSI, OpenCV for USB)
    paths.py           base dir (camera/ from source, or the .exe's folder when frozen)
    net.py             HTTPS verification helper (verify against config/ca.crt)
    models.py          download + sha256-verify the OpenCV-Zoo models (used by the .exe first run)
    events.py          event client (POST + offline queue + retry)
    keyfile.py         shared API-key loader (device vs admin key separation + perm checks)
    agent.py           main loop + motion-gated scheduler (+ pulls enrolled faces)
    app.py             .exe dispatcher: run / verify / setup / install-service / uninstall-service
    facecli.py         manage faces: list / verify / add / delete (device vs admin key)
    enroll.py          enroll a face → server (capture, average embedding, POST; admin key)
    bench.py           per-detector FPS benchmark (Pi or laptop webcam)
    detectors/
      base.py          Detector interface
      motion.py        MOG2 frame-diff (always available)
      faces.py         YuNet detect + SFace recognize (OpenCV) — landmark-aligned identity
      pose.py          MediaPipe Pose → presence/zone/posture
      gestures.py      MediaPipe Hands → open_palm/fist/thumb_up/down/point
```

Face config (`detectors.faces`): `detector_model` (YuNet) + `embed_model` (SFace) point at the
auto-downloaded ONNX files; `score_threshold` tunes detection, `recognize_threshold` is the SFace
**cosine** match cutoff (default `0.363`, OpenCV's recommended value — raise it to be stricter).
`pose`/`gestures` accept `model_complexity` and need `mediapipe` (`--with-pose` / `-WithPose`).

## Roadmap
1. ✅ **Foundation:** capture · motion · event client · agent loop · setup.
2. ✅ **Server:** `POST /events` (auth via middleware) + `GET /admin/events`. Events stored in
   `vision_events`. (Acting on them — notifications/automation — can hang off this.)
3. ✅ **Faces / pose / gestures:** implemented (faces identity is optional, model-gated). **Now:
   benchmark on the Pi** and tune `interval_s` / which to enable.
4. ✅ **Enrollment + face management:** `jarvis_camera.enroll` (CLI) → server `faces` store; admin
   **Faces** page lists / links face→user / deletes; the agent pulls `/faces/enrolled`. Future:
   in-browser capture enrollment.
5. **Identity → authorization:** link a recognized face's user to the per-user device permissions
   (the "only certain people can control the lights" goal).
6. **Actions:** map gestures → actions — define what "volume" targets (server media, a player,
   or the Pi's own audio) before wiring.
