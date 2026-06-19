"""Detector interface. Keep process() cheap-ish and side-effect-free; the agent decides
when to call heavy detectors (motion-gated, throttled, one at a time)."""


class Detector:
    name = "base"
    heavy = False   # True → the agent runs it only when motion-gated, one per cycle

    def __init__(self, cfg):
        self.cfg = cfg or {}
        self.interval_s = float(self.cfg.get("interval_s", 0))

    def process(self, frame):
        """Return a list of event dicts: [{"type": str, "data": dict}, ...] (possibly empty)."""
        raise NotImplementedError

    def close(self):
        pass
