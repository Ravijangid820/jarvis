"""Voice: text-to-speech, the spoken wake-word acknowledgement, and server microphones.

Two different microphones live here and they are not the same feature:

  /voice/mics       the mic the BOX's own always-on listener (whisper-stream) uses. Admin-only,
                    enumerated through SDL so the ids line up with `whisper-stream -c`.
  /voice/inputs     server microphones offered as a source for a USER's own voice input, streamed
                    to their browser as raw PCM. Any household member, one session at a time.

Neither is the browser's own microphone, which never involves the server at all — since v3.2.0 the
wake word and Whisper both run in the tab.
"""
import asyncio
import json
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import deps
from config import BASE_DIR, logger
from intents import greeting_reply
from llm import synthesize_tts

router = APIRouter(tags=["voice"])


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=600)


class MicSelectRequest(BaseModel):
    # -1 is whisper-stream's own default ("whatever the system calls the default input"), which is
    # the right answer when there is exactly one mic and the useful escape hatch when the list is
    # wrong. Anything >= 0 is an index into the server's SDL capture-device list.
    capture_id: int = Field(..., ge=-1, le=64)


ACTIVE_MIC_PATH = BASE_DIR / "config" / "active_mic.json"


def _read_active_mic() -> Dict[str, Any]:
    """The admin's chosen microphone: {"capture_id": int, "name": str} — or {} if never set."""
    try:
        if ACTIVE_MIC_PATH.exists():
            data = json.loads(ACTIVE_MIC_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "capture_id" in data:
                return data
    except Exception as e:
        logger.warning("could not read %s: %s", ACTIVE_MIC_PATH, e)
    return {}


def _list_capture_devices() -> Dict[str, Any]:
    """Enumerate the box's microphones by running src/scripts/list_mics.py.

    A subprocess rather than an import: enumerating means SDL_Init(AUDIO), which opens a connection
    to the sound server and installs handlers — state this long-lived HTTP process has no business
    holding for the sake of one admin listing. See that script for why SDL is the only enumeration
    whose indices whisper-stream will agree with.
    """
    import subprocess
    # Resolved from THIS file, not BASE_DIR: list_mics.py is code that ships alongside the
    # orchestrator, while BASE_DIR is JARVIS_HOME — a data root that in some deployments holds
    # config and models but no source tree. BASE_DIR stays as the fallback for layouts that
    # relocate the code. Walked up to the "src" directory by NAME rather than by counting parents,
    # so moving this file between src/orchestrator/ and src/orchestrator/routes/ cannot break it.
    _here = Path(__file__).resolve()
    _src = next((a for a in _here.parents if a.name == "src"), _here.parents[2])
    candidates = [_src / "scripts" / "list_mics.py",
                  BASE_DIR / "src" / "scripts" / "list_mics.py"]
    script = next((p for p in candidates if p.exists()), None)
    if script is None:
        return {"driver": None, "devices": [],
                "error": f"list_mics.py is missing from this deployment (looked in {candidates[0].parent})"}
    try:
        proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        # A wedged sound server blocks in SDL_Init rather than returning; don't hang the request.
        return {"driver": None, "devices": [], "error": "audio enumeration timed out after 10s"}
    except Exception as e:
        return {"driver": None, "devices": [], "error": f"could not enumerate audio devices: {e}"}
    if proc.returncode != 0:
        return {"driver": None, "devices": [],
                "error": (proc.stderr or "").strip()[:300] or f"enumeration exited {proc.returncode}"}
    try:
        return json.loads(proc.stdout)
    except Exception:
        return {"driver": None, "devices": [], "error": "audio enumeration returned unreadable output"}


@router.get("/voice/mics")
def list_mics(request: Request):
    """The microphones this server can see, for the admin's mic picker.

    Admin-only: which devices are attached, and what they're called, describes the hardware of the
    box rather than anything a chat user needs.

    `selected` is what the admin picked; `active` on a device means the running listener would use
    it. Selection is matched by NAME as well as index because SDL's indices are positional — unplug
    a webcam and every device after it shifts down one, so a stored bare index can quietly come to
    mean a different microphone. `stale` says the chosen device is not currently present.
    """
    deps.require_admin(request)
    found = _list_capture_devices()
    chosen = _read_active_mic()
    name = chosen.get("name")
    cid = chosen.get("capture_id", -1)
    devices = found.get("devices", [])
    match = next((d for d in devices if d["name"] == name), None) if name else None
    return {
        "mics": [{**d, "active": bool(match and d["id"] == match["id"])} for d in devices],
        "driver": found.get("driver"),
        "error": found.get("error"),
        "selected": {"capture_id": cid, "name": name} if chosen else None,
        # The selection names a device that isn't here right now: the listener will fall back to the
        # system default rather than open whatever has inherited that index.
        "stale": bool(chosen and cid >= 0 and name and match is None),
        "listener_restart_required": True,
    }


@router.post("/voice/mics/select")
def select_mic(req: MicSelectRequest, request: Request):
    """Choose the microphone the wake-word listener should use, from the next restart.

    Like /models/switch, this stages a choice rather than pretending to reconfigure a running
    process: the listener is a separate systemd unit holding an open capture stream, and the honest
    thing is to record the decision and say what has to happen for it to take effect.
    """
    deps.require_admin(request)
    found = _list_capture_devices()
    devices = found.get("devices", [])
    if req.capture_id < 0:
        payload = {"capture_id": -1, "name": None}      # back to the system default
        label = "system default"
    else:
        dev = next((d for d in devices if d["id"] == req.capture_id), None)
        if dev is None:
            raise HTTPException(status_code=404,
                                detail=f"No capture device {req.capture_id} on this server")
        # Store the name alongside the index — the name is what survives a device being unplugged.
        payload = {"capture_id": dev["id"], "name": dev["name"]}
        label = dev["name"]
    try:
        ACTIVE_MIC_PATH.parent.mkdir(parents=True, exist_ok=True)
        ACTIVE_MIC_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not save the microphone selection: {e}")
    deps.audit(request, "voice.mic_select", label)
    return {"status": "restart_required", "selected": payload,
            "message": f"Microphone set to “{label}”. Restart the voice listener to use it."}


# One capture stream at a time. The microphone is a single piece of hardware: a second ffmpeg on
# the same PCM either fails to open it or (with dmix) hands out a degraded duplicate, so the honest
# answer to a second listener is "busy" rather than a stream that quietly misbehaves.
_SERVER_MIC_LOCK = threading.Lock()
_SERVER_MIC_MAX_S = 15 * 60     # hard cap; a forgotten open tab must not hold the mic all day
_PCM_RATE = 16000               # what both the wake detector and Whisper want, so nothing resamples


@router.get("/voice/inputs")
def voice_inputs(request: Request):
    """Microphones attached to the SERVER, offered to any household member as an audio source.

    Distinct from /voice/mics (admin): that one picks the device the always-on wake-word listener
    opens. This one is the "use the microphone next to me" option in a user's own voice input — a
    good USB mic plugged into the box beats a laptop lid array for whoever is sitting near it.

    Devices are ALSA PCM ids from /proc/asound rather than SDL indices: they're stable across
    replug (`plughw:<card>,<dev>` plus a card name), and they're what ffmpeg captures with.
    """
    deps.require_not_demo()          # a visitor must never reach a live feed of someone's room
    found = _list_capture_devices()
    return {"inputs": found.get("alsa", []), "error": found.get("error"),
            "busy": _SERVER_MIC_LOCK.locked()}


def _ffmpeg_reason(stderr: str) -> str:
    """The one useful sentence out of ffmpeg's several lines of ALSA noise.

    A failed open prints a config-library backtrace, the actual cause, and two generic "Error opening
    input" lines. Only the middle one tells anybody anything, and it is the line that distinguishes
    "no /dev/snd in this container" from "another process holds the device"."""
    lines = [" ".join(ln.split()) for ln in (stderr or "").splitlines() if ln.strip()]
    best = next((ln for ln in lines if "cannot open audio device" in ln.lower()), None)
    if best is None:
        best = next((ln for ln in lines if ln.lower().startswith("error")), None)
    if best is None:
        best = lines[-1] if lines else "the device produced no audio"
    best = re.sub(r"^\[[^\]]*\]\s*", "", best)          # strip ffmpeg's "[alsa @ 0x…]" prefix
    return best[:160]


@router.get("/voice/server-mic/stream")
async def stream_server_mic(request: Request, device: str):
    """Stream the server microphone as raw 16 kHz mono PCM (s16le) for the browser to consume.

    The browser wraps this back into a MediaStream, so the *existing* in-tab pipeline — VAD, wake
    word, Whisper — runs unchanged and transcription stays on the user's device. Deliberately not a
    server-side transcription endpoint: v3.2.0 moved STT off this 2011 CPU on purpose, and shipping
    audio instead of text keeps it off.

    This is a live feed of the room the server sits in, so: never in a demo household, one session
    at a time, hard-capped, and written to the audit log.
    """
    deps.require_not_demo()
    # The device string becomes a subprocess argument, so it is matched against the enumerated set
    # rather than sanitised. Anything else — including a value that merely *looks* like a device —
    # is refused, so no caller-supplied text can turn into an ffmpeg flag.
    known = {d["device"] for d in _list_capture_devices().get("alsa", [])}
    if device not in known:
        raise HTTPException(status_code=404, detail=f"No such capture device: {device}")
    if not shutil.which("ffmpeg"):
        raise HTTPException(status_code=503, detail="ffmpeg is not installed on the server")
    if not _SERVER_MIC_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="The server microphone is already in use")

    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "alsa", "-i", device,
            "-ac", "1", "-ar", str(_PCM_RATE), "-f", "s16le", "-"]

    # Wait for the FIRST bytes before committing to a 200. Once a StreamingResponse starts, the
    # status is already on the wire and a failure can only appear as a stream that ends — which is
    # indistinguishable from a silent room. The overwhelmingly common failure here is a container
    # that can see the card in /proc/asound but has no /dev/snd, and ffmpeg says so precisely; that
    # sentence is worth far more to whoever is debugging than an empty 200.
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except Exception as e:
        _SERVER_MIC_LOCK.release()
        raise HTTPException(status_code=503, detail=f"Could not start audio capture: {e}")
    try:
        first = await asyncio.wait_for(proc.stdout.read(4096), timeout=8)
    except asyncio.TimeoutError:
        first = b""
    if not first:
        err = ""
        try:
            err = (await asyncio.wait_for(proc.stderr.read(600), timeout=2)).decode(errors="replace")
        except Exception:
            pass
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        _SERVER_MIC_LOCK.release()
        logger.warning("server mic %s failed to open: %s", device, " ".join(err.split()))
        raise HTTPException(status_code=503, detail=f"Could not open {device}: {_ffmpeg_reason(err)}")

    deps.audit(request, "voice.server_mic", device)

    async def pcm():
        try:
            yield first
            deadline = time.time() + _SERVER_MIC_MAX_S
            while True:
                if await request.is_disconnected() or time.time() > deadline:
                    break
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=5)
                except asyncio.TimeoutError:
                    continue                       # silence is not an error; re-check disconnect
                if not chunk:
                    # ffmpeg exited mid-session (device unplugged, say). The open failure is already
                    # ruled out above, so this is genuinely a stream that stopped; log why and end.
                    err = (await proc.stderr.read(600)).decode(errors="replace").strip()
                    if err:
                        logger.warning("server mic capture ended: %s", err)
                    break
                yield chunk
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
            _SERVER_MIC_LOCK.release()

    return StreamingResponse(pcm(), media_type="audio/L16; rate=16000; channels=1")


@router.post("/tts")
def tts(req: TTSRequest, request: Request):
    """Synthesize speech (Piper) for arbitrary text → base64 WAV. The web UI uses this to speak
    the greeting; the voice bridge uses it for spoken replies."""
    audio = synthesize_tts(req.text.strip())
    if not audio:
        raise HTTPException(status_code=503, detail="TTS unavailable")
    return {"audio": audio}


@router.get("/greeting")
def greeting(request: Request):
    """A JARVIS greeting (text + spoken audio), no LLM. Used by the voice bridge when it hears
    just the wake word ("Jarvis" → "Yes, sir?").

    The wording comes from intents.greeting_reply — the same function the typed path uses. It used
    to be a second, richer set defined here, so saying "hey Jarvis" out loud got "Good evening,
    sir." while typing it got "Sir.", for no reason anyone had chosen.
    """
    text = greeting_reply()
    return {"text": text, "audio": synthesize_tts(text)}
