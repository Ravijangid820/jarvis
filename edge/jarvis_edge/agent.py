"""Jarvis edge agent: capture → motion gate → one heavy detector per cycle → POST events.

The Pi 3 B+ can't run pose + gestures + faces concurrently in real time, so the loop runs the
cheap motion detector every frame and, only while there's motion, escalates to *one* enabled
heavy detector per cycle (round-robin, each throttled by its own interval_s). On faster hardware
just raise fps / enable more detectors — same code.
"""
import argparse
import json
import logging
import signal
import time
import urllib.request
from pathlib import Path

from .capture import Camera
from .detectors.faces import FaceDetector
from .detectors.gestures import GestureDetector
from .detectors.motion import MotionDetector
from .detectors.pose import PoseDetector
from .events import EventClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("edge.agent")

EDGE_ROOT = Path(__file__).resolve().parents[1]
HEAVY = {"faces": FaceDetector, "pose": PoseDetector, "gestures": GestureDetector}


def _load_key(cfg):
    p = Path(cfg["server"].get("api_key_file", "config/edge.key"))
    if not p.is_absolute():
        p = EDGE_ROOT / p
    if not p.exists():
        return ""
    if p.stat().st_mode & 0o077:
        log.warning("%s is group/other-readable — run: chmod 600 %s", p, p)
    return p.read_text().strip()


def _fetch_enrolled(server, key):
    """Pull the centrally-managed enrolled faces ({name: embedding}) for local recognition."""
    req = urllib.request.Request(server.rstrip("/") + "/faces/enrolled",
                                 headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode()).get("enrolled", {})
    except Exception as e:
        log.warning("could not fetch enrolled faces: %s", e)
        return {}


def _build_heavy(det_cfg):
    out = []
    for name, cls in HEAVY.items():
        c = det_cfg.get(name, {})
        if c.get("enabled"):
            out.append(cls(c))
            log.info("enabled heavy detector: %s (interval %ss)", name, c.get("interval_s"))
    return out


def run(config_path, dry_run=False):
    cfg = json.loads(Path(config_path).read_text())
    cam_cfg, det_cfg = cfg.get("camera", {}), cfg.get("detectors", {})

    key = _load_key(cfg)
    if not key and not dry_run:
        log.warning("no API key file found — running as --dry-run (events logged, not sent)")
        dry_run = True

    client = EventClient(cfg["server"]["url"], key, cfg.get("device_id", "pi"),
                         endpoint=cfg["server"].get("events_endpoint", "/events"), dry_run=dry_run)
    client.start()

    cam = Camera(backend=cam_cfg.get("backend", "auto"), device=cam_cfg.get("device", 0),
                 width=cam_cfg.get("width", 480), height=cam_cfg.get("height", 360),
                 fps=cam_cfg.get("fps", 8))
    cam.open()

    motion = MotionDetector(det_cfg.get("motion", {})) if det_cfg.get("motion", {}).get("enabled", True) else None
    heavy = _build_heavy(det_cfg)
    # Recognition matches against centrally-managed identities — pull them from the server.
    fdet = next((d for d in heavy if d.name == "faces"), None)
    if fdet is not None and key:
        enrolled = _fetch_enrolled(cfg["server"]["url"], key)
        fdet.set_known(enrolled)
        log.info("loaded %d enrolled face(s) from server", len(enrolled))
    last_run = {d.name: 0.0 for d in heavy}
    rr = 0
    last_hb = 0.0
    HB_INTERVAL = 30.0    # liveness ping so the server shows this device active even in a quiet room

    state = {"go": True}
    # Register stop signals defensively — not every signal is settable on every platform
    # (e.g. SIGTERM handling differs on Windows, where we test on a laptop webcam).
    for _sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if _sig is not None:
            try:
                signal.signal(_sig, lambda *_: state.update(go=False))
            except (ValueError, OSError, RuntimeError):
                pass

    period = 1.0 / max(cam_cfg.get("fps", 8), 1)
    log.info("agent started (dry_run=%s) — Ctrl-C to stop", dry_run)
    try:
        while state["go"]:
            t0 = time.time()
            if t0 - last_hb >= HB_INTERVAL:        # prove liveness regardless of motion
                client.send("heartbeat")
                last_hb = t0
            frame = cam.read()
            if frame is None:
                time.sleep(0.05)
                continue
            events, moving = [], True
            if motion:
                events += motion.process(frame)
                moving = motion.moving
            if moving and heavy:                      # escalate to ONE ready heavy detector
                n = len(heavy)
                for i in range(n):
                    d = heavy[(rr + i) % n]
                    if time.time() - last_run[d.name] >= d.interval_s:
                        try:
                            events += d.process(frame) or []
                        except Exception as e:
                            log.exception("detector %s failed: %s", d.name, e)
                        last_run[d.name] = time.time()
                        rr = (rr + i + 1) % n
                        break
            for e in events:
                client.send(e["type"], e.get("data"))
            dt = time.time() - t0
            if dt < period:
                time.sleep(period - dt)
    finally:
        log.info("shutting down")
        cam.close()
        for d in heavy:
            d.close()
        if motion:
            motion.close()
        client.stop()


def main():
    ap = argparse.ArgumentParser(description="Jarvis edge vision agent")
    ap.add_argument("--config", default=str(EDGE_ROOT / "config" / "config.json"))
    ap.add_argument("--dry-run", action="store_true", help="log events instead of POSTing")
    args = ap.parse_args()
    run(args.config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
