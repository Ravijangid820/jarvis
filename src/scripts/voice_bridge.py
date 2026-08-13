#!/usr/bin/env python3
"""Voice listener bridge: whisper-stream → wake word → JARVIS (spoken).

whisper-stream does continuous transcription to stdout; we read it in Python, gate on the wake
word, and either greet or POST the command to /inbox as JSON via urllib — **no shell**, so
transcribed audio can never be executed as a command. Replies are spoken back via the server's
Piper TTS (played locally with paplay/aplay/ffplay).

  uv run python src/scripts/voice_bridge.py            # uses defaults below
  (run via src/scripts/run_listener.sh on the box)

Behaviour:
  "Jarvis"                       → spoken greeting ("Yes, sir?")   [GET /greeting]
  "Jarvis, good morning"         → spoken greeting
  "Jarvis, <anything else>"      → POST /inbox, spoken reply       [voice_feedback]

Which microphone: the admin picks one in the web UI (⊕ → Microphone), which writes
<repo>/config/active_mic.json; this reads it at startup and passes `-c <id>` to whisper-stream.
Unset (or -1) means the system default, exactly as before. VOICE_CAPTURE_ID overrides both.

Tune via env vars (paths default to this repo):
  JARVIS_SERVER_URL   default http://localhost:5000
  VOICE_WAKE_WORD     default "jarvis"
  WHISPER_BIN         default <repo>/whisper/build/bin/whisper-stream
  WHISPER_MODEL       default <repo>/whisper/models/ggml-base.en.bin
  VOICE_KEY_FILE      default <repo>/config/voice_listener.key
  VOICE_CAPTURE_ID    default from config/active_mic.json (-1 = system default)
"""
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SERVER = os.environ.get("JARVIS_SERVER_URL", "http://localhost:5000").rstrip("/")
WAKE = os.environ.get("VOICE_WAKE_WORD", "jarvis").lower()
WHISPER_BIN = os.environ.get("WHISPER_BIN", str(REPO / "whisper/build/bin/whisper-stream"))
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", str(REPO / "whisper/models/ggml-base.en.bin"))
KEY_FILE = Path(os.environ.get("VOICE_KEY_FILE", str(REPO / "config/voice_listener.key")))
ACTIVE_MIC = REPO / "config" / "active_mic.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("voice-bridge")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# The orchestrator's classifier, imported rather than reimplemented. This file used to carry its
# own — `c in _GREETINGS or c.startswith(_GREETINGS)` — which, because startswith takes a tuple and
# "hi" is in the list, treated ANY utterance merely beginning with those letters as a greeting:
# "Jarvis, hit the lights" and "Jarvis, there is a problem with the fan" were both answered "Yes,
# sir?" and never acted on. intents.py is pure (re + datetime, no I/O), so importing it costs
# nothing and there is now one definition instead of a copy per surface.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestrator"))
from intents import is_greeting  # noqa: E402


def play_audio(b64: str) -> None:
    """Play base64 WAV through the first available system player (no shell interpolation)."""
    if not b64:
        return
    player = next((p for p in ("paplay", "aplay", "ffplay") if shutil.which(p)), None)
    if not player:
        log.warning("no audio player found (install pulseaudio-utils / alsa-utils / ffmpeg) — can't speak")
        return
    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(base64.b64decode(b64)); path = f.name
        args = ([player, "-nodisp", "-autoexit", "-loglevel", "quiet", path]
                if player == "ffplay" else [player, path])
        subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    except Exception as e:
        log.error("audio playback failed: %s", e)
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def fetch_greeting(token: str) -> None:
    """GET /greeting → speak JARVIS's acknowledgement (the reply to just the wake word)."""
    req = urllib.request.Request(f"{SERVER}/greeting", headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode())
        log.info("jarvis: %s", d.get("text", ""))
        play_audio(d.get("audio"))
    except Exception as e:
        log.error("greeting failed: %s", e)


def post_inbox(text: str, token: str) -> None:
    """POST the command to /inbox (JSON, no shell) and speak the reply."""
    body = json.dumps({"text": text, "voice_feedback": True}).encode("utf-8")
    req = urllib.request.Request(
        f"{SERVER}/inbox", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            resp = json.loads(r.read().decode())
        log.info("jarvis: %s", (resp.get("response") or "")[:160])
        play_audio(resp.get("audio"))
    except urllib.error.HTTPError as e:
        log.error("server HTTP %s", e.code)
    except Exception as e:
        log.error("post failed: %s", e)


def resolve_capture_id() -> int:
    """Which SDL capture device to open: the admin's choice, re-resolved by NAME. -1 = system default.

    Re-resolving matters. SDL's capture indices are positional, so unplugging a device shifts every
    one after it down by one — a stored bare index can therefore come to mean a *different*
    microphone, and an assistant that quietly starts listening on the wrong one is worse than one
    that says it can't find the right one. So the name is authoritative: if it is still present we
    use wherever it sits now, and if it has gone we fall back to the default and say so loudly
    rather than open whatever inherited its number.
    """
    env = os.environ.get("VOICE_CAPTURE_ID")
    if env not in (None, ""):
        try:
            return int(env)
        except ValueError:
            log.warning("VOICE_CAPTURE_ID=%r is not a number — using the system default", env)
            return -1
    if not ACTIVE_MIC.is_file():
        return -1
    try:
        chosen = json.loads(ACTIVE_MIC.read_text())
    except Exception as e:
        log.warning("could not read %s (%s) — using the system default", ACTIVE_MIC, e)
        return -1
    cid, name = chosen.get("capture_id", -1), chosen.get("name")
    if cid < 0 or not name:
        return -1
    try:
        proc = subprocess.run([sys.executable, str(REPO / "src/scripts/list_mics.py")],
                              capture_output=True, text=True, timeout=10)
        devices = json.loads(proc.stdout).get("devices", [])
    except Exception as e:
        log.warning("could not enumerate microphones (%s) — trusting the stored index %d", e, cid)
        return cid
    match = next((d for d in devices if d["name"] == name), None)
    if match is None:
        log.warning("selected microphone %r is not attached (devices: %s) — falling back to the "
                    "system default. Re-select it in the web UI once it's plugged in.",
                    name, [d["name"] for d in devices] or "none")
        return -1
    if match["id"] != cid:
        log.info("microphone %r moved from index %d to %d — using %d", name, cid, match["id"], match["id"])
    return match["id"]


def parse_line(line: str):
    """(heard, command) — heard=True if the wake word is in the line; command is the trailing text."""
    text = _ANSI.sub("", line).strip()
    if not text or text.startswith(("[", "#")):   # whisper status lines like "[Start speaking]"
        return (False, "")
    idx = text.lower().find(WAKE)
    if idx < 0:
        return (False, "")
    return (True, text[idx + len(WAKE):].lstrip(" ,.:;-").strip())


def main():
    if not KEY_FILE.is_file():
        sys.exit(f"FATAL: {KEY_FILE} missing. Mint one DEVICE-SCOPED (never a plain admin key —\n"
                 f"an open mic must not wield admin; see docs/planning/voice-wake.md §3):\n"
                 f"  uv run python src/scripts/manage.py mint-key <you> voice-listener voice-lab > {KEY_FILE}\n"
                 f"  chmod 600 {KEY_FILE}")
    if KEY_FILE.stat().st_mode & 0o077:
        log.warning("%s is group/other-readable — run: chmod 600 %s", KEY_FILE, KEY_FILE)
    token = KEY_FILE.read_text().strip()
    if not Path(WHISPER_BIN).exists():
        sys.exit(f"FATAL: whisper-stream not found at {WHISPER_BIN} (build it: bash src/scripts/build_native.sh)")

    # whisper-stream args are an argv LIST (no shell). VAD/sliding mode (--step 0) emits one
    # transcription per utterance. Tune --length/-vth/-t to your mic + CPU on the box.
    argv = [WHISPER_BIN, "-m", WHISPER_MODEL, "-t", "4", "--step", "0",
            "--length", "5000", "-vth", "0.6", "--no-context"]
    capture_id = resolve_capture_id()
    if capture_id >= 0:
        argv += ["-c", str(capture_id)]
    log.info("listening (wake word: '%s', mic: %s) — %s", WAKE,
             f"capture #{capture_id}" if capture_id >= 0 else "system default", " ".join(argv))
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    last, last_t = "", 0.0
    try:
        for line in proc.stdout:
            heard, cmd = parse_line(line)
            if not heard:
                continue
            now = time.time()
            key = cmd or "<wake>"
            if key == last and now - last_t < 3.0:   # debounce repeated partials
                continue
            last, last_t = key, now
            if not cmd or is_greeting(cmd):
                log.info("wake word%s → greeting", f" + '{cmd}'" if cmd else "")
                fetch_greeting(token)
            else:
                log.info("heard: %s", cmd)
                post_inbox(cmd, token)
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
