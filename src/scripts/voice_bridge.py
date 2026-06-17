#!/usr/bin/env python3
"""Voice listener bridge: whisper-stream → wake word → POST /inbox.

Replaces the old `whisper-command -cmd "curl … %s"` approach, which was both broken
(whisper-command's `-cmd` is a *commands file*, not a shell template — it never substituted the
transcript) and unsafe-by-design (shelling a transcript). Here whisper-stream does continuous
transcription to stdout; we read it in Python, gate on the wake word, and POST the command as
JSON via urllib — **no shell**, so transcribed audio can never be executed as a command.

  uv run python src/scripts/voice_bridge.py            # uses defaults below
  (run via src/scripts/run_listener.sh on the box)

Tune via env vars (paths default to this repo):
  JARVIS_SERVER_URL   default http://localhost:5000
  VOICE_WAKE_WORD     default "jarvis"
  WHISPER_BIN         default <repo>/whisper/build/bin/whisper-stream
  WHISPER_MODEL       default <repo>/whisper/models/ggml-base.en.bin
  VOICE_KEY_FILE      default <repo>/config/voice_listener.key
"""
import json
import logging
import os
import re
import subprocess
import sys
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("voice-bridge")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def post_inbox(text: str, token: str) -> None:
    """POST the command to /inbox as JSON (no shell; urllib does the encoding)."""
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{SERVER}/inbox", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read().decode())
        log.info("jarvis: %s", (resp.get("response") or "")[:160])
    except urllib.error.HTTPError as e:
        log.error("server HTTP %s", e.code)
    except Exception as e:
        log.error("post failed: %s", e)


def extract_command(line: str):
    """Return the command after the wake word, or None. Tolerant of whisper-stream's markers."""
    text = _ANSI.sub("", line).strip()
    if not text or text.startswith(("[", "#")):   # status lines like "[Start speaking]" / "###"
        return None
    low = text.lower()
    idx = low.find(WAKE)
    if idx < 0:
        return None
    cmd = text[idx + len(WAKE):].lstrip(" ,.:;-").strip()
    return cmd or None


def main():
    if not KEY_FILE.is_file():
        sys.exit(f"FATAL: {KEY_FILE} missing. Mint one:\n"
                 f"  uv run python src/scripts/manage.py mint-key admin voice-listener > {KEY_FILE}\n"
                 f"  chmod 600 {KEY_FILE}")
    token = KEY_FILE.read_text().strip()
    if not Path(WHISPER_BIN).exists():
        sys.exit(f"FATAL: whisper-stream not found at {WHISPER_BIN} (build it: bash src/scripts/build_native.sh)")

    # whisper-stream args are an argv LIST (no shell). VAD/sliding mode (--step 0) emits one
    # transcription per utterance. Tune --length/-vth/-t to your mic + CPU on the box.
    argv = [WHISPER_BIN, "-m", WHISPER_MODEL, "-t", "4", "--step", "0",
            "--length", "5000", "-vth", "0.6", "--no-context"]
    log.info("listening (wake word: '%s') — %s", WAKE, " ".join(argv))
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    last, last_t = "", 0.0
    try:
        for line in proc.stdout:
            cmd = extract_command(line)
            if not cmd:
                continue
            now = time.time()
            if cmd == last and now - last_t < 3.0:   # debounce repeated partials
                continue
            last, last_t = cmd, now
            log.info("heard: %s", cmd)
            post_inbox(cmd, token)
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
