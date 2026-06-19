"""Download + SHA-256-verify the face models from the OFFICIAL OpenCV Zoo.

Same models + same pinned hashes as setup.sh/setup.ps1 — this is the Python port so the packaged
.exe can fetch them on first run. A hash mismatch raises (supply-chain check), never silently uses a
tampered file.
"""
import hashlib
import urllib.request
from pathlib import Path

_ZOO = "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models"
MODELS = [
    ("face_detection_yunet_2023mar.onnx",
     _ZOO + "/face_detection_yunet/face_detection_yunet_2023mar.onnx",
     "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    ("face_recognition_sface_2021dec.onnx",
     _ZOO + "/face_recognition_sface/face_recognition_sface_2021dec.onnx",
     "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
]


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_models(dest_dir):
    """Ensure both models exist + verify in dest_dir; download any that are missing/wrong. Returns dest."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    for name, url, sha in MODELS:
        out = dest / name
        if out.exists() and _sha256(out) == sha:
            continue
        print(f"  downloading {name} ...")
        urllib.request.urlretrieve(url, out)
        if _sha256(out) != sha:
            out.unlink(missing_ok=True)
            raise RuntimeError(f"SHA-256 mismatch for {name} — refusing (supply-chain check failed)")
        print(f"  {name} verified")
    return dest
