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
import urllib.request
from typing import Any, Dict, List, Optional

from config import HA_ALLOWED_ENTITIES, HA_TOKEN, HA_URL, logger

_TIMEOUT = 5  # seconds — a LAN call; never let a dead HA hang a chat turn


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
