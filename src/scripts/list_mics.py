#!/usr/bin/env python3
"""Enumerate the server's audio capture devices, as JSON on stdout.

    uv run python src/scripts/list_mics.py
    {"driver": "alsa", "devices": [{"id": 0, "name": "Boya BY-M1"}],
     "alsa": [{"device": "hw:0,0", "name": "Boya BY-M1 Analog", "card": "Boya"}], "error": null}

Two lists, because the two consumers address hardware differently and neither numbering can be
translated into the other reliably:

  `devices`  SDL capture indices — what the wake-word listener passes to `whisper-stream -c`.
  `alsa`     ALSA PCM ids read from /proc/asound — what ffmpeg captures with for the browser's
             "use the server's microphone" option. These are *stable* (`hw:<card>,<dev>` with a
             card name), which is why the user-facing picker uses them rather than SDL's positional
             indices.

**Why SDL and not `arecord`/`pactl`:** whisper-stream selects its microphone with `-c <ID>`, and that
ID is an index into *SDL's* capture-device list (`SDL_GetNumAudioDevices(SDL_TRUE)` — see
whisper/examples/common-sdl.cpp). ALSA and PulseAudio number their devices differently, so a list
built from `arecord -l` would look plausible and hand whisper-stream the wrong index. Enumerating
through the same library the consumer uses is the only way the numbers mean the same thing.

**Why a separate process:** SDL_Init(AUDIO) opens a connection to the sound server and installs
handlers. Neither the orchestrator (long-lived, serving HTTP) nor the voice bridge (about to fork
whisper-stream, which does its own SDL_Init) should carry that state around for the sake of one
listing, so both shell out to this script and read the JSON back.

Exit code is 0 even when no device is found — "this container has no microphone" is an answer, not a
failure, and it is the answer the UI needs in order to say so.
"""
import ctypes
import ctypes.util
import json
import re
import sys
from pathlib import Path

SDL_INIT_AUDIO = 0x00000010
SDL_TRUE = 1

# "00-00: 92HD81B1X5 Analog : 92HD81B1X5 Analog : playback 1 : capture 1"
_PCM_LINE = re.compile(r"^(\d+)-(\d+):\s*([^:]*?)\s*:\s*([^:]*?)\s*:(.*)$")


def alsa_capture_devices(proc_asound="/proc/asound"):
    """Capture PCMs from /proc/asound — no alsa-utils, no libasound, just two text files.

    Deliberately reads /proc rather than shelling out: `arecord -l` isn't installed on a minimal
    container (this one has only ffmpeg), and the kernel already publishes exactly this.

    Note /proc/asound is visible inside an LXC even when /dev/snd has NOT been passed through — so
    a device listed here can still fail to open. That asymmetry is useful: it lets the UI tell
    "there is no sound card" apart from "the card is there but this container can't reach it".
    """
    root = Path(proc_asound)
    out = []
    try:
        pcm = (root / "pcm").read_text()
    except OSError:
        return out
    # Card index -> friendly name, e.g. " 0 [PCH  ]: HDA-Intel - HDA Intel PCH"
    cards = {}
    try:
        for line in (root / "cards").read_text().splitlines():
            m = re.match(r"^\s*(\d+)\s*\[([^\]]*)\]\s*:\s*(.*)$", line)
            if m:
                cards[m.group(1).lstrip("0") or "0"] = m.group(2).strip() or m.group(3).strip()
    except OSError:
        pass
    for line in pcm.splitlines():
        m = _PCM_LINE.match(line.strip())
        if not m or "capture" not in m.group(5):
            continue                                  # playback-only PCMs (HDMI, speakers)
        card, dev, name = m.group(1), m.group(2), (m.group(4) or m.group(3))
        cid = card.lstrip("0") or "0"
        out.append({
            # plughw rather than hw: it inserts ALSA's format/rate conversion, so a mic that only
            # does 44.1 kHz stereo still yields the 16 kHz mono we ask ffmpeg for.
            "device": f"plughw:{int(card)},{int(dev)}",
            "name": name.strip(),
            "card": cards.get(cid, f"card {cid}"),
        })
    return out


def enumerate_capture_devices():
    """Return {"driver": str|None, "devices": [{"id": int, "name": str}], "error": str|None}.

    Never raises: a missing libSDL2, a sound server that won't talk to us, and a container with no
    /dev/snd are all ordinary states of the world here, and each needs to reach the admin as text
    rather than as a 500.
    """
    out = {"driver": None, "devices": [], "alsa": alsa_capture_devices(), "error": None}
    libname = ctypes.util.find_library("SDL2") or "libSDL2-2.0.so.0"
    try:
        sdl = ctypes.CDLL(libname)
    except OSError as e:
        out["error"] = f"libSDL2 not available ({e}) — whisper-stream could not run either"
        return out

    sdl.SDL_GetAudioDeviceName.restype = ctypes.c_char_p
    sdl.SDL_GetCurrentAudioDriver.restype = ctypes.c_char_p
    sdl.SDL_GetError.restype = ctypes.c_char_p

    if sdl.SDL_Init(SDL_INIT_AUDIO) != 0:
        out["error"] = (sdl.SDL_GetError() or b"").decode(errors="replace") or "SDL audio init failed"
        return out
    try:
        driver = sdl.SDL_GetCurrentAudioDriver()
        out["driver"] = driver.decode(errors="replace") if driver else None
        count = sdl.SDL_GetNumAudioDevices(SDL_TRUE)
        for i in range(max(0, count)):          # -1 means "can't enumerate", not "none"
            raw = sdl.SDL_GetAudioDeviceName(i, SDL_TRUE)
            out["devices"].append({"id": i, "name": raw.decode(errors="replace") if raw else f"Device {i}"})
        if count < 0:
            out["error"] = "this audio driver cannot enumerate capture devices; only the system default is usable"
        # The dummy driver accepts a capture device and returns silence forever, which looks exactly
        # like a mic that is muted or too quiet. Say so here rather than let it be debugged by ear.
        elif out["driver"] == "dummy":
            out["error"] = "SDL fell back to the 'dummy' audio driver — any capture will be silence"
    finally:
        sdl.SDL_Quit()
    return out


def main():
    json.dump(enumerate_capture_devices(), sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
