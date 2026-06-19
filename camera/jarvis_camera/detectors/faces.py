"""Face detection + optional identity.

Detection prefers **MediaPipe BlazeFace** (more robust than Haar — fewer false positives, handles
angle/lighting, CPU-friendly), and automatically falls back to a DNN SSD (if you point
`detector_proto`/`detector_model` at the res10 caffe files) or the OpenCV Haar cascade if MediaPipe
isn't installed — so detection works on any device, just better where MediaPipe is present.

MediaPipe finds *where* a face is; **identity** (*who* it is) is separate and optional: it turns on
when you provide `embed_model` (an ONNX face-embedding model, e.g. MobileFaceNet) and an
`enrolled_file` (JSON: {name: [embedding floats]}). Without those it emits `face_seen` with the
bounding box and name=null.

Pi 3 B+: keep this motion-gated and throttled (interval_s). All heavy imports are lazy and the
detector degrades to a no-op (with one warning) if a dependency/model is missing — so enabling it
never crashes the agent.
"""
import json
import logging
import math
from pathlib import Path

from .base import Detector

log = logging.getLogger("camera.faces")


class FaceDetector(Detector):
    name = "faces"
    heavy = True

    def __init__(self, cfg):
        super().__init__(cfg)
        self._cv2 = None
        self._mp = None           # MediaPipe BlazeFace detector (preferred)
        self._net = None          # DNN detector (optional)
        self._cascade = None      # Haar detector (fallback, always available)
        self._embed = None        # ONNX embedding session (optional)
        self._known = {}          # name -> embedding (list[float])
        self._thresh = float(self.cfg.get("recognize_threshold", 0.45))
        self._size = int(self.cfg.get("embed_size", 112))
        self._ok = False
        self._warned = False
        self._init()

    def _init(self):
        try:
            import cv2
            self._cv2 = cv2
            # Preferred: MediaPipe BlazeFace. Fall back to DNN (if model files) or Haar.
            try:
                import mediapipe as mp
                self._mp = mp.solutions.face_detection.FaceDetection(
                    model_selection=0, min_detection_confidence=float(self.cfg.get("min_confidence", 0.6)))
                log.info("faces: using MediaPipe BlazeFace detector")
            except Exception:
                proto, model = self.cfg.get("detector_proto"), self.cfg.get("detector_model")
                if proto and model and Path(proto).exists() and Path(model).exists():
                    self._net = cv2.dnn.readNetFromCaffe(proto, model)
                    log.info("faces: MediaPipe unavailable — using DNN detector")
                else:
                    self._cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
                    log.info("faces: MediaPipe unavailable — using Haar cascade detector")
            emb = self.cfg.get("embed_model")
            if emb and Path(emb).exists():
                import onnxruntime as ort
                self._embed = ort.InferenceSession(emb, providers=["CPUExecutionProvider"])
                ef = self.cfg.get("enrolled_file")
                if ef and Path(ef).exists():
                    self._known = json.loads(Path(ef).read_text())
                log.info("faces: identity on (%d enrolled)", len(self._known))
            self._ok = True
        except Exception as e:
            log.warning("faces init failed: %s", e)

    def _detect(self, frame):
        cv2 = self._cv2
        h, w = frame.shape[:2]
        if self._mp is not None:
            res = self._mp.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            out = []
            for d in (res.detections or []):
                b = d.location_data.relative_bounding_box
                x, y = int(b.xmin * w), int(b.ymin * h)
                out.append((max(0, x), max(0, y), max(1, int(b.width * w)), max(1, int(b.height * h))))
            return out
        if self._net is not None:
            h, w = frame.shape[:2]
            blob = cv2.dnn.blobFromImage(frame, 1.0, (300, 300), (104, 177, 123))
            self._net.setInput(blob)
            det = self._net.forward()
            out = []
            for i in range(det.shape[2]):
                if det[0, 0, i, 2] >= 0.6:
                    x1, y1, x2, y2 = (det[0, 0, i, 3:7] * [w, h, w, h]).astype(int)
                    out.append((max(0, x1), max(0, y1), max(1, x2 - x1), max(1, y2 - y1)))
            return out
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return [tuple(map(int, b)) for b in self._cascade.detectMultiScale(gray, 1.2, 5, minSize=(60, 60))]

    def embed(self, crop):
        """Face crop → L2-normalized embedding (list[float]); None if no embedding model.
        Used both for recognition and by the enrollment CLI."""
        if self._embed is None:
            return None
        cv2 = self._cv2
        face = cv2.resize(crop, (self._size, self._size)).astype("float32")
        face = (face - 127.5) / 128.0
        blob = face.transpose(2, 0, 1)[None, ...]          # NCHW
        name_in = self._embed.get_inputs()[0].name
        vec = self._embed.run(None, {name_in: blob})[0][0]
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [float(v / norm) for v in vec]

    def _recognize(self, crop):
        vec = self.embed(crop)
        if vec is None:
            return (None, None)
        best, best_sim = None, -1.0
        for nm, ev in self._known.items():
            sim = sum(a * b for a, b in zip(vec, ev))      # both L2-normalized → cosine
            if sim > best_sim:
                best, best_sim = nm, sim
        return (best, round(best_sim, 3)) if best_sim >= self._thresh else ("unknown", round(best_sim, 3))

    def set_known(self, known):
        """Replace the enrolled set (name → embedding). The agent pulls this from /faces/enrolled."""
        self._known = dict(known or {})

    def has_identity(self):
        """True if recognition/enrollment is possible (the embedding model loaded)."""
        return bool(self._ok and self._embed is not None)

    def process(self, frame):
        if not self._ok:
            if not self._warned:
                log.warning("faces unavailable (init failed) — no-op")
                self._warned = True
            return []
        events = []
        for (x, y, w, h) in self._detect(frame):
            name, score = (None, None)
            if self._embed is not None:
                try:
                    name, score = self._recognize(frame[y:y + h, x:x + w])
                except Exception as e:
                    log.debug("recognize failed: %s", e)
            events.append({"type": "face_seen", "data": {"box": [int(x), int(y), int(w), int(h)], "name": name, "score": score}})
        return events
