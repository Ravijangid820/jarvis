#!/usr/bin/env python3
"""Enroll a face: capture a few frames from the camera, average the embedding, register it.

  uv run --no-project python -m jarvis_edge.enroll --name "Ravi" [--frames 7] [--config config/config.json]

MediaPipe finds the face; the **embedding model** (set `detectors.faces.embed_model` to an ONNX
face-embedding model, e.g. MobileFaceNet) turns it into the vector that identifies a person. This
POSTs to the server's `/faces/enroll`, which is **admin-only** — so the configured `api_key_file`
must hold an admin API key. Afterwards the running agent picks it up from `/faces/enrolled`.
"""
import argparse
import json
import logging
import math
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .capture import Camera
from .detectors.faces import FaceDetector

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("edge.enroll")
EDGE_ROOT = Path(__file__).resolve().parents[1]


def _load_key(cfg):
    p = Path(cfg["server"].get("api_key_file", "config/edge.key"))
    if not p.is_absolute():
        p = EDGE_ROOT / p
    return p.read_text().strip() if p.exists() else ""


def _largest(boxes):
    return max(boxes, key=lambda b: b[2] * b[3]) if boxes else None


def run(name, frames, config_path):
    cfg = json.loads(Path(config_path).read_text())
    fd = FaceDetector(cfg.get("detectors", {}).get("faces", {}))
    if not fd.has_identity():
        sys.exit("Enrollment needs the embedding model — set detectors.faces.embed_model (ONNX) in "
                 "your config. (MediaPipe finds the face; the embedding model identifies it.)")

    cam_cfg = cfg.get("camera", {})
    cam = Camera(backend=cam_cfg.get("backend", "auto"), device=cam_cfg.get("device", 0),
                 width=cam_cfg.get("width", 480), height=cam_cfg.get("height", 360), fps=cam_cfg.get("fps", 8))
    cam.open()
    log.info("Look at the camera — capturing %d good frames of '%s'...", frames, name)
    vecs, tries = [], 0
    while len(vecs) < frames and tries < frames * 15:
        tries += 1
        frame = cam.read()
        if frame is None:
            time.sleep(0.05); continue
        box = _largest(fd._detect(frame))
        if not box:
            continue
        x, y, w, h = box
        v = fd.embed(frame[y:y + h, x:x + w])
        if v:
            vecs.append(v)
            log.info("  captured %d/%d", len(vecs), frames)
        time.sleep(0.25)
    cam.close()
    if len(vecs) < max(1, frames // 2):
        sys.exit(f"Only captured {len(vecs)} face(s) — try again with better lighting / framing.")

    dim = len(vecs[0])
    avg = [sum(v[i] for v in vecs) / len(vecs) for i in range(dim)]
    norm = math.sqrt(sum(a * a for a in avg)) or 1.0
    avg = [a / norm for a in avg]

    key = _load_key(cfg)
    if not key:
        sys.exit("No API key — put an ADMIN key in the configured api_key_file to enroll.")
    url = cfg["server"]["url"].rstrip("/") + "/faces/enroll"
    body = json.dumps({"name": name, "embedding": avg}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            json.loads(r.read().decode())
        log.info("✓ enrolled '%s' (averaged %d frames). Manage / link to a user in the admin Faces page.", name, len(vecs))
    except urllib.error.HTTPError as e:
        sys.exit(f"server HTTP {e.code} — /faces/enroll is admin-only; is the key an admin key?")
    except Exception as e:
        sys.exit(f"enroll POST failed: {e}")


def main():
    ap = argparse.ArgumentParser(description="Enroll a face for Jarvis recognition")
    ap.add_argument("--name", required=True, help="person's name (display label)")
    ap.add_argument("--frames", type=int, default=7, help="good frames to average (default 7)")
    ap.add_argument("--config", default=str(EDGE_ROOT / "config" / "config.json"))
    args = ap.parse_args()
    run(args.name, args.frames, args.config)


if __name__ == "__main__":
    main()
