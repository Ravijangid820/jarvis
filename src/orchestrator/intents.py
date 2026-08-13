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


# --- "was that ONLY a command?" -----------------------------------------------------------------
# Filler that carries no information once the command itself is understood. Stripping it is what
# lets "can you please turn off the light?" be recognised as a bare command while "I'm freezing,
# turn off the fan" is recognised as a sentence that also said something.
_COMMAND_FILLER = re.compile(
    r"\b(?:please|kindly|thanks|thank you|now|for me|hey|ok|okay|alright|"
    r"jarvis|can|could|would|will|you|your|i|want|need|to|the|my|a|an|and|then|"
    r"just|also|maybe|quickly|again|back|it|that|this|them|"
    r"turn|switch|power|shut|toggle|put|set|run|trigger|execute|activate|launch|start|stop|"
    r"on|off|up|down|is|are|was|were|do|does|did|let|lets|us)\b",
    re.I)
_NON_WORD = re.compile(r"[^a-z0-9\s]+", re.I)


def command_residual(text: str, device_words: str = "") -> str:
    """What the sentence said BEYOND the smart-home command it contains.

    Returns "" when the utterance was purely a command, so the caller can answer instantly with the
    deterministic acknowledgement, and non-empty when the person said something a template cannot
    honour ("I'm feeling cold, turn off the fan" deserves more than "Okay - the Fan is now off.").

    Deliberately crude: it strips control verbs, polite scaffolding and the device's own name, then
    reports whatever is left. Over-reporting a residual costs one LLM turn; under-reporting costs a
    person being answered by a template when they said something real, which is the failure this
    whole change exists to fix.

    `device_words` is the resolved device's display name, whose words are not "extra" either.
    """
    cleaned = _NON_WORD.sub(" ", (text or "").lower())
    for word in (device_words or "").lower().split():
        cleaned = re.sub(rf"\b{re.escape(word)}\b", " ", cleaned)
    cleaned = _COMMAND_FILLER.sub(" ", cleaned)
    return " ".join(cleaned.split())


def says_more_than_command(text: str, device_words: str = "", min_words: int = 2) -> bool:
    """True when the utterance carries enough beyond the command to deserve a composed reply.

    Two words rather than one: a single stray token is usually a filler this list does not know
    or a speech-to-text artifact ("uh"), and promoting those to a full LLM turn would make the
    common case — flipping a light — slow again for no gain.
    """
    return len(command_residual(text, device_words).split()) >= min_words


# --- Greetings ---------------------------------------------------------------
# Kept in step with frontend/src/wake-phrases.js (isGreetingRemainder) and voice_bridge.py's
# _GREETINGS: the same words must get the same answer whether they were typed, heard by the
# browser, or heard by the box's own microphone. Divergence here is invisible and maddening.
_WAKE_PREFIX = re.compile(r"^\s*(?:hey|hi|hello|ok|okay|yo)?\s*,?\s*jarvis\b[\s,.!?-]*", re.I)
_WAKE_SUFFIX = re.compile(r"[\s,.!?-]*\bjarvis\b\s*$", re.I)

# The COMPLETE set of utterances answered without the model, listed exactly. Membership is an
# equality test against this set — there is no decomposition into words, no prefix matching, and
# no length heuristic.
#
# There was, and it is the reason this list is now explicit. The old rule accepted any utterance of
# three words or fewer whose every word merely *began* one of these phrases, which quietly made
# "how are you" a greeting — "how" is a prefix of "howdy", "are" of "are you there", "you" of
# "you there" — so a question about how Jarvis was doing was answered "Yes, sir." Nobody wrote that
# rule intending it; it fell out of a `startswith` that looked harmless.
#
# Everything here is either a bare address or a presence check, where an acknowledgement IS the
# complete and correct answer. Anything else — including "how are you", "how's it going",
# "what's up", "are you ok" — is a question, and questions go to the model. Adding a phrasing here
# saves one LLM turn; adding one wrongly answers a real question with a pleasantry, which is far
# worse and is exactly what happened. When in doubt, leave it out and let the model answer.
GREETING_PHRASES = frozenset({
    "hello", "hi", "hey", "yo", "hiya", "howdy", "greetings", "sup",
    "good morning", "good afternoon", "good evening", "good day",
    "morning", "afternoon", "evening",
    "hello there", "hi there", "hey there", "there",
    "you there", "are you there", "you up", "are you up",
    "you awake", "are you awake", "wake up", "you online", "are you online",
})
# Fragments a speech-to-text pass or a stray keypress leaves behind. A bare "I" was one of the
# messages that produced a recitation of the whole house, because the model had nothing else to
# answer and a device list in front of it.
_NOISE_ONLY = frozenset({"i", "a", "uh", "um", "hm", "hmm", "eh", "ah", "oh", "so", "well",
                         "ok", "okay"})


def is_greeting(text: str) -> bool:
    """True when the message is ONLY a greeting or content-free noise — nothing to answer.

    Exists because a 2B model handed a contentless turn does not stay quiet. Re-measured against
    the real model on this box while fixing the bug above: asked nothing but "Hey Jarvis", with the
    live device block in context, it answered *"Sir, the lights, tube light, and fan are all off."*
    Nobody asked about the lights. So a bare address still never reaches the model.

    What it must NOT do is swallow a question. The same session measured the model answering
    "How are you?" with "I am functioning as expected." and "How's it going?" with "It's running at
    100% efficiency, sir." — real replies, worth the ~20 s they cost. A greeting fast path that
    intercepts those is not saving time, it is replacing a good answer with a worse one.

    So the rule is exact membership in GREETING_PHRASES, and anything else falls through: "hey
    jarvis, turn off the fan" is a command, "hi, what's the weather" is a question, and "how are
    you" is a question.
    """
    stripped = _WAKE_PREFIX.sub("", text or "", count=1)
    # ...and trailing, because half the shipped wake phrases put the name last ("wake up jarvis",
    # "you there jarvis"). Stripping only the prefix left "jarvis" as an unrecognised word and the
    # whole utterance fell through to the model.
    stripped = _WAKE_SUFFIX.sub("", stripped, count=1)
    cleaned = " ".join(_NON_WORD.sub(" ", stripped.lower()).split())
    if not cleaned:
        return bool((text or "").strip())        # the wake word alone ("jarvis") — still an address
    return cleaned in _NOISE_ONLY or cleaned in GREETING_PHRASES


# The spoken acknowledgement, and now the typed one too — they were two different sets, so saying
# "hey Jarvis" out loud got "Good evening, sir." while typing it got "Sir." for no reason anyone
# chose. Never repeats the previous pick: with a dozen options that alone is most of the perceived
# variety, because back-to-back repeats are what the ear and the eye notice, not the size of the
# pool. Never fed back to the model, so the variation cannot teach it to imitate the template
# (the failure that kind='greeting' exists for).
_LAST_ACK: list = []


def greeting_reply(now: Optional[datetime] = None) -> str:
    """A short, time-aware JARVIS acknowledgement — the whole answer to a bare address."""
    import random
    hour = (now or datetime.now()).hour
    part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
    options = [
        "Yes, sir?", "At your service, sir.", "How can I help, sir?",
        f"Good {part}, sir.", "Standing by, sir.", "I'm here, sir.",
        "Listening, sir.", "Sir?", "Go ahead, sir.", "Ready when you are, sir.",
        "You have my attention, sir.", "What can I do for you, sir?",
    ]
    if hour < 5:                    # the small hours deserve their own line
        options += ["Still awake, sir?", "Burning the midnight oil, sir?"]
    pick = random.choice([o for o in options if o not in _LAST_ACK] or options)
    _LAST_ACK.clear()
    _LAST_ACK.append(pick)
    return pick


# Retained as the set a reply is drawn from, for tests and for anything that wants to recognise one.
GREETING_REPLIES = ("Yes, sir?", "At your service, sir.", "How can I help, sir?",
                    "Standing by, sir.", "I'm here, sir.", "Listening, sir.", "Sir?",
                    "Go ahead, sir.", "Ready when you are, sir.", "You have my attention, sir.",
                    "What can I do for you, sir?", "Still awake, sir?",
                    "Burning the midnight oil, sir?",
                    "Good morning, sir.", "Good afternoon, sir.", "Good evening, sir.")
