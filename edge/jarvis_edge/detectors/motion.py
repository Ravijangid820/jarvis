"""Motion detection — cheap, runs every frame, and acts as the gate for the heavy detectors.

Uses an MOG2 background subtractor. `moving` reflects the current frame (the agent reads it
to decide whether to escalate); a "motion" event is emitted at most once per cooldown so we
don't spam the server while someone is in view.
"""
import logging
import time

from .base import Detector

log = logging.getLogger("edge.motion")


class MotionDetector(Detector):
    name = "motion"
    heavy = False

    def __init__(self, cfg):
        super().__init__(cfg)
        import cv2
        self._bg = cv2.createBackgroundSubtractorMOG2(history=200, varThreshold=25, detectShadows=False)
        self.min_area = int(self.cfg.get("min_area", 1500))
        self.cooldown_s = float(self.cfg.get("cooldown_s", 3))
        self.moving = False
        self._last_event = 0.0

    def process(self, frame):
        import cv2
        mask = self._bg.apply(frame)
        mask = cv2.medianBlur(mask, 5)
        area = int((mask > 0).sum())
        self.moving = area > self.min_area
        if self.moving and (time.time() - self._last_event) > self.cooldown_s:
            self._last_event = time.time()
            return [{"type": "motion", "data": {"area": area}}]
        return []
