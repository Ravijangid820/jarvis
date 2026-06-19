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
# install mediapipe (pose/gestures) into the venv first if you want to bench them:
#   uv pip install --python .venv/bin/python mediapipe onnxruntime
cd camera && .venv/bin/python -m jarvis_camera.bench --frames 60
#   motion   :  28.0 FPS  (  35.7 ms/frame)
#   faces    :   6.5 FPS  ( 153.0 ms/frame)
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
isn't present, so you can test the whole pipeline before you have the Pi. Everything stays in a
**uv-managed venv** (`camera/.venv`) + a **uv-managed Python** — nothing is installed globally.

> MediaPipe (faces/pose/gestures) has **no Python 3.13 wheels yet**, so pin **3.12** for the venv
> (`uv venv --python 3.12`). uv downloads a managed CPython 3.12 into its own cache — not a system
> Python. Motion-only (opencv) works on any version.

### Windows (PowerShell) — one script

```powershell
# 0. Install uv once if you don't have it (user-level binary, not a global Python package):
#    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
# 1. Get the code (small; models/builds are gitignored):
git clone <repo> jarvis ; cd jarvis\camera
# 2. Bootstrap the sandbox (add -WithFaces to also install mediapipe + onnxruntime):
powershell -ExecutionPolicy Bypass -File setup.ps1 -WithFaces
# 3. Edit config\config.json: device_id="laptop-cam", server.url="http://192.168.0.101:5000",
#    camera.backend="auto"; for faces set detectors.faces.enabled=true.
# 4. Test with NO server first (run via the venv's python — fully sandboxed):
.venv\Scripts\python -m jarvis_camera.bench --frames 60
.venv\Scripts\python -m jarvis_camera.agent --dry-run
# 5. Go live: on the SERVER mint a device-bound key → save to camera\config\agent.key, then:
.venv\Scripts\python -m jarvis_camera.agent      # events POST to /events → admin shows the camera green
```

### macOS / Linux

```bash
# 1. Get the code (or sparse-checkout just camera/):
git clone <repo> jarvis && cd jarvis/camera
# 2. Sandbox: Python 3.12 venv + desktop deps (add mediapipe/onnxruntime for faces/pose):
uv venv --python 3.12
uv pip install --python .venv/bin/python -r requirements-desktop.txt
uv pip install --python .venv/bin/python "mediapipe>=0.10,<0.11" "onnxruntime>=1.17,<2"   # optional: faces
# 3. Config (you're in camera/):
cp config.example.json config/config.json
#    device_id="laptop-cam", server.url="http://192.168.0.101:5000", camera.backend="auto"
# 4. Test with NO server first — ALWAYS run via the venv's python (not `uv run`):
.venv/bin/python -m jarvis_camera.bench --frames 60
.venv/bin/python -m jarvis_camera.agent --dry-run
# 5. Go live: on the SERVER mint a device-bound key, save to camera/config/agent.key, then:
.venv/bin/python -m jarvis_camera.agent
```

> Mint the device key **on the server**: `uv run python src/scripts/manage.py mint-key <user> laptop-cam laptop-cam`
> (the last arg binds the key to `laptop-cam` so it can only post as that device). For **face
> enrollment** you instead need an **admin** key (`/faces/enroll` is admin-only) — see below.

**Windows gotchas:** allow camera access (Settings → Privacy & security → Camera → *Let desktop apps
access your camera*); close Teams/Zoom/etc. so the webcam is free; the server's `:5000` must be
reachable from the laptop (it already is if you open the Jarvis web UI in the laptop's browser).

## Enroll a face (identity = who, not just where)

MediaPipe finds the face; **identity** needs an embedding model — point
`detectors.faces.embed_model` at an ONNX face-embedding model (e.g. MobileFaceNet) in your config,
and use an **admin** API key (`/faces/enroll` is admin-only). Then, on the device with the camera:

```bash
cd camera
.venv/bin/python -m jarvis_camera.enroll --name "Ravi"            # ~7 frames, averaged, registered
#  Windows:  .venv\Scripts\python -m jarvis_camera.enroll --name "Ravi"
```

The `api_key_file` in your config must hold an **admin** key for enrollment. For a first test you can
point a second config (or temporarily swap `agent.key`) to an admin key just to enroll, then put the
device-bound key back for normal running.

Then **manage / link** faces in the **admin → Faces** page (linking a face to a user account is what
gates device actions by who's present). The running agent pulls the enrolled set from
`/faces/enrolled` at startup, and recognition happens locally.

## Layout

```
camera/
  README.md            this file
  requirements.txt          Pi-side deps (separate from the server's pyproject)
  requirements-desktop.txt  laptop deps (opencv + numpy + requests; mediapipe/onnxruntime optional)
  config.example.json       server URL, camera, per-detector toggles/thresholds
  setup.sh                  Pi / Linux / macOS bootstrap (uv venv + uv pip)
  setup.ps1                 Windows laptop bootstrap (uv venv 3.12 + uv pip; -WithFaces)
  jarvis_camera/
    capture.py         camera abstraction (picamera2 for CSI, OpenCV for USB)
    events.py          event client (POST + offline queue + retry)
    agent.py           main loop + motion-gated scheduler (+ pulls enrolled faces)
    enroll.py          enroll a face → server (capture, average embedding, POST)
    bench.py           per-detector FPS benchmark (Pi or laptop webcam)
    detectors/
      base.py          Detector interface
      motion.py        MOG2 frame-diff (always available)
      faces.py         MediaPipe BlazeFace (Haar/DNN fallback) + optional ONNX identity
      pose.py          MediaPipe Pose → presence/zone/posture
      gestures.py      MediaPipe Hands → open_palm/fist/thumb_up/down/point
```

Optional model config (in each detector's config block): `faces` detection prefers **MediaPipe**
(install it), with `detector_proto` + `detector_model` (res10 DNN) or Haar as fallbacks, and
`min_confidence` to tune it; `embed_model` (ONNX) + `enrolled_file` (JSON of `{name: [embedding]}`)
turn on **identity**. `pose`/`gestures` accept `model_complexity`.

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
