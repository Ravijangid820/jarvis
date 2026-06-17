"""Hand-gesture recognition via MediaPipe Hands. STUB — implement/tune on the Pi.

Plan: MediaPipe Hands on motion-gated frames → classify a small gesture set from the landmarks
(e.g. open palm, fist, thumb up/down, swipe up/down) → emit
  {"type": "gesture", "data": {"gesture": "volume_up", ...}}.
The server (or the Pi, if configured for local actions) maps gestures → actions. DEFINE what
"volume" controls — the server's media, a specific player, or the Pi's own audio — before wiring.

Pi 3 B+ reality: same as pose — a few FPS, memory-tight, 64-bit OS; run one heavy detector at a time.
"""
import logging

from .base import Detector

log = logging.getLogger("edge.gestures")


class GestureDetector(Detector):
    name = "gestures"
    heavy = True

    def __init__(self, cfg):
        super().__init__(cfg)
        self._warned = False
        # TODO: import mediapipe; self._hands = mp.solutions.hands.Hands(max_num_hands=1, ...)

    def process(self, frame):
        if not self._warned:
            log.warning("gestures detector enabled but not implemented yet — no-op")
            self._warned = True
        return []
