"""Hand-gesture recognition via MediaPipe Hands.

Classifies a small set from the landmarks — open_palm, fist, thumb_up, thumb_down, point — and
emits a `gesture` event on change (debounced). The server (or the Pi, if you add local actions)
maps gestures → actions. DEFINE what "volume" controls (server media / a player / the Pi's audio)
before wiring that mapping. Lazy MediaPipe import + graceful no-op if it's missing.

Pi 3 B+: model_complexity=0, max_num_hands=1, motion-gated; expect a few FPS; 64-bit OS.
"""
import logging

from .base import Detector

log = logging.getLogger("edge.gestures")

_TIPS, _PIPS = (8, 12, 16, 20), (6, 10, 14, 18)


class GestureDetector(Detector):
    name = "gestures"
    heavy = True

    def __init__(self, cfg):
        super().__init__(cfg)
        self._mp = None
        self._hands = None
        self._ok = False
        self._warned = False
        self._last = None
        try:
            import mediapipe as mp
            self._mp = mp
            self._hands = mp.solutions.hands.Hands(
                max_num_hands=1, model_complexity=int(self.cfg.get("model_complexity", 0)),
                min_detection_confidence=0.6, min_tracking_confidence=0.5)
            self._ok = True
        except Exception as e:
            log.warning("gestures init failed (mediapipe missing/unsupported?): %s", e)

    @staticmethod
    def _classify(lm, handed):
        # Non-thumb finger extended if its tip is above (smaller y) its PIP joint.
        fingers = sum(1 for t, p in zip(_TIPS, _PIPS) if lm[t].y < lm[p].y)
        thumb_ext = (lm[4].x < lm[3].x) if handed == "Right" else (lm[4].x > lm[3].x)
        total = fingers + (1 if thumb_ext else 0)
        if total == 0:
            return "fist"
        if total == 5:
            return "open_palm"
        if thumb_ext and fingers == 0:
            return "thumb_up" if lm[4].y < lm[0].y else "thumb_down"
        if fingers == 1 and lm[8].y < lm[6].y and not thumb_ext:
            return "point"
        return None      # unrecognized — don't emit (avoid noise)

    def process(self, frame):
        if not self._ok:
            if not self._warned:
                log.warning("gestures unavailable — no-op")
                self._warned = True
            return []
        import cv2
        res = self._hands.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        if not res.multi_hand_landmarks:
            self._last = None
            return []
        lm = res.multi_hand_landmarks[0].landmark
        handed = "Right"
        try:
            handed = res.multi_handedness[0].classification[0].label
        except Exception:
            pass
        g = self._classify(lm, handed)
        if g and g != self._last:
            self._last = g
            return [{"type": "gesture", "data": {"gesture": g}}]
        return []

    def close(self):
        if self._hands is not None:
            try:
                self._hands.close()
            except Exception:
                pass
