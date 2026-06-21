"""Jarvis camera agent: capture → motion gate → one heavy detector per cycle → POST events.

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
from urllib.parse import urlsplit, quote
from pathlib import Path

from .capture import Camera
from .detectors.faces import FaceDetector
from .detectors.gestures import GestureDetector
from .detectors.motion import MotionDetector
from .detectors.pose import PoseDetector
from . import net
from .enroll import capture_average
from .events import EventClient
from .keyfile import load_key
from .paths import base_dir

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("camera.agent")

CAMERA_ROOT = base_dir()
HEAVY = {"faces": FaceDetector, "pose": PoseDetector, "gestures": GestureDetector}
_MAX_ENROLLED_BYTES = 16 * 1024 * 1024   # ample for names + 128-float embeddings; caps OOM from a bad server


def _load_key(cfg):
    # The always-on agent uses ONLY the low-privilege, device-bound key (never the admin key).
    return load_key(cfg["server"].get("api_key_file", "config/agent.key"), CAMERA_ROOT)


def _fetch_enrolled(server, key, ctx=None):
    """Pull the centrally-managed enrolled faces ({name: [emb,...]}) for local recognition.
    Returns the dict on success ({} if genuinely none), or None on error — so a transient failure
    during a periodic refresh doesn't wipe the faces we already know."""
    req = urllib.request.Request(server.rstrip("/") + "/faces/enrolled",
                                 headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            data = r.read(_MAX_ENROLLED_BYTES + 1)        # bound the read (don't trust the server's size)
            if len(data) > _MAX_ENROLLED_BYTES:
                log.warning("enrolled response too large (>%d bytes) — ignoring", _MAX_ENROLLED_BYTES)
                return None
            return json.loads(data.decode()).get("enrolled", {})
    except Exception as e:
        log.warning("could not fetch enrolled faces: %s", e)
        return None


def _poll_enroll(server, key, ctx=None):
    """Check for a pending enroll request for THIS device (server binds it to our key)."""
    req = urllib.request.Request(server.rstrip("/") + "/faces/enroll-request",
                                 headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
        return json.loads(r.read(_MAX_ENROLLED_BYTES + 1).decode()).get("request")


def _submit_enroll(server, key, request_id, embedding=None, error=None, ctx=None):
    body = json.dumps({"request_id": request_id, "embedding": embedding, "error": error}).encode("utf-8")
    req = urllib.request.Request(server.rstrip("/") + "/faces/enroll-result", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
        r.read(4096)


def _preview_uploader(server, key, request_id, ctx=None):
    """Return an on_frame(frame, row, captured, total) callback that relays annotated JPEG frames to
    the server so the admin UI shows a smooth (~10 fps) live view of what the camera sees during
    enrollment. Frames go over ONE kept-alive connection — at 10 fps a fresh TLS handshake per frame
    would needlessly load the box. Call .close() when the capture is done to release it."""
    import base64
    import http.client
    u = urlsplit(server)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    state = {"last": 0.0, "conn": None}

    def _connect():
        if u.scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=3, context=ctx)
        return http.client.HTTPConnection(host, port, timeout=3)

    def on_frame(frame, row, captured, total):
        now = time.time()
        if now - state["last"] < 0.1:           # ~10 fps — smooth without flooding the link
            return
        state["last"] = now
        try:
            import cv2
            f = frame
            if row is not None:
                x, y, w, h = (int(v) for v in row[:4])
                f = frame.copy()
                cv2.rectangle(f, (x, y), (x + w, y + h), (0, 255, 0), 2)
            ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 55])
            if not ok:
                return
            img = base64.b64encode(buf.tobytes()).decode("ascii")
            body = json.dumps({"request_id": request_id, "image": img,
                               "captured": captured, "total": total}).encode("utf-8")
            headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
            for attempt in (1, 2):              # reconnect once if the kept connection went stale
                try:
                    if state["conn"] is None:
                        state["conn"] = _connect()
                    state["conn"].request("POST", "/faces/enroll-preview", body=body, headers=headers)
                    state["conn"].getresponse().read()
                    break
                except Exception:
                    if state["conn"] is not None:
                        try: state["conn"].close()
                        except Exception: pass
                    state["conn"] = None
                    if attempt == 2:
                        raise
        except Exception as e:
            log.debug("preview upload failed: %s", e)

    def close():
        if state["conn"] is not None:
            try: state["conn"].close()
            except Exception: pass
            state["conn"] = None

    on_frame.close = close
    return on_frame


def _maybe_enroll(server, key, cam, fdet, frames=7, ctx=None):
    """If an admin queued an enroll request for this device, capture on-camera + submit the embedding."""
    try:
        reqd = _poll_enroll(server, key, ctx)
    except Exception as e:
        log.debug("enroll poll failed: %s", e)
        return
    if not reqd:
        return
    rid, name = reqd.get("id"), reqd.get("name")
    log.info("enroll request #%s: capturing '%s' — look at the camera", rid, name)
    uploader = _preview_uploader(server, key, rid, ctx)
    try:
        vec = capture_average(cam, fdet, frames, log_progress=False, on_frame=uploader)
    except Exception as e:
        vec = None
        log.warning("enroll capture failed: %s", e)
    finally:
        uploader.close()
    try:
        if vec:
            _submit_enroll(server, key, rid, embedding=vec, ctx=ctx)
            log.info("✓ enrolled '%s' from this camera", name)
        else:
            _submit_enroll(server, key, rid, error="no clear face captured", ctx=ctx)
            log.warning("enroll '%s' failed: no clear face", name)
    except Exception as e:
        log.warning("enroll submit failed: %s", e)


def _poll_commands(server, key, device, ctx=None):
    """Pull pending device commands (wait=0 → immediate) — e.g. an admin/voice 'enter gesture mode'.
    Returns a list of {action, params}."""
    url = server.rstrip("/") + "/devices/commands?device=" + quote(str(device), safe="") + "&wait=0"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
            return json.loads(r.read(_MAX_ENROLLED_BYTES).decode()).get("commands", [])
    except Exception as e:
        log.debug("command poll failed: %s", e)
        return []


def _run_gesture_volume(server, key, cam, gdet, ctx, ttl):
    """Gesture-volume control: while the server's mode is live, track the hand's height and report it
    (~8/sec over one kept-alive connection). The SERVER maps movement → volume; we just report. Ends
    on a fist, when the server says the mode ended, or a local safety timeout."""
    if gdet is None or not gdet.available():
        log.warning("gesture volume requested but hand tracking is unavailable (mediapipe missing)")
        return
    import http.client
    u = urlsplit(server)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    state = {"conn": None}

    def _post_y(y):
        body = json.dumps({"y": round(float(y), 4)}).encode("utf-8")
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
        for attempt in (1, 2):
            try:
                if state["conn"] is None:
                    state["conn"] = (http.client.HTTPSConnection(host, port, timeout=4, context=ctx)
                                     if u.scheme == "https" else http.client.HTTPConnection(host, port, timeout=4))
                state["conn"].request("POST", "/devices/gesture", body=body, headers=headers)
                return json.loads(state["conn"].getresponse().read().decode()).get("active", False)
            except Exception:
                if state["conn"] is not None:
                    try: state["conn"].close()
                    except Exception: pass
                state["conn"] = None
                if attempt == 2:
                    raise

    log.info("gesture volume: ON (~%ss) — raise/lower your hand; make a fist to stop", ttl)
    deadline = time.time() + ttl + 5            # local safety past the server's TTL
    last_post = 0.0
    try:
        while time.time() < deadline:
            frame = cam.read()
            if frame is None:
                time.sleep(0.03); continue
            y, gesture = gdet.hand_state(frame)
            if gesture == "fist":
                log.info("gesture volume: stop (fist)")
                break
            if y is not None and time.time() - last_post >= 0.12:    # ~8 reports/sec
                last_post = time.time()
                try:
                    if not _post_y(y):
                        log.info("gesture volume: mode ended")
                        break
                except Exception as e:
                    log.debug("gesture post failed: %s", e)
            time.sleep(0.02)
    finally:
        if state["conn"] is not None:
            try: state["conn"].close()
            except Exception: pass
        log.info("gesture volume: OFF")


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
    url = str(cfg["server"]["url"])
    if key and url.lower().startswith("http://"):
        log.warning("server.url is http:// — the API key is sent in cleartext on the LAN. "
                    "Use https:// (TLS) once the orchestrator has it.")
    ctx = net.ssl_context(cfg)        # verify against the configured CA on https (else default/no-op)
    if url.lower().startswith("https://") and ctx is None:
        log.warning("https without server.ca_cert — verifying against system CAs (a local CA won't be "
                    "trusted). Set server.ca_cert to config/ca.crt.")

    client = EventClient(url, key, cfg.get("device_id", "pi"),
                         endpoint=cfg["server"].get("events_endpoint", "/events"),
                         dry_run=dry_run, verify=net.verify_arg(cfg))
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
        enrolled = _fetch_enrolled(url, key, ctx)
        fdet.set_known(enrolled or {})
        log.info("loaded %d enrolled face(s) from server", len(enrolled or {}))
    last_run = {d.name: 0.0 for d in heavy}
    rr = 0
    last_hb = 0.0
    HB_INTERVAL = 30.0    # liveness ping so the server shows this device active even in a quiet room
    last_enroll = 0.0
    ENROLL_INTERVAL = 4.0  # how often to check for an admin-queued "enroll this face" request
    last_known = time.time()
    KNOWN_REFRESH = 60.0   # re-pull enrolled faces so new enrollments are recognized without a restart
    can_enroll = bool(key) and fdet is not None and fdet.has_identity() and not dry_run
    device_id = cfg.get("device_id", "pi")
    gdet_volume = next((d for d in heavy if d.name == "gestures"), None)   # reuse if enabled
    last_cmd = 0.0
    CMD_INTERVAL = 2.0     # how often to check for pull-commands (e.g. "enter gesture volume mode")

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
            if can_enroll and t0 - last_enroll >= ENROLL_INTERVAL:   # admin-queued enroll-from-UI
                last_enroll = t0
                _maybe_enroll(url, key, cam, fdet, ctx=ctx)
            if fdet is not None and key and t0 - last_known >= KNOWN_REFRESH:   # pick up new enrollments
                last_known = t0
                enr = _fetch_enrolled(url, key, ctx)
                if enr is not None:                  # None = fetch failed → keep what we have
                    fdet.set_known(enr)
            if key and not dry_run and t0 - last_cmd >= CMD_INTERVAL:   # voice-triggered modes
                last_cmd = t0
                for cmd in _poll_commands(url, key, device_id, ctx):
                    if cmd.get("action") == "gesture_mode":
                        if gdet_volume is None:                        # lazily start hand tracking
                            gdet_volume = GestureDetector({"model_complexity": 0})
                        ttl = int((cmd.get("params") or {}).get("ttl", 12))
                        _run_gesture_volume(url, key, cam, gdet_volume, ctx, ttl)
                        last_hb = time.time()                          # we were busy; reset cadence
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
    ap = argparse.ArgumentParser(description="Jarvis camera vision agent")
    ap.add_argument("--config", default=str(CAMERA_ROOT / "config" / "config.json"))
    ap.add_argument("--dry-run", action="store_true", help="log events instead of POSTing")
    args = ap.parse_args()
    run(args.config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
