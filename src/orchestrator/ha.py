"""Home Assistant REST client + entity guardrails.

Security model (matches the rest of Jarvis — the LLM NEVER holds authority):
- The HA long-lived token lives in config (or env) and is used ONLY here, server-side.
  Mint it from a dedicated NON-ADMIN HA user so even this token is least-privilege.
- The LLM is offered narrow tools; every call is validated against ALLOWED_ENTITIES
  before any HTTP leaves the box. No generic "call any service" passthrough exists —
  a prompt injection can at worst toggle an allowlisted light.
- Control uses HA's generic homeassistant.turn_on/turn_off/toggle services, which work
  across light/switch/input_boolean/... — one narrow surface for all simple devices.

Depends only on config (acyclic import graph). All functions are fail-soft: network
errors return False/None and the caller words a friendly reply.
"""
import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from config import HA_ALLOWED_ENTITIES, HA_TOKEN, HA_URL, logger

_TIMEOUT = 5  # seconds — a LAN call; never let a dead HA hang a chat turn

# Entity domains the generic homeassistant.turn_on/off/toggle services can drive — the set the UI
# device-picker offers and the model can control. (Read-only sensors etc. are excluded.)
CONTROLLABLE_DOMAINS = ("light", "switch", "input_boolean", "fan", "cover", "scene", "script",
                        "media_player", "climate", "automation")

# HA_URL / HA_TOKEN / HA_ALLOWED_ENTITIES start from config (env or jarvis.json) and are the LIVE
# values the functions below read. configure() reassigns them so the settings can change at runtime
# (loaded from the DB at startup, updated by the admin UI) without a restart.


# Which household this process's Home Assistant belongs to. Lives here rather than in main so that
# prompt assembly (chat.py) can ask "may this household see the devices?" without importing main —
# which it cannot, since main imports it. One source of truth for the same boundary main enforces
# on the HTTP surface.
HA_HOUSEHOLD_ID: Optional[int] = None


def configure(url: Optional[str] = None, token: Optional[str] = None,
              allowed: Optional[List[str]] = None,
              household_id: Optional[int] = None) -> None:
    """Update the live HA settings. Only non-None args are applied."""
    global HA_URL, HA_TOKEN, HA_ALLOWED_ENTITIES, HA_HOUSEHOLD_ID
    if url is not None:
        HA_URL = url.rstrip("/")
    if token is not None:
        HA_TOKEN = token
    if allowed is not None:
        HA_ALLOWED_ENTITIES = [e.strip() for e in allowed if e and e.strip()]
    if household_id is not None:
        HA_HOUSEHOLD_ID = household_id
    _SNAPSHOT["at"] = 0.0          # settings changed → the cached view is stale


def owns(household_id: Optional[int]) -> bool:
    """True if this household is the one the smart home belongs to. A demo household never is."""
    return HA_HOUSEHOLD_ID is not None and household_id == HA_HOUSEHOLD_ID


def configured() -> bool:
    return bool(HA_URL and HA_TOKEN)


# One /api/states call answers for every device, so a turn that asks twice should not pay twice.
# The TTL is short because the point of the block is to be TRUE: a stale "on" that the user can see
# is off would be worse than no block at all, since it teaches them the assistant doesn't know.
_SNAPSHOT: Dict[str, Any] = {"at": 0.0, "rows": []}
_SNAPSHOT_TTL = 4.0


def invalidate_snapshot() -> None:
    """Force the next snapshot() to re-read from HA.

    Called the moment an action succeeds. Without it the prompt could carry the state from up to a
    few seconds BEFORE the switch that this very message caused — and a device block saying "Fan —
    on" next to a note saying the fan was just turned off is a contradiction the model resolves by
    inventing a reason for it (observed: "the temperature is too low, and the motor has been shut
    down", describing a sensor that does not exist).
    """
    _SNAPSHOT["at"] = 0.0


def snapshot(ttl: float = _SNAPSHOT_TTL) -> List[Dict[str, str]]:
    """Allowlisted devices and their CURRENT state, for grounding the model.

    Only the allowlist: the model should be told about exactly the devices it is permitted to
    reason about, so the prompt can't invite it to discuss something it would then be refused.
    Returns [] when unconfigured or unreachable — the caller then says nothing rather than
    asserting an empty house.
    """
    if not configured() or not HA_ALLOWED_ENTITIES:
        return []
    now = time.monotonic()
    if now - _SNAPSHOT["at"] < ttl:
        return _SNAPSHOT["rows"]
    states = _request("GET", "/api/states")
    if not isinstance(states, list):
        return _SNAPSHOT["rows"] if _SNAPSHOT["rows"] else []
    allowed = set(HA_ALLOWED_ENTITIES)
    rows = []
    for s in states:
        eid = (s or {}).get("entity_id", "")
        if eid not in allowed:
            continue
        rows.append({
            "entity_id": eid,
            "name": (s.get("attributes") or {}).get("friendly_name") or eid.partition(".")[2].replace("_", " "),
            "state": s.get("state") or "unknown",
            "domain": eid.partition(".")[0],
        })
    rows.sort(key=lambda r: r["name"].lower())
    _SNAPSHOT.update(at=now, rows=rows)
    return rows


def _request(method: str, path: str, payload: Optional[dict] = None) -> Optional[Any]:
    req = urllib.request.Request(
        f"{HA_URL}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode() or "null")
    except Exception as e:
        logger.warning("Home Assistant %s %s failed: %s", method, path, e)
        return None


def ping() -> bool:
    """True if HA answers /api/ with our token (used by the admin services board)."""
    return configured() and _request("GET", "/api/") is not None


def get_state(entity_id: str) -> Optional[Dict[str, Any]]:
    """State object for one entity: {'state': 'on', 'attributes': {'friendly_name': ...}, ...}"""
    return _request("GET", f"/api/states/{entity_id}")


# Outcomes of asking Home Assistant about one entity. "HA does not have this" and "HA did not
# answer" must never reach the user as the same sentence: the first is a stale allowlist they can
# fix in the admin page, the second is a network blip they can only wait out.
ENTITY_FOUND = "found"
ENTITY_MISSING = "missing"
HA_UNREACHABLE = "unreachable"


def probe_entity(entity_id: str) -> tuple:
    """(status, state_object) — status is ENTITY_FOUND / ENTITY_MISSING / HA_UNREACHABLE.

    Exists because HA's generic homeassistant.turn_on/turn_off answer 200 with an empty body for
    an entity_id they do NOT have, so acting was indistinguishable from succeeding: a device
    renamed or deleted in HA produced a confident "the fan is now off" while nothing happened.
    A 404 from /api/states is the one unambiguous, race-free signal — unlike reading state back
    after acting, which a physical switch may not have reported yet.
    """
    if not configured():
        return HA_UNREACHABLE, None
    req = urllib.request.Request(f"{HA_URL}/api/states/{entity_id}",
                                 headers={"Authorization": f"Bearer {HA_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return ENTITY_FOUND, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return ENTITY_MISSING, None
        logger.warning("Home Assistant probe %s failed: HTTP %s", entity_id, e.code)
        return HA_UNREACHABLE, None
    except Exception as e:
        logger.warning("Home Assistant probe %s failed: %s", entity_id, e)
        return HA_UNREACHABLE, None


def test_connection(url: Optional[str], token: Optional[str]) -> tuple:
    """Probe /api/ with the given creds (falling back to the live ones), WITHOUT mutating state —
    lets the admin UI validate a URL/token before saving. Returns (ok: bool, detail: str)."""
    url = (url or HA_URL or "").rstrip("/")
    token = token or HA_TOKEN
    if not url or not token:
        return False, "URL and token are both required."
    req = urllib.request.Request(f"{url}/api/", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            json.loads(r.read().decode() or "null")
        return True, "Connected to Home Assistant."
    except urllib.error.HTTPError as e:
        return False, ("Token rejected (check it's a valid long-lived token)." if e.code in (401, 403)
                       else f"Home Assistant returned HTTP {e.code}.")
    except Exception as e:
        return False, f"Could not reach Home Assistant: {e}"


def list_entities() -> List[Dict[str, Any]]:
    """Controllable entities for the UI picker:
    [{entity_id, name, state, domain, allowed, available}].
    Empty list on any failure (unconfigured, unreachable, bad token).

    Includes allowlisted entities Home Assistant NO LONGER KNOWS, flagged available=False. They
    are listed precisely because they are broken: an entity that has been renamed or deleted in HA
    stays in the stored allowlist forever otherwise — the picker only ever rendered what HA
    currently returns, so there was no way to untick it, and every save wrote it straight back.
    Worse than untidy, a stale entry actively breaks resolution: a dead `input_boolean.fan` ties
    with a real switch named "Fan", and the tie makes resolve_entity refuse BOTH — which reads to
    the user as "the allowlist didn't save".
    """
    states = _request("GET", "/api/states")
    if not isinstance(states, list):
        return []
    allowed = set(HA_ALLOWED_ENTITIES)
    out = []
    known = set()
    for s in states:
        eid = (s or {}).get("entity_id", "")
        domain = eid.partition(".")[0]
        if not eid:
            continue
        known.add(eid)
        if domain not in CONTROLLABLE_DOMAINS:
            continue
        out.append({
            "entity_id": eid,
            "name": (s.get("attributes") or {}).get("friendly_name") or eid,
            "state": s.get("state"),
            "domain": domain,
            "allowed": eid in allowed,
            "available": True,
        })
    out.sort(key=lambda e: (e["domain"], e["name"].lower()))
    # Stale allowlist entries last, so the normal list keeps its familiar order and the broken
    # ones are grouped where the UI can call them out.
    for eid in HA_ALLOWED_ENTITIES:
        if eid in known:
            continue
        out.append({
            "entity_id": eid,
            "name": eid.partition(".")[2].replace("_", " "),
            "state": "unknown to Home Assistant",
            "domain": eid.partition(".")[0],
            "allowed": True,
            "available": False,
        })
    return out


def turn(entity_id: str, action: str) -> bool:
    """turn_on / turn_off / toggle via the domain-generic homeassistant services."""
    service = {"on": "turn_on", "off": "turn_off", "toggle": "toggle"}.get(action)
    if service is None:
        return False
    return _request("POST", f"/api/services/homeassistant/{service}",
                    {"entity_id": entity_id}) is not None


# Domains whose actions can be EXECUTED on demand ("run the movie night automation").
RUNNABLE_DOMAINS = ("automation", "script", "scene")


def run(entity_id: str) -> bool:
    """Execute an automation/script/scene NOW. Security posture (data-leak proof by construction):
    - payload is HARDCODED to the entity_id — no variables/service-data channel exists, so the LLM
      cannot inject parameters into HA no matter what it emits;
    - automations run with skip_condition=False: the automation's OWN guard conditions still apply;
    - HA's response body is discarded (bool out) — no HA state ever flows back toward the model.
    Callers must have validated entity_id against the allowlist (resolve_entity), as with turn()."""
    domain = entity_id.partition(".")[0]
    if domain == "automation":
        return _request("POST", "/api/services/automation/trigger",
                        {"entity_id": entity_id, "skip_condition": False}) is not None
    if domain == "script":
        return _request("POST", "/api/services/script/turn_on", {"entity_id": entity_id}) is not None
    if domain == "scene":
        return _request("POST", "/api/services/scene/turn_on", {"entity_id": entity_id}) is not None
    return False    # "run" is meaningless for lights/switches — callers map it to turn(entity, "on")


def stop(entity_id: str) -> bool:
    """Abort a RUNNING automation/script WITHOUT changing its enabled state — "stop" must not
    quietly disarm an automation (only an explicit disable/turn-off should do that).
    - automation: turn_off (stop_actions aborts the in-flight run) then turn_on to re-arm;
    - script: script.turn_off stops execution (scripts have no armed state to preserve).
    Same leak posture as run(): hardcoded payloads, responses discarded."""
    domain = entity_id.partition(".")[0]
    if domain == "automation":
        off_ok = _request("POST", "/api/services/automation/turn_off",
                          {"entity_id": entity_id, "stop_actions": True}) is not None
        on_ok = _request("POST", "/api/services/automation/turn_on",
                         {"entity_id": entity_id}) is not None
        return off_ok and on_ok
    if domain == "script":
        return _request("POST", "/api/services/script/turn_off", {"entity_id": entity_id}) is not None
    return False    # plain devices: callers map "stop" to turn(entity, "off")


def _norm(s: str) -> set:
    return set(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


# entity_id -> friendly_name, cached from /api/states. THE reason this exists: real hardware gets
# machine-generated ids ("switch.4node_smart_switch_switch_3") while the human name people actually
# speak ("Fan") lives only in the friendly_name attribute. Resolving on the id alone meant "turn on
# the fan" matched nothing at all, and the LLM's own (correct) tool call — device:"fan" — was thrown
# away by the resolver. Refreshed off-request (startup + admin save), never from a chat turn.
_FRIENDLY: Dict[str, str] = {}


def refresh_names() -> int:
    """Re-read entity_id -> friendly_name from HA. Returns how many names were cached (0 on any
    failure, leaving the previous cache intact — a transient HA blip must not un-name every device
    and silently degrade resolution back to machine ids)."""
    global _FRIENDLY
    states = _request("GET", "/api/states")
    if not isinstance(states, list):
        return 0
    names = {}
    for s in states:
        eid = (s or {}).get("entity_id")
        nice = ((s or {}).get("attributes") or {}).get("friendly_name")
        if eid and nice:
            names[eid] = str(nice)
    if names:
        _FRIENDLY = names
    return len(names)


def friendly_names() -> Dict[str, str]:
    """A copy of the cached entity_id -> friendly_name map (injectable into the pure helpers)."""
    return dict(_FRIENDLY)


def display_name(entity_id: str, names: Optional[Dict[str, str]] = None) -> str:
    """What a human calls this entity: its friendly name, else the object part of the id."""
    names = _FRIENDLY if names is None else names
    return (names.get(entity_id) or entity_id.partition(".")[2].replace("_", " ")).strip()


def resolve_entity(text: str, allowlist: Optional[List[str]] = None,
                   names: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Map what the model said ('kitchen light', 'Fan', 'input_boolean.test_light') to ONE
    allowlisted entity id. Exact id match first; else word-overlap against each entity's NAME words
    — its friendly name plus its id's object part — and its domain.
    Returns None when nothing (or more than one thing) matches — the caller asks for clarification.
    Pure function (allowlist AND names injectable) so it's unit-testable without HA."""
    allowlist = HA_ALLOWED_ENTITIES if allowlist is None else allowlist
    names = _FRIENDLY if names is None else names
    text = (text or "").strip().lower()
    if not text or not allowlist:
        return None
    if text in (e.lower() for e in allowlist):
        return text
    words = _norm(text)
    # Domain words ("light", "switch", …) are generic: they may select a device only when they
    # single one out — they never count as a NAME match (else "the light" would silently pick
    # whichever entity happens to contain "light" in its name, with three lights present).
    domain_words = set()
    for e in allowlist:
        domain_words |= _norm(e.partition(".")[0])
    candidates = []   # (fully_named, name_overlap, entity) for anything the words touch at all
    for ent in allowlist:
        dom, _, obj = ent.partition(".")
        name_overlap = len(words & ((_norm(obj) | _norm(names.get(ent, ""))) - domain_words))
        loose_overlap = len(words & (_norm(obj) | _norm(dom) | _norm(names.get(ent, ""))))
        # Whether the utterance contains this device's WHOLE display name. With a "Light" and a
        # "Tube Light" both allowlisted, "turn on the light" overlaps each by one word — but only
        # "Light" is named completely, and that is the one the speaker meant. "tube light" names
        # both completely, and the overlap count then picks the more specific one.
        #
        # Computed from the DISPLAY name alone, never the union with the id: a device whose id is
        # `switch.4node_smart_switch_switch_1` would otherwise need the speaker to recite "4node
        # smart 1" before it counted as fully named, which is the whole problem being fixed.
        display_words = _norm(names.get(ent) or obj) - domain_words
        fully_named = bool(display_words) and display_words <= words
        if loose_overlap:
            candidates.append((fully_named, name_overlap, ent))
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][2]          # unique however it matched ("the switch" with one switch)
    candidates.sort(reverse=True)
    if candidates[0][:2] > candidates[1][:2]:
        return candidates[0][2]          # a NAME distinguishes it ("kitchen light", "tube light")
    return None                          # ambiguous — never guess which device to actuate
