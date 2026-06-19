#!/usr/bin/env python3
"""Manage faces from the device that has the camera — one small CLI (Windows-friendly).

  .venv\\Scripts\\python -m jarvis_camera.facecli list                  # who is enrolled
  .venv\\Scripts\\python -m jarvis_camera.facecli verify [--seconds 5]  # who is at the camera NOW
  .venv\\Scripts\\python -m jarvis_camera.facecli add --name "Ravi"     # enroll  (admin key)
  .venv\\Scripts\\python -m jarvis_camera.facecli delete --name "Ravi"  # remove  (admin key)
  (Unix:  .venv/bin/python -m jarvis_camera.facecli ...)

Security model (small, deliberate):
  • `list` / `verify` use the low-privilege **device** key (read-only) — they cannot change anything.
  • `add` / `delete` use the separate **admin** key (config/admin.key) — the only privileged ops.
  • `verify` is fully **local**: it captures, recognizes on-device, and sends NOTHING to the server.
  • Outbound HTTP only; this process never opens a listening socket.
"""
import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

from .capture import Camera
from .detectors.faces import FaceDetector
from .keyfile import load_key
from . import enroll

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("camera.facecli")
CAMERA_ROOT = Path(__file__).resolve().parents[1]


def _cfg(path):
    p = Path(path)
    if not p.is_absolute():
        p = CAMERA_ROOT / p
    if not p.exists():
        sys.exit(f"No config at {p} — copy config.example.json to config/config.json first.")
    return json.loads(p.read_text())


def _req(method, url, key, data=None, timeout=20):
    """Authenticated JSON request (outbound only). Returns parsed JSON ({} if empty body)."""
    body = json.dumps(data).encode("utf-8") if data is not None else None
    headers = {"Authorization": "Bearer " + key}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


def _device_key(cfg):
    k = load_key(cfg["server"].get("api_key_file", "config/agent.key"), CAMERA_ROOT)
    if not k:
        sys.exit("No device key in config/agent.key — mint a device-bound key in the admin Keys tab.")
    return k


def _admin_key(cfg):
    k = load_key(cfg["server"].get("admin_key_file", "config/admin.key"), CAMERA_ROOT)
    if not k:
        sys.exit("No admin key in config/admin.key — add/delete are admin-only. Put an ADMIN key there "
                 "(keep it off the device when you're done managing).")
    return k


def cmd_list(cfg, args):
    key = _device_key(cfg)
    try:
        enrolled = _req("GET", cfg["server"]["url"].rstrip("/") + "/faces/enrolled", key).get("enrolled", {})
    except urllib.error.HTTPError as e:
        sys.exit(f"server HTTP {e.code} listing faces")
    if not enrolled:
        print("No faces enrolled yet."); return
    print(f"{len(enrolled)} enrolled face(s):")
    for name in sorted(enrolled):
        print(f"  - {name}")


def cmd_verify(cfg, args):
    key = _device_key(cfg)
    fd = FaceDetector(cfg.get("detectors", {}).get("faces", {}))
    if not fd.has_identity():
        sys.exit("Verify needs OpenCV + the YuNet/SFace models — run setup (it downloads them) and "
                 "set detectors.faces.detector_model / embed_model.")
    try:
        enrolled = _req("GET", cfg["server"]["url"].rstrip("/") + "/faces/enrolled", key).get("enrolled", {})
    except Exception as e:
        log.warning("could not fetch enrolled set (%s) — continuing with none", e)
        enrolled = {}
    fd.set_known(enrolled)

    cam_cfg = cfg.get("camera", {})
    cam = Camera(backend=cam_cfg.get("backend", "auto"), device=cam_cfg.get("device", 0),
                 width=cam_cfg.get("width", 480), height=cam_cfg.get("height", 360), fps=cam_cfg.get("fps", 8))
    cam.open()
    log.info("Look at the camera — identifying for %ds (fully local, nothing is sent)...", args.seconds)
    votes, scores, t0 = Counter(), [], time.time()
    while time.time() - t0 < args.seconds:
        frame = cam.read()
        if frame is None:
            time.sleep(0.05); continue
        rows = fd.detect(frame)
        if not rows:
            continue
        row = max(rows, key=lambda r: r[2] * r[3])
        name, score = fd.recognize(frame, row)
        if name:
            votes[name] += 1
            if score is not None:
                scores.append(score)
        time.sleep(0.1)
    cam.close()
    if not votes:
        print("No face recognized (no face seen, or no enrolled match)."); return
    best, n = votes.most_common(1)[0]
    avg = sum(scores) / len(scores) if scores else 0.0
    print(f"Identified: {best}  ({n} frames, avg similarity {avg:.2f})")


def cmd_add(cfg, args):
    # Reuse the enrollment flow (capture -> average embedding -> POST), which uses the ADMIN key.
    enroll.run(args.name, args.frames, args.config)


def cmd_delete(cfg, args):
    key = _admin_key(cfg)
    base = cfg["server"]["url"].rstrip("/")
    try:
        faces = _req("GET", base + "/admin/faces", key).get("faces", [])
    except urllib.error.HTTPError as e:
        sys.exit(f"server HTTP {e.code} — /admin/faces is admin-only; is config/admin.key an admin key?")
    matches = [f for f in faces if f["name"] == args.name]
    if not matches:
        sys.exit(f"No enrolled face named '{args.name}'. Run `list` to see the names.")
    for f in matches:
        _req("DELETE", f"{base}/admin/faces/{f['id']}", key)
        log.info("deleted face '%s' (id %s)", f["name"], f["id"])
    print(f"Deleted {len(matches)} face(s) named '{args.name}'.")


def main():
    ap = argparse.ArgumentParser(description="Manage faces (list/verify/add/delete) on the camera device")
    ap.add_argument("--config", default=str(CAMERA_ROOT / "config" / "config.json"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list enrolled face names (device key, read-only)")
    v = sub.add_parser("verify", help="identify who is at the camera now (fully local)")
    v.add_argument("--seconds", type=int, default=5, help="how long to sample (default 5)")
    a = sub.add_parser("add", help="enroll a face (admin key)")
    a.add_argument("--name", required=True, help="person's display name")
    a.add_argument("--frames", type=int, default=7, help="good frames to average (default 7)")
    d = sub.add_parser("delete", help="delete an enrolled face by name (admin key)")
    d.add_argument("--name", required=True, help="exact enrolled name to remove")
    args = ap.parse_args()
    cfg = _cfg(args.config)
    {"list": cmd_list, "verify": cmd_verify, "add": cmd_add, "delete": cmd_delete}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
