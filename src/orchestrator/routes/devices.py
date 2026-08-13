"""Device control: volume, the gesture mode, the agent command queue, and Home Assistant.

Also the LLM tool layer, which lives here because every tool it exposes is a device action. The
rule the whole file is built around: **the model only ever proposes.** Whatever route an intent
arrives by — a regex fast-path, the semantic router, a tool call the model emitted — it lands in
the same executor and passes the same gates: deps.can_control_devices, the optional presence
gate, the Home Assistant entity allowlist, and the audit log. An ambiguous device name is refused,
never guessed.
"""
import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

import deps
import ha
import intent_router
import memory
from config import REQUIRE_PRESENCE_FOR_CONTROL, JARVIS_MODE, logger
from db import get_db
from intents import (HOME_CONTROL_VERB, is_gesture_volume, parse_home_command, parse_reminder,
                     parse_volume, says_more_than_command)
from llm import request_llm_tools

router = APIRouter(tags=["devices"])


VOICE_DEVICE = "laptop"
VOICE_CAMERA = "laptop-cam"      # camera a spoken "volume" (gesture) request engages

# Gesture-volume mode: a spoken "Jarvis, volume" opens a short, voice-authorized window during which
# the camera reports hand height and the SERVER maps movement → volume steps (so the camera key needs
# no device-control permission). State is in-memory; entries expire on their own.
_GESTURE_MODES: Dict[str, Dict[str, Any]] = {}   # camera_device_id -> {expires, last_y, target}
_GESTURE_TTL_S = 12.0            # mode lifetime, refreshed on each hand report
_GESTURE_GAIN = 110              # normalized Δy (0..1) → volume %  (~half-frame swing ≈ 55%)
_GESTURE_DEADZONE = 0.015        # ignore sub-threshold jitter
_GESTURE_STEP_CLAMP = 25         # max % change per report (smoothness / anti-jump)


class VolumeRequest(BaseModel):
    action: str = Field(..., max_length=16)        # set | step | mute | unmute
    value: Optional[int] = Field(default=None, ge=-100, le=100)
    device: str = Field(default="laptop", max_length=64)


class GestureReport(BaseModel):
    y: float = Field(..., ge=0.0, le=1.0)          # normalized hand height (0=top, 1=bottom of frame)


def _enqueue_volume(household_id: int, action: str, value: Optional[int], device: str) -> int:
    """Validate + enqueue one volume command (the tiny vocabulary set|step|mute|unmute). Shared by
    the REST endpoint and the voice fast-path. Raises HTTPException on a bad command. NOT an authz
    check — callers must gate on deps.can_control_devices first."""
    action = (action or "").lower()
    params: Dict[str, Any] = {}
    if action == "set":
        if value is None or not (0 <= value <= 100):
            raise HTTPException(status_code=400, detail="set requires value 0–100")
        params = {"value": value}
    elif action == "step":
        if value is None:
            raise HTTPException(status_code=400, detail="step requires value (-100…100)")
        params = {"value": max(-100, min(value, 100))}
    elif action not in ("mute", "unmute"):
        raise HTTPException(status_code=400, detail="action must be set|step|mute|unmute")
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO device_commands (household_id, device_id, action, params) VALUES (?, ?, ?, ?)",
            (household_id, device, action, json.dumps(params)))
        # Retention: drop delivered commands older than a day so the queue doesn't grow forever.
        conn.execute("DELETE FROM device_commands WHERE status='delivered' AND delivered_at < datetime('now','-1 day')")
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _spoken_volume_ack(action: str, value: Optional[int]) -> str:
    """A short, speakable confirmation for the voice path."""
    if action == "set":
        return f"Volume set to {value} percent."
    if action == "step":
        return f"Turning it {'up' if (value or 0) >= 0 else 'down'} by {abs(value or 0)} percent."
    if action == "mute":
        return "Muted."
    if action == "unmute":
        return "Unmuted."
    return "Done."


def _open_gesture_mode(household_id: int, camera: str, target: str) -> None:
    """Authorize a time-boxed gesture→volume window for `camera` and signal it via the command
    channel (long-poll). The server-side mode entry gates POST /devices/gesture."""
    now = time.time()
    _GESTURE_MODES[camera] = {"expires": now + _GESTURE_TTL_S, "last_y": None, "target": target}
    for k in [k for k, v in _GESTURE_MODES.items() if v["expires"] < now]:   # prune stale
        _GESTURE_MODES.pop(k, None)
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO device_commands (household_id, device_id, action, params) VALUES (?, ?, ?, ?)",
            (household_id, camera, "gesture_mode",
             json.dumps({"mode": "volume", "ttl": int(_GESTURE_TTL_S)})))
        conn.execute("DELETE FROM device_commands WHERE status='delivered' AND delivered_at < datetime('now','-1 day')")
        conn.commit()
    finally:
        conn.close()


def _handle_volume_command(user_text: str, raw_request: Request) -> Optional[str]:
    """If user_text is a recognized volume command, authorize + act on it and return a short spoken
    ack; otherwise None (→ caller falls through to the LLM). Shared by /inbox and /chat/stream."""
    vol = parse_volume(user_text)
    is_gesture = vol is None and is_gesture_volume(user_text)
    if (vol is not None or is_gesture) and JARVIS_MODE == "demo":
        return "Hardware device control is disabled in public Demo Mode."
    if (vol is not None or is_gesture) and REQUIRE_PRESENCE_FOR_CONTROL and not deps.authorized_person_present(deps.household(raw_request)):
        return "I don't see anyone authorized in the room, so I can't change that right now."
    if vol is not None:
        if not deps.can_control_devices(raw_request):
            return "Sorry — you're not authorized to control devices."
        _enqueue_volume(deps.household(raw_request), vol["action"], vol.get("value"), VOICE_DEVICE)
        deps.audit(raw_request, "device.volume", f"{vol['action']} {vol.get('value', '')}".strip())
        return _spoken_volume_ack(vol["action"], vol.get("value"))
    if is_gesture_volume(user_text):                 # "Jarvis, volume" → hand-gesture control
        if not deps.can_control_devices(raw_request):
            return "Sorry — you're not authorized to control devices."
        _open_gesture_mode(deps.household(raw_request), VOICE_CAMERA, VOICE_DEVICE)
        deps.audit(raw_request, "device.gesture_mode", VOICE_CAMERA)
        return "Gesture volume control on — raise or lower your hand."
    return None


# Anaphora for the fast-path: "switch it off" → the device this session touched last. In-memory
# (like presence state); a restart just means naming the device once again.
_LAST_HOME_ENTITY: Dict[str, Any] = {}   # session_id -> (entity_id, monotonic seconds)
_HOME_PRONOUNS = {"it", "that", "this", "that one", "them"}
_LAST_HOME_TTL = 900                     # "it" stays meaningful for 15 minutes

# Semantic-router proposals awaiting a yes/no ("Should I turn on the fan?"). Per session, short TTL.
_PENDING_HOME: Dict[str, Any] = {}       # session_id -> (entity_id, action, monotonic seconds)
_PENDING_TTL = 120
_YES_RE = re.compile(r"^\s*(yes|yeah|yep|sure|ok|okay|please do|do it|go ahead|go for it)\b", re.I)
_NO_RE = re.compile(r"^\s*(no|nah|nope|don'?t|do not|cancel|leave it|never mind|nevermind)\b", re.I)


def _ha_act(entity: str, action: str):
    """Execute a validated (allowlisted) action. "run" EXECUTES automations/scripts/scenes; "stop"
    aborts them (automation stays enabled). For plain devices run→on and stop→off.

    Returns (ok, effective_action, error_reply) — the effective action drives the reply wording,
    and error_reply is a ready-to-speak sentence when ok is False.

    Pre-flights the entity, because HA's generic turn_on/turn_off answer 200 with an empty body
    for an entity_id it does not have. Without this, an entity renamed or deleted in Home
    Assistant reported a confident success while doing nothing — the most corrosive failure mode
    there is for something you talk to, because nothing in the reply hints that it lied.
    """
    status, _state = ha.probe_entity(entity)
    if status == ha.ENTITY_MISSING:
        logger.warning("HA entity %s is allowlisted but Home Assistant does not have it", entity)
        return False, action, (
            f"Home Assistant doesn't have {ha.display_name(entity)} any more — it looks renamed or "
            f"deleted there. I've left it alone. You can remove it in the Smart Home settings.")
    if status == ha.HA_UNREACHABLE:
        return False, action, "I couldn't reach Home Assistant to do that."

    # Whatever happens below, the cached device snapshot no longer describes this house — drop it
    # so the next prompt reads live state. Invalidated on failure too: after an action we could not
    # confirm, a stale "on" is exactly the assertion we least want the model repeating.
    if action == "run":
        if entity.partition(".")[0] in ha.RUNNABLE_DOMAINS:
            ok = ha.run(entity)
            ha.invalidate_snapshot()
            return ok, "run", None if ok else "I couldn't reach Home Assistant to do that."
        action = "on"
    elif action == "stop":
        if entity.partition(".")[0] in ("automation", "script"):
            ok = ha.stop(entity)
            ha.invalidate_snapshot()
            return ok, "stop", None if ok else "I couldn't reach Home Assistant to do that."
        action = "off"
    ok = ha.turn(entity, action)
    ha.invalidate_snapshot()
    return ok, action, None if ok else "I couldn't reach Home Assistant to do that."


def _ha_label(entity: str):
    """("the morning automation", domain, "morning") — appends the kind for automations/scripts/
    scenes unless the name already contains it.

    Uses the entity's friendly name, so replies and clarifying questions name the device the way
    the speaker does ("the Fan") instead of reciting its id ("the 4node smart switch switch 3")."""
    domain, _, _obj = entity.partition(".")
    nice = ha.display_name(entity)
    kind = domain if domain in ("automation", "script", "scene") else None
    label = f"the {nice}" if (kind is None or kind in nice.lower()) else f"the {nice} {kind}"
    return label, domain, nice


def _ha_reply(entity: str, action: str) -> str:
    """A reply that says what actually HAPPENED, in the entity's own terms — 'disabled' for an
    automation is a different fact than 'off' for a light."""
    label, domain, _ = _ha_label(entity)
    if domain == "automation":
        return {
            "on": f"Okay — {label} is enabled and will run on its triggers.",
            "off": f"Okay — {label} is disabled. It won't run until you enable it again.",
            "stop": f"Okay — I stopped {label}'s current run. It stays enabled for next time.",
            "run": f"Okay — running {label} now.",
            "toggle": f"Okay — {label} was toggled.",
        }[action]
    if domain == "script":
        return {
            "on": f"Okay — I ran {label}.", "run": f"Okay — I ran {label}.",
            "off": f"Okay — {label} was stopped.", "stop": f"Okay — {label} was stopped.",
            "toggle": f"Okay — {label} was toggled.",
        }[action]
    if domain == "scene":
        return f"Okay — {label} is applied."
    verb = {"on": "is now on", "off": "is now off", "toggle": "was toggled"}.get(action, action)
    return f"Okay — {label} {verb}."


def _ha_state_phrase(entity: str, st: dict) -> str:
    """Status wording in the entity's own terms (automation: enabled/disabled; script: running)."""
    label, domain, nice = _ha_label(entity)
    state = st.get("state")
    if domain == "automation":
        return f"{label.capitalize()} is {'enabled' if state == 'on' else 'disabled'}."
    if domain == "script":
        return f"{label.capitalize()} is {'running right now' if state == 'on' else 'not running'}."
    name = (st.get("attributes") or {}).get("friendly_name") or nice
    return f"{name} is {state}."


def _home_says_more(user_text: str, session_id: str) -> bool:
    """Did this message say anything beyond the smart-home command it contained?

    Bare commands keep the instant templated reply — that speed is the point of the fast path, and
    on this hardware an LLM turn for "turn off the light" would cost seconds. A message that also
    said something ("I'm feeling cold, turn off the fan") gets a composed reply instead, because a
    template answering only half of what someone said is exactly what makes an assistant feel
    mechanical.
    """
    ent = (_LAST_HOME_ENTITY.get(session_id) or (None,))[0]
    return says_more_than_command(user_text, ha.display_name(ent) if ent else "")


def _handle_home_command(user_text: str, raw_request: Request, session_id: str) -> Optional[str]:
    """Deterministic smart-home fast-path (shared by /inbox and /chat/stream): "turn on the X",
    "toggle X", "is X on?" — instant, no LLM. Only acts when the device RESOLVES against the HA
    allowlist; anything else returns None and falls through to the LLM, so ordinary sentences
    ("turn my life around") are never hijacked. "it"/"that" refers to the session's last device."""
    if not ha.configured() or not deps.owns_smart_home(raw_request):
        return None            # no smart home for this household → fall through to the LLM
    if JARVIS_MODE == "demo":
        return "Home Assistant control is disabled in public Demo Mode."

    # 0) A semantic proposal awaiting yes/no? ("Should I turn on the fan?")
    pending = _PENDING_HOME.pop(session_id, None)
    if pending is not None and (time.monotonic() - pending[2]) < _PENDING_TTL:
        p_entity, p_action, _ = pending
        if _YES_RE.match(user_text):
            if not deps.can_control_devices(raw_request):
                return "Sorry — you're not authorized to control devices."
            if REQUIRE_PRESENCE_FOR_CONTROL and not deps.authorized_person_present(deps.household(raw_request)):
                return "I don't see anyone authorized in the room, so I can't change that right now."
            ok, eff, err = _ha_act(p_entity, p_action)
            if not ok:
                return err
            _LAST_HOME_ENTITY[session_id] = (p_entity, time.monotonic())
            deps.audit(raw_request, "device.home_assistant", f"{p_action} {p_entity} (semantic, confirmed)")
            return _ha_reply(p_entity, eff)
        if _NO_RE.match(user_text):
            return "Okay — leaving it as is."
        # any other message: drop the proposal silently and process the message normally

    def semantic_route():
        # 2nd understanding layer: MEANING, not phrasings — embeds the utterance and compares it to
        # per-device exemplars ("i'm melting in here" ≈ fan on). Costs one query embed (~175 ms);
        # negligible next to the LLM turn it replaces or precedes.
        if not intent_router.ready():
            return None
        r = intent_router.route(user_text, memory.embed_query)
        if r is None:
            return None
        if not deps.can_control_devices(raw_request):
            return None                    # don't tease users who can't control devices anyway
        label, _, _ = _ha_label(r["entity"])
        if r["decision"] == "act":
            if REQUIRE_PRESENCE_FOR_CONTROL and not deps.authorized_person_present(deps.household(raw_request)):
                return "I don't see anyone authorized in the room, so I can't change that right now."
            ok, eff, err = _ha_act(r["entity"], r["action"])
            if not ok:
                return err
            _LAST_HOME_ENTITY[session_id] = (r["entity"], time.monotonic())
            deps.audit(raw_request, "device.home_assistant",
                   f"{r['action']} {r['entity']} (semantic, {r['score']})")
            return _ha_reply(r["entity"], eff)
        _PENDING_HOME[session_id] = (r["entity"], r["action"], time.monotonic())
        verb = "run" if r["action"] == "run" else f"turn {r['action']}"
        return f"Should I {verb} {label}?"

    def tool_or_clarify():
        # Last two layers before the LLM. The message names an allowlisted device AND uses a
        # control verb, but no clean (action, device) came out of the rules or the router.
        #
        # Returning None here used to hand the turn to the STREAMING LLM, which is offered no
        # tools at all — so it invented acks ("Done.") while nothing happened. That is the gap
        # this closes: give the model the home tools for one round-trip and execute what it
        # calls; only if it calls nothing do we fall back to the canned question.
        #
        # Ordinary sentences (no control verb, or no device named) never reach this and so never
        # pay for the extra call.
        if not HOME_CONTROL_VERB.search(user_text):
            return None
        hinted = ha.resolve_entity(user_text)          # fuzzy match over the whole utterance
        if hinted is None:
            return None
        acted = _home_tool_roundtrip(user_text, raw_request)
        if acted is not None:
            return acted
        nice = ha.display_name(hinted)
        return (f"I think you want me to control {nice} — try \"turn on/off {nice}\", "
                f"\"run {nice}\", or \"stop {nice}\".")

    cmd = parse_home_command(user_text)
    if cmd is None:
        return semantic_route() or tool_or_clarify()
    if cmd["device"].lower() in _HOME_PRONOUNS:
        last = _LAST_HOME_ENTITY.get(session_id)
        entity = last[0] if last and (time.monotonic() - last[1]) < _LAST_HOME_TTL else None
        if entity is None:
            # A device-shaped command with no referent: ASK — never fall through to the LLM,
            # which (toolless on the streaming path) would confidently pretend it acted.
            allowed = ", ".join(ha.display_name(e) for e in ha.HA_ALLOWED_ENTITIES)
            return f"Which device do you mean? I can control: {allowed or 'nothing yet'}."
    else:
        entity = ha.resolve_entity(cmd["device"])
    if entity is None:
        return semantic_route() or tool_or_clarify()   # meaning, then tools, then the ask; else LLM
    if not deps.can_control_devices(raw_request):
        return "Sorry — you're not authorized to control devices."
    if REQUIRE_PRESENCE_FOR_CONTROL and not deps.authorized_person_present(deps.household(raw_request)):
        return "I don't see anyone authorized in the room, so I can't change that right now."
    if cmd["action"] == "status":
        # Same distinction as _ha_act: a device HA no longer has is a stale allowlist entry the
        # user can fix, not an outage.
        st_status, st = ha.probe_entity(entity)
        if st_status == ha.ENTITY_MISSING:
            return (f"Home Assistant doesn't have {ha.display_name(entity)} any more — it looks "
                    f"renamed or deleted there. You can remove it in the Smart Home settings.")
        if not st:
            return "I couldn't reach Home Assistant."
        _LAST_HOME_ENTITY[session_id] = (entity, time.monotonic())
        return _ha_state_phrase(entity, st)
    ok, eff, err = _ha_act(entity, cmd["action"])
    if not ok:
        return err
    _LAST_HOME_ENTITY[session_id] = (entity, time.monotonic())
    deps.audit(raw_request, "device.home_assistant", f"{cmd['action']} {entity} (fast-path)")
    return _ha_reply(entity, eff)


# ---- LLM tool-calling (voice path). Rule fast-paths still run first; this catches phrasings they
# miss, and is where new actions (e.g. lights) plug in. Single round-trip + templated confirmation. ----
TOOLS_SPEC = [
    {"type": "function", "function": {
        "name": "set_volume", "description": "Set or change the speaker volume.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["set", "step", "mute", "unmute"],
                       "description": "set=absolute level, step=relative change, mute/unmute"},
            "value": {"type": "integer", "description": "0-100 for set; positive/negative for step"}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "create_reminder", "description": "Create a reminder/timer that fires after some minutes.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "what to remind about"},
            "in_minutes": {"type": "integer", "description": "minutes from now"}},
            "required": ["in_minutes"]}}},
    {"type": "function", "function": {
        "name": "get_presence", "description": "Who the cameras currently recognize as present.",
        "parameters": {"type": "object", "properties": {}}}},
]

# HA tools are kept SEPARATE and merged per-request by _active_tools(raw_request): HA config is runtime-mutable
# (admin UI / DB), so an import-time `if ha.configured()` would freeze the menu — configuring HA via
# the UI would never expose the tools to the model (the v2.5.0 bug).
HA_TOOLS = [
    {"type": "function", "function": {
        "name": "home_control",
        "description": "Control a smart-home device (light, switch, plug) — on/off/toggle — or RUN an automation, script, or scene.",
        "parameters": {"type": "object", "properties": {
            "device": {"type": "string", "description": "which device/automation, e.g. 'kitchen light', 'movie night'"},
            "action": {"type": "string", "enum": ["on", "off", "toggle", "run", "stop"]}},
            "required": ["device", "action"]}}},
    {"type": "function", "function": {
        "name": "home_status",
        "description": "Get the current on/off state of the smart-home devices.",
        "parameters": {"type": "object", "properties": {
            "device": {"type": "string", "description": "one device; omit for all"}}}}},
]


def _active_tools(raw_request: Request):
    """The tool menu offered to the model on THIS request — reflects live HA config AND whether the
    caller's household owns the smart home. Withholding the tools (rather than refusing the call
    afterwards) means a demo session has no vocabulary for home control at all."""
    return TOOLS_SPEC + (HA_TOOLS if (ha.configured() and deps.owns_smart_home(raw_request)) else [])


def _tool_set_volume(args, raw_request):
    if not deps.can_control_devices(raw_request):
        return "Sorry — you're not authorized to control devices."
    if REQUIRE_PRESENCE_FOR_CONTROL and not deps.authorized_person_present(deps.household(raw_request)):
        return "I don't see anyone authorized in the room, so I can't change that right now."
    action, value = str(args.get("action", "set")).lower(), args.get("value")
    try:
        _enqueue_volume(deps.household(raw_request), action, value, VOICE_DEVICE)
    except HTTPException:
        return "I couldn't make that volume change."
    deps.audit(raw_request, "device.volume", f"{action} {value if value is not None else ''} (tool)".strip())
    return _spoken_volume_ack(action, value)


def _tool_create_reminder(args, raw_request):
    try:
        mins = int(args.get("in_minutes"))
    except (TypeError, ValueError):
        mins = 0
    if mins <= 0:
        return "When would you like to be reminded?"
    text = (str(args.get("text") or "Reminder")).strip()[:200] or "Reminder"
    due = datetime.now() + timedelta(minutes=mins)
    conn = get_db()
    try:
        conn.execute("INSERT INTO reminders (user_id, text, due_at) VALUES (?, ?, ?)",
                     (raw_request.state.user_id, text, due.strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    finally:
        conn.close()
    deps.audit(raw_request, "reminder.create", f"{text} @ {due.strftime('%Y-%m-%d %H:%M')} (tool)")
    if text in ("Timer", "Reminder"):
        return f"{text} set for {due.strftime('%H:%M')}."
    return f"Okay — I'll remind you to {text} at {due.strftime('%H:%M')}."


def _tool_get_presence(args, raw_request):
    names = memory.get_present_people(deps.household(raw_request))
    return ("I can see " + ", ".join(names) + ".") if names else "I don't see anyone right now."


def _tool_home_control(args, raw_request):
    if not deps.can_control_devices(raw_request):
        return "Sorry — you're not authorized to control devices."
    if REQUIRE_PRESENCE_FOR_CONTROL and not deps.authorized_person_present(deps.household(raw_request)):
        return "I don't see anyone authorized in the room, so I can't change that right now."
    deps.require_smart_home(raw_request)
    action = str(args.get("action", "")).lower()
    entity = ha.resolve_entity(str(args.get("device", "")))
    if entity is None:
        allowed = ", ".join(ha.display_name(e) for e in ha.HA_ALLOWED_ENTITIES)
        return f"I'm not sure which device you mean. I can control: {allowed or 'nothing yet — the allowlist is empty'}."
    ok, eff, err = _ha_act(entity, action)
    if not ok:
        return err
    deps.audit(raw_request, "device.home_assistant", f"{action} {entity} (tool)")
    return _ha_reply(entity, eff)


def _tool_home_status(args, raw_request):
    if not deps.can_control_devices(raw_request):
        return "Sorry — you're not authorized to view device states."
    deps.require_smart_home(raw_request)
    device = str(args.get("device") or "").strip()
    entities = [ha.resolve_entity(device)] if device else list(ha.HA_ALLOWED_ENTITIES)
    if not entities or entities[0] is None:
        return "I'm not sure which device you mean."
    parts = []
    for ent in entities:
        st = ha.get_state(ent)
        if st:
            parts.append(_ha_state_phrase(ent, st).rstrip("."))
    return ("; ".join(parts) + ".") if parts else "I couldn't reach Home Assistant."


_TOOLS = {"set_volume": _tool_set_volume, "create_reminder": _tool_create_reminder,
          "get_presence": _tool_get_presence,
          "home_control": _tool_home_control, "home_status": _tool_home_status}


def _run_tool_calls(message: Dict[str, Any], raw_request: Request) -> Optional[str]:
    """Execute the first tool call in an assistant message; return a spoken reply, or None if none."""
    calls = message.get("tool_calls") or []
    if not calls:
        return None
    fn = calls[0].get("function", {})
    handler = _TOOLS.get(fn.get("name"))
    if not handler:
        return None
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except (ValueError, TypeError):
        args = {}
    try:
        return handler(args, raw_request)
    except Exception as e:
        logger.warning("tool %s failed: %s", fn.get("name"), e)
        return None


def _home_tool_roundtrip(user_text: str, raw_request: Request) -> Optional[str]:
    """Offer the model the HOME tools for one round-trip and execute whatever it calls.

    This is the layer that makes the web UI behave like the voice path. /inbox always called the
    LLM with tools; /chat/stream called it with none, so any phrasing the rules and the semantic
    router both missed reached a toolless model — which had no way to act and answered as if it
    had. Measured on this box, the 2B model emits clean calls for exactly these utterances
    (home_control{"device":"fan","action":"on"}), so the understanding was there all along; only
    the wiring was missing.

    Authority is unchanged. The executor (_tool_home_control) re-runs the same
    can-control-devices, presence and allowlist gates and writes the same audit entry, so this
    widens what Jarvis UNDERSTANDS without widening what it is permitted to do. Returns None when
    the model calls nothing, so the caller can fall back.
    """
    messages = [
        {"role": "system",
         "content": "You control a smart home. If the user is asking to change or check a device, "
                    "call the appropriate tool. Otherwise say nothing. /no_think"},
        {"role": "user", "content": user_text},
    ]
    try:
        resp = request_llm_tools(messages, HA_TOOLS, temperature=0.1, n_predict=160)
    except Exception as e:
        logger.warning("home tool round-trip failed: %s", e)
        return None
    msg = (resp.get("choices") or [{}])[0].get("message", {})
    return _run_tool_calls(msg, raw_request)


def _handle_reminder(user_text: str, raw_request: Request) -> Optional[str]:
    """If user_text is a reminder/timer, store it for the caller and return a confirmation; else None."""
    now = datetime.now()
    r = parse_reminder(user_text, now)
    if r is None:
        return None
    due = r["due_at"]
    conn = get_db()
    try:
        conn.execute("INSERT INTO reminders (user_id, text, due_at) VALUES (?, ?, ?)",
                     (raw_request.state.user_id, r["text"], due.strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    finally:
        conn.close()
    deps.audit(raw_request, "reminder.create", f"{r['text']} @ {due.strftime('%Y-%m-%d %H:%M')}")
    when = due.strftime("%H:%M")
    if r["text"] in ("Timer", "Reminder"):
        return f"{r['text']} set for {when}."
    return f"Okay — I'll remind you to {r['text']} at {when}."


@router.post("/devices/volume")
def queue_volume(req: VolumeRequest, request: Request):
    """Enqueue a volume command for a device agent (e.g. the Windows volume agent).

    Authorization is enforced here against the caller's identity/permissions; the command is
    a tiny validated vocabulary (no shell, no free text). The device agent pulls + executes it.
    """
    deps.require_not_demo()
    if not deps.can_control_devices(request):
        raise HTTPException(status_code=403, detail="Not authorized to control devices")
    cmd_id = _enqueue_volume(deps.household(request), req.action, req.value, req.device)
    deps.audit(request, "device.volume", f"{req.action} {req.value or ''} -> {req.device}".strip())
    return {"status": "ok", "id": cmd_id}


@router.post("/devices/gesture")
def report_gesture(req: GestureReport, request: Request):
    """The camera reports normalized hand height while in gesture mode; the server maps movement to
    volume steps for the mode's target. Gated by an active, voice-authorized mode for THIS camera, so
    the camera key needs no device-control permission. Returns {active} so the camera knows when to stop."""
    deps.require_not_demo()
    dev = getattr(request.state, "device_id", None)
    if not dev and getattr(request.state, "is_admin", False):
        dev = request.query_params.get("device")          # admin may drive it for testing
    if not dev:
        raise HTTPException(status_code=403, detail="device-scoped key (or admin + ?device=) required")
    now = time.time()
    mode = _GESTURE_MODES.get(dev)
    if not mode or mode["expires"] < now:
        _GESTURE_MODES.pop(dev, None)
        return {"active": False}
    if mode["last_y"] is not None:
        dy = mode["last_y"] - req.y                        # hand up = smaller y = louder
        if abs(dy) >= _GESTURE_DEADZONE:
            step = max(-_GESTURE_STEP_CLAMP, min(int(round(dy * _GESTURE_GAIN)), _GESTURE_STEP_CLAMP))
            if step != 0:
                _enqueue_volume(deps.household(request), "step", step, mode["target"])
    mode["last_y"] = req.y
    mode["expires"] = now + _GESTURE_TTL_S                 # refresh while the hand is active
    return {"active": True, "expires_in": int(mode["expires"] - now)}


# Cap concurrent long-polls so a flood of GET /devices/commands can't pile up unbounded.
_poll_sem = asyncio.Semaphore(16)


def _claim_commands(household_id: int, device: str) -> List[Dict[str, Any]]:
    """Atomically claim (mark delivered + return) pending commands for one device. A single
    UPDATE…RETURNING — not SELECT-then-UPDATE — so two concurrent pollers can't double-deliver
    the same command (the second writer finds nothing still 'pending')."""
    conn = get_db()
    try:
        rows = conn.execute(
            "UPDATE device_commands SET status='delivered', delivered_at=datetime('now') "
            "WHERE id IN (SELECT id FROM device_commands WHERE household_id = ? AND device_id = ? "
            "AND status='pending' ORDER BY id LIMIT 50) RETURNING id, action, params",
            (household_id, device)).fetchall()
        conn.commit()
        return [{"id": r["id"], "action": r["action"], "params": json.loads(r["params"] or "{}")} for r in rows]
    finally:
        conn.close()


@router.get("/devices/commands")
async def pull_device_commands(request: Request, device: str, wait: int = 20):
    """Device agents PULL their pending commands here (outbound-only; no inbound port on the
    device). Long-polls up to `wait` seconds, returning as soon as commands exist.

    `async` + `asyncio.sleep` so a waiting poll holds no worker thread (a sync handler would
    exhaust the thread pool under many concurrent polls). The key must be bound to `device`
    (or be an admin): a key for one device can't drain another device's queue (F1)."""
    deps.require_not_demo()
    dev = getattr(request.state, "device_id", None)
    if not getattr(request.state, "is_admin", False) and dev != device:
        raise HTTPException(status_code=403, detail="This key is not bound to that device")
    wait = max(0, min(wait, 30))
    deadline = time.time() + wait
    async with _poll_sem:
        while True:
            cmds = await run_in_threadpool(_claim_commands, deps.household(request), device)
            if cmds:
                return {"commands": cmds}
            if time.time() >= deadline:
                return {"commands": []}
            await asyncio.sleep(0.5)
