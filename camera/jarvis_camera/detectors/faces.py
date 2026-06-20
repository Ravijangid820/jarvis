"""Face detection + identity on the OpenCV **YuNet + SFace** stack (CPU; no MediaPipe/onnxruntime).

- **YuNet** (`cv2.FaceDetectorYN`) finds faces and returns 5 landmarks per face — fast + accurate on
  CPU (tiny: ~75K params).
- **SFace** (`cv2.FaceRecognizerSF`) uses those landmarks to **align** each face before producing a
  128-D embedding. Alignment is what makes recognition accurate (vs a naive center-crop).

Both are small ONNX models from the **official OpenCV Zoo** (auto-downloaded + sha256-verified by
setup). Everything runs through `opencv-python`, which the agent already needs — so the face path
adds no extra runtime dependency.

Detection turns on when `detector_model` (YuNet) is set; identity additionally needs `embed_model`
(SFace). Missing model → a one-warning no-op (the agent never crashes). Keep it motion-gated +
throttled (`interval_s`) on a Pi.
"""
import json
import logging
import math
from pathlib import Path

from .base import Detector
from ..paths import base_dir

log = logging.getLogger("camera.faces")
CAMERA_ROOT = base_dir()


class FaceDetector(Detector):
    name = "faces"
    heavy = True

    def __init__(self, cfg):
        super().__init__(cfg)
        self._cv2 = None
        self._det = None          # YuNet detector
        self._rec = None          # SFace recognizer
        self._known = {}          # name -> embedding (list[float], L2-normalized)
        self._size = (0, 0)       # last input size pushed to YuNet
        self._score = float(self.cfg.get("score_threshold", 0.9))
        self._nms = float(self.cfg.get("nms_threshold", 0.3))
        self._topk = int(self.cfg.get("top_k", 5000))
        self._thresh = float(self.cfg.get("recognize_threshold", 0.363))  # SFace cosine threshold
        self._ok = False
        self._warned = False
        self._init()

    def _resolve(self, p):
        """Resolve a model path (relative paths are relative to the camera/ root), or None."""
        if not p:
            return None
        path = Path(p)
        if not path.is_absolute():
            path = CAMERA_ROOT / path
        return path if path.exists() else None

    def _init(self):
        try:
            import cv2
            self._cv2 = cv2
            det_model = self._resolve(self.cfg.get("detector_model"))
            if det_model is None:
                log.warning("faces: YuNet model not found — run setup to download it "
                            "(detectors.faces.detector_model)")
                return
            self._det = cv2.FaceDetectorYN.create(str(det_model), "", (320, 320),
                                                  self._score, self._nms, self._topk)
            emb_model = self._resolve(self.cfg.get("embed_model"))
            if emb_model is not None:
                self._rec = cv2.FaceRecognizerSF.create(str(emb_model), "")
                ef = self._resolve(self.cfg.get("enrolled_file"))
                if ef is not None:
                    self._known = json.loads(ef.read_text())
                log.info("faces: YuNet + SFace identity ON (%d enrolled)", len(self._known))
            else:
                log.info("faces: YuNet detection ON (no SFace embed_model → no identity)")
            self._ok = True
        except Exception as e:
            log.warning("faces init failed: %s", e)

    def detect(self, frame):
        """Return a list of YuNet face rows (each: box[4] + 5 landmarks[10] + score[1]); [] if none."""
        if self._det is None:
            return []
        h, w = frame.shape[:2]
        if (w, h) != self._size:
            self._det.setInputSize((w, h))
            self._size = (w, h)
        _, faces = self._det.detect(frame)
        return [row for row in faces] if faces is not None else []

    def embed(self, frame, face_row):
        """Align (SFace, using YuNet's landmarks) + embed one face → L2-normalized list[float]."""
        if self._rec is None:
            return None
        aligned = self._rec.alignCrop(frame, face_row)
        feat = self._rec.feature(aligned)[0]
        norm = math.sqrt(float(sum(float(v) * float(v) for v in feat))) or 1.0
        return [float(v) / norm for v in feat]

    def recognize(self, frame, face_row):
        """(name, cosine) for one face; ('unknown', score) if below threshold; (None, None) if no model.
        Each person may have MANY embeddings — score against the best of them."""
        vec = self.embed(frame, face_row)
        if vec is None:
            return (None, None)
        best, best_sim = None, -1.0
        for nm, embs in self._known.items():
            for ev in embs:                                # both L2-normalized → cosine
                sim = sum(a * b for a, b in zip(vec, ev))
                if sim > best_sim:
                    best, best_sim = nm, sim
        if best_sim >= self._thresh:
            return (best, round(best_sim, 3))
        return ("unknown", round(best_sim, 3))

    def set_known(self, known):
        """Replace the enrolled set. Accepts {name: [emb, ...]} (multiple per person) or the older
        {name: emb} — normalized to a list per name. The agent pulls this from /faces/enrolled."""
        norm = {}
        for nm, val in (known or {}).items():
            norm[nm] = val if (val and isinstance(val[0], (list, tuple))) else [val]
        self._known = norm

    def has_identity(self):
        """True if recognition/enrollment is possible (SFace embedding model loaded)."""
        return bool(self._ok and self._rec is not None)

    @staticmethod
    def _box(face_row):
        x, y, w, h = (int(v) for v in face_row[:4])
        return (max(0, x), max(0, y), max(1, w), max(1, h))

    def process(self, frame):
        if not self._ok:
            if not self._warned:
                log.warning("faces unavailable (init failed / no model) — no-op")
                self._warned = True
            return []
        events = []
        for row in self.detect(frame):
            name, score = (None, None)
            if self._rec is not None:
                try:
                    name, score = self.recognize(frame, row)
                except Exception as e:
                    log.debug("recognize failed: %s", e)
            x, y, w, h = self._box(row)
            events.append({"type": "face_seen",
                           "data": {"box": [x, y, w, h], "name": name, "score": score}})
        return events
