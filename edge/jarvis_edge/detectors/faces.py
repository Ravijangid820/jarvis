"""Face detection + identity. STUB — implement/tune on the Pi.

Plan (Pi 3 B+ friendly, triggered by motion, not every frame):
  1. Detect faces with OpenCV's DNN SSD (res10) — light and decent.
  2. For each face, compute a 128/512-d embedding with a small ONNX model (e.g. MobileFaceNet)
     via onnxruntime; match against enrolled embeddings (cosine) → name or "unknown".
  3. Emit {"type": "face_seen", "data": {"name": ..., "score": ...}}.
Enroll known faces offline into a small embeddings file. Throttle via interval_s; on 1 GB RAM
keep this the only heavy detector running at a time.
"""
import logging

from .base import Detector

log = logging.getLogger("edge.faces")


class FaceDetector(Detector):
    name = "faces"
    heavy = True

    def __init__(self, cfg):
        super().__init__(cfg)
        self._warned = False
        # TODO: load the OpenCV DNN face detector + ONNX embedding model + enrolled faces.

    def process(self, frame):
        if not self._warned:
            log.warning("faces detector enabled but not implemented yet — no-op")
            self._warned = True
        return []
