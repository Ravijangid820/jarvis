"""Jarvis volume agent (Windows) — OUTBOUND-ONLY.

Polls the orchestrator for pending volume commands and applies them to the system master
volume via the Windows Core Audio API (pycaw). It opens **no listening socket** — only
authenticated *outbound* requests to the orchestrator — so it cannot be a network entry point.
The command vocabulary is a tiny validated set (set/step/mute/unmute); there is **no shell-out**,
so there's no command-injection path. Run as your normal user — no admin required.

  python volume_agent.py --config config.json
"""
import argparse
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("volume-agent")
HERE = Path(__file__).resolve().parent


class Volume:
    """Thin wrapper over the Windows master volume (pycaw). Kept separate from the transport
    so a future move to MQTT/Home Assistant doesn't touch the audio logic."""

    def __init__(self):
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        spk = AudioUtilities.GetSpeakers()
        iface = spk.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        self._vol = cast(iface, POINTER(IAudioEndpointVolume))

    def get_pct(self):
        return self._vol.GetMasterVolumeLevelScalar() * 100.0

    def set_pct(self, pct):
        self._vol.SetMasterVolumeLevelScalar(max(0.0, min(1.0, pct / 100.0)), None)

    def mute(self, on):
        self._vol.SetMute(1 if on else 0, None)


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def apply_command(vol, cmd):
    """Apply one command, validating the server's data defensively — never assume the
    server/channel is honest: unknown actions and bad/missing values are rejected, and the
    target is clamped to 0–100. The worst a malformed command can do is nothing."""
    action = (cmd.get("action") or "").lower()
    p = cmd.get("params") or {}
    if action in ("set", "step"):
        val = _int_or_none(p.get("value"))
        if val is None:
            log.warning("ignoring %s with bad/missing value: %r", action, p.get("value"))
            return
        target = val if action == "set" else vol.get_pct() + val
        vol.set_pct(max(0, min(100, target)))
    elif action == "mute":
        vol.mute(True)
    elif action == "unmute":
        vol.mute(False)
    else:
        log.warning("ignoring unknown action: %r", action)
        return
    log.info("applied %s %s", action, p or "")


def run(cfg):
    base = cfg["server"]["url"].rstrip("/")
    device = cfg.get("device_id", "laptop")
    wait = int(cfg.get("poll_wait_s", 20))
    key_path = Path(cfg["server"]["api_key_file"])
    if not key_path.is_absolute():
        key_path = HERE / key_path
    headers = {"Authorization": "Bearer " + key_path.read_text().strip()}
    url = f"{base}/devices/commands?device={device}&wait={wait}"

    vol = Volume()
    log.info("volume agent started (device=%s, server=%s) — outbound poll only", device, base)
    backoff = 1
    while True:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=wait + 10) as r:
                cmds = json.loads(r.read().decode()).get("commands", [])
            backoff = 1
            for cmd in cmds:
                try:
                    apply_command(vol, cmd)
                except Exception as e:
                    log.error("apply failed for %s: %s", cmd, e)
        except urllib.error.HTTPError as e:
            log.error("server HTTP %s — backing off %ss", e.code, backoff)
            time.sleep(min(backoff, 30)); backoff = min(backoff * 2, 30)
        except Exception as e:
            log.warning("poll error (%s) — retrying in %ss", e, backoff)
            time.sleep(min(backoff, 30)); backoff = min(backoff * 2, 30)


def main():
    ap = argparse.ArgumentParser(description="Jarvis Windows volume agent (outbound-only)")
    ap.add_argument("--config", default=str(HERE / "config.json"))
    run(json.loads(Path(ap.parse_args().config).read_text()))


if __name__ == "__main__":
    main()
