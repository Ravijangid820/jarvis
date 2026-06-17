"""Benchmark each detector's throughput — run ON THE PI to check what it can handle.

  python -m jarvis_edge.bench [--config config/config.json] [--frames 60]

Reports achieved FPS + mean latency per detector (motion, faces, pose, gestures) using the
config's camera/detector settings, so you can see e.g. "pose: 2.3 FPS" before deciding what to
enable. A detector that can't construct (e.g. mediapipe not installed) is reported as unavailable.
"""
import argparse
import json
import logging
import time
from pathlib import Path

from .capture import Camera
from .detectors.faces import FaceDetector
from .detectors.gestures import GestureDetector
from .detectors.motion import MotionDetector
from .detectors.pose import PoseDetector

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
EDGE_ROOT = Path(__file__).resolve().parents[1]
DETECTORS = [("motion", MotionDetector), ("faces", FaceDetector),
             ("pose", PoseDetector), ("gestures", GestureDetector)]


def _bench(name, det, cam, frames):
    warm = cam.read()
    if warm is not None:
        try:
            det.process(warm)
        except Exception as e:
            print(f"  {name:9s}: unavailable ({e})")
            return
    times, n = [], 0
    while n < frames:
        f = cam.read()
        if f is None:
            continue
        t0 = time.time()
        try:
            det.process(f)
        except Exception as e:
            print(f"  {name:9s}: error during run ({e})")
            return
        times.append(time.time() - t0)
        n += 1
    mean = sum(times) / len(times)
    print(f"  {name:9s}: {1 / mean:5.1f} FPS   ({mean * 1000:6.1f} ms/frame)")


def main():
    ap = argparse.ArgumentParser(description="Benchmark edge detectors on this device")
    ap.add_argument("--config", default=str(EDGE_ROOT / "config" / "config.json"))
    ap.add_argument("--frames", type=int, default=60)
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text())
    cam_cfg, det_cfg = cfg.get("camera", {}), cfg.get("detectors", {})
    cam = Camera(backend=cam_cfg.get("backend", "auto"), device=cam_cfg.get("device", 0),
                 width=cam_cfg.get("width", 480), height=cam_cfg.get("height", 360),
                 fps=cam_cfg.get("fps", 8))
    cam.open()
    print(f"Benchmarking {args.frames} frames @ {cam_cfg.get('width', 480)}x{cam_cfg.get('height', 360)}:")
    try:
        for name, cls in DETECTORS:
            det = cls(det_cfg.get(name, {}))
            _bench(name, det, cam, args.frames)
            det.close()
    finally:
        cam.close()


if __name__ == "__main__":
    main()
