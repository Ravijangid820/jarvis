"""Camera capture abstraction.

Returns BGR numpy frames (OpenCV convention) regardless of backend, so the rest of the
pipeline doesn't care whether it's a CSI Camera Module (picamera2) or a USB webcam (OpenCV).
Backend is chosen by config: "auto" prefers picamera2 if importable, else OpenCV.
"""
import logging

log = logging.getLogger("camera.capture")


class Camera:
    def __init__(self, backend="auto", device=0, width=480, height=360, fps=8):
        self.backend = backend
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self._impl = None
        self._cap = None      # OpenCV VideoCapture
        self._picam = None    # picamera2 Picamera2

    @staticmethod
    def _has_picamera2():
        try:
            import picamera2  # noqa: F401
            return True
        except Exception:
            return False

    def open(self):
        backend = self.backend
        if backend == "auto":
            backend = "picamera2" if self._has_picamera2() else "opencv"
        if backend == "picamera2":
            self._open_picamera2()
        else:
            self._open_opencv()
        self._impl = backend
        log.info("camera opened via %s @ %dx%d", backend, self.width, self.height)

    def _open_picamera2(self):
        from picamera2 import Picamera2
        self._picam = Picamera2()
        cfg = self._picam.create_preview_configuration(
            main={"size": (self.width, self.height), "format": "RGB888"})
        self._picam.configure(cfg)
        self._picam.start()

    def _open_opencv(self):
        import cv2
        self._cap = cv2.VideoCapture(self.device)
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        self._cap.set(cv2.CAP_PROP_FPS, self.fps)
        if not self._cap.isOpened():
            raise RuntimeError(f"OpenCV could not open camera device {self.device}")

    def read(self):
        """Return one BGR frame (numpy array) or None on failure."""
        import cv2
        if self._impl == "picamera2":
            arr = self._picam.capture_array()       # RGB
            frame = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
            if frame.shape[1] != self.width or frame.shape[0] != self.height:
                frame = cv2.resize(frame, (self.width, self.height))
            return frame
        ok, frame = self._cap.read()
        return frame if ok else None

    def close(self):
        try:
            if self._cap is not None:
                self._cap.release()
            if self._picam is not None:
                self._picam.stop()
        except Exception as e:
            log.warning("camera close error: %s", e)
