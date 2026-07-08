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


def configure(url: Optional[str] = None, token: Optional[str] = None,
              allowed: Optional[List[str]] = None) -> None:
    """Update the live HA settings. Only non-None args are applied."""
    global HA_URL, HA_TOKEN, HA_ALLOWED_ENTITIES
    if url is not None:
        HA_URL = url.rstrip("/")
    if token is not None:
        HA_TOKEN = token
    if allowed is not None:
        HA_ALLOWED_ENTITIES = [e.strip() for e in allowed if e and e.strip()]


def configured() -> bool:
    return bool(HA_URL and HA_TOKEN)


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
    """Controllable entities for the UI picker: [{entity_id, name, state, domain, allowed}].
    Empty list on any failure (unconfigured, unreachable, bad token)."""
    states = _request("GET", "/api/states")
    if not isinstance(states, list):
        return []
    allowed = set(HA_ALLOWED_ENTITIES)
    out = []
    for s in states:
        eid = (s or {}).get("entity_id", "")
        domain = eid.partition(".")[0]
        if domain not in CONTROLLABLE_DOMAINS:
            continue
        out.append({
            "entity_id": eid,
            "name": (s.get("attributes") or {}).get("friendly_name") or eid,
            "state": s.get("state"),
            "domain": domain,
            "allowed": eid in allowed,
        })
    out.sort(key=lambda e: (e["domain"], e["name"].lower()))
    return out


def turn(entity_id: str, action: str) -> bool:
    """turn_on / turn_off / toggle via the domain-generic homeassistant services."""
    service = {"on": "turn_on", "off": "turn_off", "toggle": "toggle"}.get(action)
    if service is None:
        return False
    return _request("POST", f"/api/services/homeassistant/{service}",
                    {"entity_id": entity_id}) is not None


def _norm(s: str) -> set:
    return set(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


def resolve_entity(text: str, allowlist: Optional[List[str]] = None) -> Optional[str]:
    """Map what the model said ('kitchen light', 'input_boolean.test_light') to ONE allowlisted
    entity id. Exact id match first; else word-overlap against each id's object part (and domain).
    Returns None when nothing (or more than one thing) matches — the caller asks for clarification.
    Pure function (allowlist injectable) so it's unit-testable without HA."""
    allowlist = HA_ALLOWED_ENTITIES if allowlist is None else allowlist
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
    candidates = []   # (name_overlap, entity) for anything the words touch at all
    for ent in allowlist:
        dom, _, obj = ent.partition(".")
        name_overlap = len(words & (_norm(obj) - domain_words))
        loose_overlap = len(words & (_norm(obj) | _norm(dom)))
        if loose_overlap:
            candidates.append((name_overlap, ent))
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][1]          # unique however it matched ("the switch" with one switch)
    candidates.sort(reverse=True)
    if candidates[0][0] > candidates[1][0]:
        return candidates[0][1]          # a NAME distinguishes it ("kitchen light")
    return None                          # ambiguous — never guess which device to actuate
