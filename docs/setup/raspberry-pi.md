# Jarvis Edge (Raspberry Pi vision agent)

An on-device agent that watches a camera and sends **high-level events** (not video) to the
Jarvis orchestrator. All recognition runs **on the Pi** so the server isn't loaded, and no
imagery ever leaves the device — only small JSON events over the LAN.

> **Code:** [`edge/`](../../edge/) in the repo. Run the commands below **on the Pi, from the `edge/` directory.**

> Status: **scaffold, untested on hardware.** Written ahead of having the Pi connected. The
> foundation (capture, motion, event client, agent loop) is standard. The heavy detectors
> (faces/pose/gestures) are now **implemented** but unrun on this hardware — use `bench.py`
> (below) on the Pi to measure real FPS and decide what to enable.

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

`POST {server.url}{server.events_endpoint}` with `Authorization: Bearer <edge key>`:

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
#   .venv/bin/pip install mediapipe onnxruntime
.venv/bin/python -m jarvis_edge.bench --frames 60
#   motion   :  28.0 FPS  (  35.7 ms/frame)
#   faces    :   6.5 FPS  ( 153.0 ms/frame)
#   pose     :   2.3 FPS  ( 435.0 ms/frame)   ← decide if this is usable for you
#   gestures :   3.1 FPS  ( 322.0 ms/frame)
```

Use the numbers to set each detector's `interval_s` (and whether to enable it) in config.

## Setup (on the Pi)

```bash
bash edge/setup.sh          # 64-bit check, apt deps, venv, pip install, config
# then on the SERVER, mint a key for this device and copy it to the Pi:
#   uv run python src/scripts/manage.py mint-key <user> pi-vision <device_id>  →  edge/config/edge.key
#   (the last arg binds the key to that device — it may then only post events as that device)
cp edge/config.example.json edge/config/config.json   # review server.url / camera / detectors
python -m jarvis_edge.agent --config edge/config/config.json        # or --dry-run
```

## Layout

```
edge/
  README.md            this file
  requirements.txt     Pi-side deps (separate from the server's pyproject)
  config.example.json  server URL, camera, per-detector toggles/thresholds
  setup.sh             Pi bootstrap (64-bit check, apt + venv + pip)
  jarvis_edge/
    capture.py         camera abstraction (picamera2 for CSI, OpenCV for USB)
    events.py          event client (POST + offline queue + retry)
    agent.py           main loop + motion-gated scheduler
    bench.py           per-detector FPS benchmark (run on the Pi)
    detectors/
      base.py          Detector interface
      motion.py        MOG2 frame-diff (always available)
      faces.py         OpenCV detect (Haar/DNN) + optional ONNX identity
      pose.py          MediaPipe Pose → presence/zone/posture
      gestures.py      MediaPipe Hands → open_palm/fist/thumb_up/down/point
```

Optional model config (in each detector's config block): `faces` accepts `detector_proto` +
`detector_model` (res10 DNN, else Haar), and `embed_model` (ONNX) + `enrolled_file` (JSON of
`{name: [embedding]}`) to turn on identity. `pose`/`gestures` accept `model_complexity`.

## Roadmap
1. ✅ **Foundation:** capture · motion · event client · agent loop · setup.
2. ✅ **Server:** `POST /events` (auth via middleware) + `GET /admin/events`. Events stored in
   `vision_events`. (Acting on them — notifications/automation — can hang off this.)
3. ✅ **Faces / pose / gestures:** implemented (faces identity is optional, model-gated). **Now:
   benchmark on the Pi** and tune `interval_s` / which to enable.
4. **Enrollment:** add a helper to build `enrolled_file` embeddings from known-face images.
5. **Actions:** map gestures → actions — define what "volume" targets (server media, a player,
   or the Pi's own audio) before wiring.
