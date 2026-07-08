"""Offline intent parsing for spoken/typed device commands.

A closed, deterministic vocabulary is far more reliable (and instant) on a small local model than
asking the LLM to call tools — so we match the common phrasings here first and only fall through to
the LLM for anything we don't recognize. Pure functions, no I/O, easy to unit-test.

`parse_volume(text)` → {"action": "set|step|mute|unmute", "value": int?} or None.
  - set  : absolute level 0–100
  - step : signed delta (+ louder / − quieter)
  - mute / unmute : no value
"""
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

DEFAULT_STEP = 10                       # % change for a bare "volume up/down"

_VOL = re.compile(r"\b(volume|sound|audio)\b", re.I)            # is this about audio at all?
_NUM = re.compile(r"(\d{1,3})")
_UP = re.compile(r"\b(up|louder|raise|increase|higher|crank|boost)\b", re.I)
_DOWN = re.compile(r"\b(down|quieter|softer|lower|decrease|reduce)\b", re.I)
_MAX = re.compile(r"\b(max|maximum|full|loudest|all the way)\b", re.I)
_MIN = re.compile(r"\b(min|minimum|lowest|zero)\b", re.I)

_GESTURE = re.compile(r"\b(gesture|gestures|hand|hands)\b", re.I)
_BARE_VOLUME = {"volume", "the volume", "volume control", "control volume",
                "control the volume", "volume please", "volume mode", "control my volume"}


_REMINDER_KW = re.compile(r"\b(remind|reminder|timer|alarm|wake me)\b", re.I)
_DUR = re.compile(r"(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|\bh\b|\bm\b|\bs\b)", re.I)
_AT = re.compile(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?", re.I)


def parse_reminder(text: str, now: datetime) -> Optional[Dict[str, Any]]:
    """Parse a reminder/timer request → {'due_at': datetime, 'text': str} or None.
    Handles 'remind me [to X] in N min', 'timer for N', 'remind me to X at 6pm', 'wake me at 7:30'."""
    if not text or not _REMINDER_KW.search(text):
        return None
    t = text.lower()

    total = 0
    for m in _DUR.finditer(t):
        n, u = int(m.group(1)), m.group(2)
        if u.startswith(("h", "hr")):
            total += n * 3600
        elif u.startswith(("m", "min")):
            total += n * 60
        else:
            total += n
    due = None
    if total > 0 and re.search(r"\b(in|for)\b", t):
        due = now + timedelta(seconds=total)
    else:
        m = _AT.search(t)
        if m:
            hh, mm = int(m.group(1)), int(m.group(2) or 0)
            ap = (m.group(3) or "").replace(".", "")
            if ap == "pm" and hh < 12:
                hh += 12
            if ap == "am" and hh == 12:
                hh = 0
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if due <= now:
                    due += timedelta(days=1)
    if due is None or due <= now:
        return None

    body = None
    m = re.search(r"\b(?:to|that)\s+(.+)", text, re.I)   # original case for the body
    if m:
        body = re.sub(r"\s*\b(in|at|for)\b\s+[\w:.\s]*$", "", m.group(1), flags=re.I).strip()
    body = (body or ("Timer" if "timer" in t else "Reminder")).rstrip(".!?") or "Reminder"
    return {"due_at": due, "text": body[:200]}


def is_gesture_volume(text: str) -> bool:
    """True if the user wants HAND-GESTURE volume control (as opposed to a concrete set/step/mute,
    which parse_volume handles and should be checked first). e.g. "volume", "volume control",
    "control the volume with gestures", "hand volume"."""
    if not text:
        return False
    t = re.sub(r"\s+", " ", re.sub(r"[^a-z ]", "", text.lower())).strip()
    if t in _BARE_VOLUME:
        return True
    return bool(_VOL.search(t) and _GESTURE.search(t))


def parse_volume(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    t = text.lower().strip()
    has_vol = bool(_VOL.search(t))

    # mute / unmute first ("unmute" contains "mute", so check it first)
    if re.search(r"\bunmute\b", t) or (has_vol and re.search(r"\bback on\b", t)):
        return {"action": "unmute"}
    if re.search(r"\bmute\b", t) or re.search(r"\bsilenc\w+\b", t):
        return {"action": "mute"}

    # From here we need it to be about audio — except for unambiguous words — so we don't fire on
    # things like "turn up the heat" or "lower the blinds".
    unambiguous = re.search(r"\b(louder|quieter|softer)\b", t)
    if not has_vol and not unambiguous:
        return None

    if has_vol and _MAX.search(t):
        return {"action": "set", "value": 100}
    if has_vol and (_MIN.search(t) or re.search(r"\bhalf\b", t)):
        return {"action": "set", "value": 50 if re.search(r"\bhalf\b", t) else 0}

    up, down = bool(_UP.search(t)), bool(_DOWN.search(t))
    m = _NUM.search(t)
    n = int(m.group(1)) if m else None

    if up or down:                       # relative change (optional explicit amount)
        amt = max(1, min(n if n is not None else DEFAULT_STEP, 100))
        return {"action": "step", "value": amt if up else -amt}
    if n is not None:                    # "volume 40", "set volume to 40 percent"
        return {"action": "set", "value": max(0, min(n, 100))}
    return None


# --- Smart home (Home Assistant) --------------------------------------------
# "turn on the test light", "switch the desk fan off", "toggle kitchen light",
# "is the test light on?" → {"action": "on|off|toggle|status", "device": str} or None.
# The caller only ACTS when the device resolves against the HA allowlist — a non-matching
# device falls through to the LLM, so ordinary sentences are never hijacked.
# The device phrase ends at a CLAUSE boundary, not the end of the sentence — people wrap commands
# in context ("turn on the fan, i am feeling hot" / "…because it's warm" / "…please").
_BOUND = r"(?=\s*(?:[,.;:!?]|$)|\s+(?:because|since|cause|so|as|while|please|right\s+now|now|thanks|already|yet|anymore|for\s+me)\b)"
_HOME_ON_A = re.compile(r"\b(?:turn|switch|power)\s+on\s+(?:the\s+|my\s+)?(?P<dev>[\w -]+?)" + _BOUND, re.I)
_HOME_ON_B = re.compile(r"\b(?:turn|switch|power)\s+(?:the\s+|my\s+)?(?P<dev>[\w -]+?)\s+(?:back\s+)?on" + _BOUND, re.I)
_HOME_OFF_A = re.compile(r"\b(?:turn|switch|power|shut)\s+off\s+(?:the\s+|my\s+)?(?P<dev>[\w -]+?)" + _BOUND, re.I)
_HOME_OFF_B = re.compile(r"\b(?:turn|switch|power|shut)\s+(?:the\s+|my\s+)?(?P<dev>[\w -]+?)\s+(?:back\s+)?off" + _BOUND, re.I)
_HOME_TOGGLE = re.compile(r"\btoggle\s+(?:the\s+|my\s+)?(?P<dev>[\w -]+?)" + _BOUND, re.I)
_HOME_STATUS = re.compile(r"\b(?:is|are)\s+(?:the\s+|my\s+)?(?P<dev>[\w -]+?)\s+(?:on|off|running)" + _BOUND, re.I)
_HOME_RUN = re.compile(r"\b(?:run|trigger|execute|activate|launch|start)\s+(?:the\s+|my\s+)?(?P<dev>[\w -]+?)" + _BOUND, re.I)
_HOME_STOP = re.compile(r"\b(?:stop|halt|kill|cancel)\s+(?:the\s+|my\s+)?(?P<dev>[\w -]+?)" + _BOUND, re.I)
_HOME_ENABLE = re.compile(r"\b(?:enable|arm)\s+(?:the\s+|my\s+)?(?P<dev>[\w -]+?)" + _BOUND, re.I)
_HOME_DISABLE = re.compile(r"\b(?:disable|disarm)\s+(?:the\s+|my\s+)?(?P<dev>[\w -]+?)" + _BOUND, re.I)

# Any control-ish verb — used by the anti-bluff guard: a message that mentions an allowlisted device
# AND one of these, but doesn't parse as a clean command, gets a clarification — it must never fall
# through to the (toolless, streaming) LLM, which bluffs acks like "Done."
HOME_CONTROL_VERB = re.compile(
    r"\b(?:turn|switch|power|shut|toggle|run|trigger|execute|activate|launch|start|stop|halt|kill|enable|disable|arm|disarm)\b", re.I)


def parse_home_command(text: str) -> Optional[Dict[str, str]]:
    """Deterministic parse of common smart-home phrasings; None when it isn't one."""
    if not text or _VOL.search(text):        # audio commands belong to the volume intent
        return None
    for action, pattern in (("toggle", _HOME_TOGGLE), ("on", _HOME_ON_A), ("on", _HOME_ON_B),
                            ("off", _HOME_OFF_A), ("off", _HOME_OFF_B), ("status", _HOME_STATUS),
                            ("run", _HOME_RUN), ("stop", _HOME_STOP),
                            ("on", _HOME_ENABLE), ("off", _HOME_DISABLE)):
        m = pattern.search(text)
        if m:
            dev = m.group("dev").strip()
            if 0 < len(dev) <= 60:
                return {"action": action, "device": dev}
    return None
