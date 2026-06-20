"""Event client: POST high-level events to the orchestrator.

Events are queued and sent from a background thread with retry, so a brief server outage
or network blip doesn't block the capture loop or lose events (bounded in-memory queue;
disk persistence is a future enhancement). Auth uses the device's machine API key.
"""
import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger("camera.events")


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EventClient:
    def __init__(self, url, api_key, device_id, endpoint="/events", dry_run=False, timeout=5,
                 max_queue=500, verify=True):
        self.endpoint = url.rstrip("/") + endpoint
        self.device_id = device_id
        self.dry_run = dry_run
        self.timeout = timeout
        self.verify = verify          # requests verify= : CA path (https w/ local CA) or True
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._q = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._worker = None

    def start(self):
        if self.dry_run:
            return
        self._worker = threading.Thread(target=self._drain, daemon=True, name="camera-events")
        self._worker.start()

    def send(self, type, data=None):
        evt = {"device_id": self.device_id, "type": type, "ts": _now_iso(), "data": data or {}}
        if self.dry_run:
            log.info("[dry-run] event %s", json.dumps(evt))
            return
        try:
            self._q.put_nowait(evt)
        except queue.Full:
            log.warning("event queue full — dropping %s", type)

    def _drain(self):
        backoff = 1
        while not self._stop.is_set():
            try:
                evt = self._q.get(timeout=1)
            except queue.Empty:
                continue
            while not self._stop.is_set():
                try:
                    r = requests.post(self.endpoint, headers=self._headers, json=evt,
                                      timeout=self.timeout, verify=self.verify)
                    if r.status_code < 300:
                        backoff = 1
                        break
                    log.warning("event POST %s → HTTP %s", evt["type"], r.status_code)
                except requests.RequestException as e:
                    log.warning("event POST failed (%s) — retrying in %ss", e, backoff)
                # retry with capped backoff; the event stays in hand until it lands
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30)
            self._q.task_done()

    def stop(self):
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=2)
