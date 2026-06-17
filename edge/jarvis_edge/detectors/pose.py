"""Body/pose tracking via MediaPipe Pose.

Emits a `pose` event when a person appears/disappears or changes zone (left/center/right) and a
coarse posture guess (standing/sitting from the shoulder–hip vs hip–knee span). We send derived
signals, not raw landmarks. Lazy MediaPipe import + graceful no-op if it's missing.

Pi 3 B+: model_complexity=0, motion-gated, ~2–4 FPS expected; needs a 64-bit OS. Don't run
concurrently with gestures/faces on 1 GB RAM.
"""
import logging

from .base import Detector

log = logging.getLogger("edge.pose")


class PoseDetector(Detector):
    name = "pose"
    heavy = True

    def __init__(self, cfg):
        super().__init__(cfg)
        self._mp = None
        self._pose = None
        self._ok = False
        self._warned = False
        self._last = None      # last emitted (present, zone)
        try:
            import mediapipe as mp
            self._mp = mp
            self._pose = mp.solutions.pose.Pose(
                model_complexity=int(self.cfg.get("model_complexity", 0)),
                min_detection_confidence=0.5, min_tracking_confidence=0.5)
            self._ok = True
        except Exception as e:
            log.warning("pose init failed (mediapipe missing/unsupported?): %s", e)

    def process(self, frame):
        if not self._ok:
            if not self._warned:
                log.warning("pose unavailable — no-op")
                self._warned = True
            return []
        import cv2
        res = self._pose.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        present = res.pose_landmarks is not None
        zone, posture = None, None
        if present:
            lm = res.pose_landmarks.landmark
            P = self._mp.solutions.pose.PoseLandmark
            nose_x = lm[P.NOSE].x
            zone = "left" if nose_x < 0.33 else "right" if nose_x > 0.66 else "center"
            shoulder_y = (lm[P.LEFT_SHOULDER].y + lm[P.RIGHT_SHOULDER].y) / 2
            hip_y = (lm[P.LEFT_HIP].y + lm[P.RIGHT_HIP].y) / 2
            knee_y = (lm[P.LEFT_KNEE].y + lm[P.RIGHT_KNEE].y) / 2
            torso, legs = abs(hip_y - shoulder_y), abs(knee_y - hip_y)
            posture = "sitting" if legs < 0.6 * torso else "standing"
        key = (present, zone)
        if key != self._last:
            self._last = key
            data = {"present": present}
            if present:
                data.update(zone=zone, posture=posture)
            return [{"type": "pose", "data": data}]
        return []

    def close(self):
        if self._pose is not None:
            try:
                self._pose.close()
            except Exception:
                pass
