"""Body/pose tracking via MediaPipe Pose. STUB — implement/tune on the Pi.

Plan: run MediaPipe Pose (model_complexity=0, low res) on motion-gated frames only. Derive
simple, useful signals from the landmarks — person present, rough position (left/center/right),
standing vs sitting — and emit {"type": "pose", "data": {...}} rather than raw landmarks.

Pi 3 B+ reality: expect ~2–4 FPS and meaningful RAM use; needs a 64-bit OS for the mediapipe
wheel. Keep it OFF unless actively used, and never run concurrently with gestures/faces on 1 GB.
"""
import logging

from .base import Detector

log = logging.getLogger("edge.pose")


class PoseDetector(Detector):
    name = "pose"
    heavy = True

    def __init__(self, cfg):
        super().__init__(cfg)
        self._warned = False
        # TODO: import mediapipe; self._pose = mp.solutions.pose.Pose(model_complexity=0, ...)

    def process(self, frame):
        if not self._warned:
            log.warning("pose detector enabled but not implemented yet — no-op")
            self._warned = True
        return []
