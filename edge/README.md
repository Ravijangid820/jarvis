# Jarvis Edge (Raspberry Pi vision agent)

An on-device agent that watches a camera and sends **high-level events** (not video) to the
Jarvis orchestrator. All recognition runs **on the Pi** so the server isn't loaded, and no
imagery ever leaves the device — only small JSON events over the LAN.

> Status: **scaffold, untested on hardware.** Written ahead of having the Pi connected. The
> foundation (capture, motion, event client, agent loop) is standard; the heavy detectors
> (faces/pose/gestures) are stubs to implement + tune **on the actual Pi**.

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

The server `/events` endpoint is **not built yet** — see the roadmap. Until then the agent can
run with `--dry-run` (logs events instead of POSTing).

## Setup (on the Pi)

```bash
bash edge/setup.sh          # 64-bit check, apt deps, venv, pip install, config
# then on the SERVER, mint a key for this device and copy it to the Pi:
#   uv run python src/scripts/manage.py mint-key <user> pi-vision   →  edge/config/edge.key
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
    detectors/
      base.py          Detector interface
      motion.py        ✅ implemented (frame-diff)
      faces.py         ⤵ stub — detect + identify (triggered)
      pose.py          ⤵ stub — MediaPipe Pose
      gestures.py      ⤵ stub — MediaPipe Hands
```

## Roadmap
1. **Foundation (this scaffold):** capture · motion · event client · agent loop · setup.
2. **Server:** add `POST /events` to the orchestrator (auth via the existing middleware) + a
   place to store/route events. (Contract above.)
3. **Faces:** detection (OpenCV DNN) + identity (small ONNX embedding); enroll known faces.
4. **Pose/gestures:** MediaPipe, motion-gated, off by default — tune FPS/res on the Pi.
5. Map gestures → actions (define what "volume" targets: server media, a player, or Pi audio).
