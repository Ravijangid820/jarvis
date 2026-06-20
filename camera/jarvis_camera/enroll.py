#!/usr/bin/env python3
"""Enroll a face: capture a few frames from the camera, average the embedding, register it.

  .venv/bin/python -m jarvis_camera.enroll --name "Ravi" [--frames 7] [--config config/config.json]
  (Windows:  .venv\\Scripts\\python -m jarvis_camera.enroll --name "Ravi")

YuNet finds + aligns the face; SFace turns it into the embedding that identifies a person (both
models are downloaded by setup). This POSTs to the server's `/faces/enroll`, which is **admin-only**
— so it uses the **admin** key
(`admin_key_file`, default `config/admin.key`), NOT the agent's device key. Keep that admin key off
the device except while enrolling. Afterwards the running agent picks the face up from
`/faces/enrolled`.
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
from .keyfile import load_key
from .paths import base_dir
from . import net

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("camera.enroll")
CAMERA_ROOT = base_dir()


def _load_admin_key(cfg):
    # Enrollment is privileged → the ADMIN key, deliberately a different file from the device key.
    return load_key(cfg["server"].get("admin_key_file", "config/admin.key"), CAMERA_ROOT)


def _largest(rows):
    return max(rows, key=lambda r: r[2] * r[3]) if rows else None


def capture_average(cam, fd, frames, log_progress=True, on_frame=None):
    """Capture `frames` good face embeddings from an OPEN camera and return their L2-normalized
    average, or None if too few faces were seen. Shared by the CLI and the agent's enroll handler.
    `on_frame(frame, row, captured, total)` is called for every read frame (for the live preview)."""
    vecs, tries = [], 0
    while len(vecs) < frames and tries < frames * 15:
        tries += 1
        frame = cam.read()
        if frame is None:
            time.sleep(0.05); continue
        row = _largest(fd.detect(frame))
        if on_frame is not None:
            try:
                on_frame(frame, row, len(vecs), frames)
            except Exception:
                pass
        if row is None:
            continue
        v = fd.embed(frame, row)            # SFace aligns via YuNet's landmarks, then embeds
        if v:
            vecs.append(v)
            if log_progress:
                log.info("  captured %d/%d", len(vecs), frames)
        time.sleep(0.25)
    if len(vecs) < max(1, frames // 2):
        return None
    dim = len(vecs[0])
    avg = [sum(v[i] for v in vecs) / len(vecs) for i in range(dim)]
    norm = math.sqrt(sum(a * a for a in avg)) or 1.0
    return [a / norm for a in avg]


def run(name, frames, config_path, replace=False):
    cfg = json.loads(Path(config_path).read_text())
    fd = FaceDetector(cfg.get("detectors", {}).get("faces", {}))
    if not fd.has_identity():
        sys.exit("Enrollment needs the SFace embedding model — run setup to download it, or set "
                 "detectors.faces.embed_model. (YuNet finds the face; SFace identifies it.)")

    cam_cfg = cfg.get("camera", {})
    cam = Camera(backend=cam_cfg.get("backend", "auto"), device=cam_cfg.get("device", 0),
                 width=cam_cfg.get("width", 480), height=cam_cfg.get("height", 360), fps=cam_cfg.get("fps", 8))
    cam.open()
    log.info("Look at the camera — capturing %d good frames of '%s'...", frames, name)
    avg = capture_average(cam, fd, frames)
    cam.close()
    if avg is None:
        sys.exit("Couldn't capture enough clear faces — try again with better lighting / framing.")

    key = _load_admin_key(cfg)
    if not key:
        sys.exit("No admin key — put an ADMIN API key in config/admin.key (admin_key_file) to enroll. "
                 "(The device's agent.key can't enroll — enrollment is admin-only by design.)")
    url = cfg["server"]["url"].rstrip("/") + "/faces/enroll"
    body = json.dumps({"name": name, "embedding": avg, "replace": bool(replace),
                       "source": cfg.get("device_id")}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=30, context=net.ssl_context(cfg)) as r:
            json.loads(r.read().decode())
        verb = "replaced — now 1 embedding for" if replace else "added an embedding for"
        log.info("✓ %s '%s' (averaged %d frames). Manage in the admin Faces page.", verb, name, len(vecs))
    except urllib.error.HTTPError as e:
        sys.exit(f"server HTTP {e.code} — /faces/enroll is admin-only; is the key an admin key?")
    except Exception as e:
        sys.exit(f"enroll POST failed: {e}")


def main():
    ap = argparse.ArgumentParser(description="Enroll a face for Jarvis recognition")
    ap.add_argument("--name", required=True, help="person's name (display label)")
    ap.add_argument("--frames", type=int, default=7, help="good frames to average (default 7)")
    ap.add_argument("--replace", action="store_true", help="replace this person's embeddings instead of adding")
    ap.add_argument("--config", default=str(CAMERA_ROOT / "config" / "config.json"))
    args = ap.parse_args()
    run(args.name, args.frames, args.config, replace=args.replace)


if __name__ == "__main__":
    main()
