"""Semantic intent router — layer 2 of device-command understanding.

Layer 1 (regex fast-paths in intents.py) catches exact phrasings. This layer catches MEANING:
"i'm melting in here" lands near the fan's "turn on" exemplars by embedding similarity (the same
ONNX embedder + cosine math the RAG memory already uses — no LLM call, no new dependency).

Decisions, by similarity of the best-matching exemplar:
    ACT      — confident: do it (plain on/off/toggle only)
    CONFIRM  — plausible: ask ("Should I turn on the fan?") and remember the proposal
    None     — not a device intent: fall through to the LLM

Safety posture:
- The router only PROPOSES — the caller runs the same allowlist + authz gates + audit as always.
- Automations/scripts/scenes are NEVER auto-acted from a fuzzy match — always CONFIRM: a paraphrase
  should not be able to fire a whole routine without a yes.
- Ambiguity margin: if two different entities score too close, downgrade to CONFIRM (top one).
- If the embedder is unavailable, the router is silently off (regex + clarify guard still work).

Import graph: config → {ha, memory} → intent_router → main (acyclic).
"""
import re
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

import ha
from config import logger

# Thresholds are cosine similarities under the doc/query prefix scheme memory already uses.
# Calibrated 2026-07-09 against the real embeddinggemma-300m ONNX bundle on the box:
#   negatives (10 unrelated chat msgs): max 0.627 ("the sun is very hot today"), typical 0.24–0.47
#   positives (paraphrases):            0.656–0.829 ("i'm melting in here" = 0.829)
# CONFIRM sits just above the negative ceiling; ACT stays conservative — a confirm is one harmless
# question, an act is an actuation.
ACT_SIM = 0.80
CONFIRM_SIM = 0.63
AMBIGUITY_MARGIN = 0.04     # top-2 entities closer than this → don't auto-act, confirm the top one

# --- Exemplar generation ------------------------------------------------------
# Semantic classes by what the device NAME suggests it does. Only classes we're confident about get
# environment paraphrases; unknown devices still get the generic command templates.
_CLASS_PATTERNS = [
    ("cooling", re.compile(r"\b(fan|cooler|ac|air.?con|vent)\b", re.I)),
    ("light",   re.compile(r"\b(light|lamp|bulb|led|strip)\b", re.I)),
    ("heating", re.compile(r"\b(heater|warmer|radiator)\b", re.I)),
]

_CLASS_EXEMPLARS: Dict[str, Dict[str, List[str]]] = {
    "cooling": {
        "on":  ["it is hot in here", "i am feeling hot", "i need some air",
                "cool the room down", "it is getting warm", "i am sweating",
                "i am melting in here", "it is boiling in here", "it is so stuffy"],
        "off": ["it is getting cold", "too much air", "i am feeling cold now", "that is enough cooling"],
    },
    "light": {
        "on":  ["it is dark in here", "i cannot see anything", "brighten the room",
                "make it brighter", "too dark to read"],
        "off": ["it is too bright", "make it dark", "i am going to sleep", "dim everything"],
    },
    "heating": {
        "on":  ["it is cold in here", "i am freezing", "warm the room up", "i am feeling cold"],
        "off": ["it is getting too warm", "that is enough heat"],
    },
}

_GENERIC_TEMPLATES: Dict[str, List[str]] = {
    "on":     ["turn on the {n}", "switch the {n} on", "{n} on please", "start the {n}",
               "can you put the {n} on", "get the {n} going"],
    "off":    ["turn off the {n}", "switch the {n} off", "{n} off please", "shut down the {n}",
               "kill the {n}"],
    "run":    ["run the {n}", "trigger the {n}", "start my {n}", "execute the {n}",
               "do the {n} now"],
}


def _nice(entity_id: str) -> str:
    return entity_id.partition(".")[2].replace("_", " ")


def build_exemplars(allowlist: List[str]) -> List[Tuple[str, str, str]]:
    """[(entity_id, action, phrase)] for every allowlisted entity. Pure — unit-testable."""
    out: List[Tuple[str, str, str]] = []
    for ent in allowlist:
        domain = ent.partition(".")[0]
        name = _nice(ent)
        if domain in ha.RUNNABLE_DOMAINS:
            for phrase in _GENERIC_TEMPLATES["run"]:
                out.append((ent, "run", phrase.format(n=name)))
            # enabling/disabling routines is regex territory; fuzzy matching stays away from it
            continue
        for action in ("on", "off"):
            for phrase in _GENERIC_TEMPLATES[action]:
                out.append((ent, action, phrase.format(n=name)))
        for cls, pat in _CLASS_PATTERNS:
            if pat.search(name) or pat.search(domain):
                for action, phrases in _CLASS_EXEMPLARS[cls].items():
                    for phrase in phrases:
                        out.append((ent, action, phrase))
                break
    return out


# --- Index (embedded exemplars) -------------------------------------------------
_lock = threading.Lock()
_index: Dict[str, Any] = {"key": None, "exemplars": [], "vectors": None}


def _allowlist_key() -> str:
    return "|".join(sorted(ha.HA_ALLOWED_ENTITIES))


def rebuild(embed_documents: Callable[[List[str]], List[List[float]]]) -> bool:
    """(Re)build the exemplar index for the current allowlist. Called at startup and when the
    admin saves the Smart Home config; a few dozen short embeds — seconds, done off-request."""
    key = _allowlist_key()
    exemplars = build_exemplars(ha.HA_ALLOWED_ENTITIES)
    if not exemplars:
        with _lock:
            _index.update(key=key, exemplars=[], vectors=None)
        return False
    try:
        vectors = embed_documents([p for (_, _, p) in exemplars])
    except Exception as e:
        logger.warning("intent router: exemplar embedding failed (%s) — router off", e)
        return False
    with _lock:
        _index.update(key=key, exemplars=exemplars, vectors=vectors)
    logger.info("intent router: %d exemplars indexed for %d entities",
                len(exemplars), len(ha.HA_ALLOWED_ENTITIES))
    return True


def ready() -> bool:
    with _lock:
        return _index["vectors"] is not None and _index["key"] == _allowlist_key()


def route(text: str, embed_query: Callable[[str], List[List[float]]]) -> Optional[Dict[str, Any]]:
    """Classify an utterance against the exemplar index.
    Returns {"decision": "act"|"confirm", "entity": ..., "action": ..., "score": ...} or None."""
    with _lock:
        exemplars, vectors, key = _index["exemplars"], _index["vectors"], _index["key"]
    if vectors is None or key != _allowlist_key():
        return None
    try:
        q = embed_query(text)[0]
    except Exception:
        return None
    # cosine == dot product (both sides normalized). Track the best score per (entity, action)
    # and the best score per *other* entity for the ambiguity margin.
    best: Dict[Tuple[str, str], float] = {}
    for (ent, action, _), vec in zip(exemplars, vectors):
        s = sum(a * b for a, b in zip(q, vec))
        k = (ent, action)
        if s > best.get(k, -1.0):
            best[k] = s
    if not best:
        return None
    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    (entity, action), score = ranked[0]
    runner_up = next((s for (e, _), s in ranked[1:] if e != entity), -1.0)
    if score < CONFIRM_SIM:
        return None
    decision = "act" if score >= ACT_SIM else "confirm"
    if entity.partition(".")[0] in ha.RUNNABLE_DOMAINS:
        decision = "confirm"                      # never auto-fire a routine from a fuzzy match
    elif score - runner_up < AMBIGUITY_MARGIN and runner_up >= CONFIRM_SIM:
        decision = "confirm"                      # two entities too close — ask about the top one
    return {"decision": decision, "entity": entity, "action": action, "score": round(score, 4)}
