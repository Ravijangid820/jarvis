"""Semantic intent router — mechanics with FAKE embeddings (deterministic bag-of-words),
plus the confirmation flow in routes/devices.py. Threshold calibration against the real embedder was done
on the box (values recorded in intent_router.py); these tests pin the LOGIC, not the model.
"""
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "orchestrator"))

import deps  # noqa: E402
import ha  # noqa: E402
from routes import devices as routes_devices  # noqa: E402
import intent_router as ir  # noqa: E402

class _FakeState:
    """Minimal stand-in for request.state — enough for the household/authz lookups."""
    def __init__(self, user_id=1, household_id=1, is_admin=True):
        self.user_id = user_id
        self.household_id = household_id
        self.is_admin = is_admin
        self.device_id = None


class _FakeRequest:
    def __init__(self, **kw):
        self.state = _FakeState(**kw)


def _req(**kw):
    """A principal in household 1 — the household that owns the smart home in these tests.
    The home fast-path and the HA tools now require one: they check that the CALLER's household
    actually owns the smart home before touching Home Assistant."""
    return _FakeRequest(**kw)



import zlib
def _bow_embed(texts):
    """Deterministic fake: bag-of-words indicator vectors → cosine = word overlap."""
    vocab = {}
    def vec(t):
        tokens = set(t.lower().split())
        for w in tokens:
            vocab.setdefault(w, len(vocab))
        v = [0.0] * 512
        for w in tokens:
            v[zlib.crc32(w.encode("utf-8")) % 512] += 1.0
        n = math.sqrt(sum(x * x for x in v))
        return [x / n for x in v]
    return [vec(t) for t in texts]


def _embed_query(text):
    return _bow_embed([text])


def _setup(monkeypatch, allowlist):
    monkeypatch.setattr(ha, "HA_ALLOWED_ENTITIES", allowlist)
    assert ir.rebuild(_bow_embed) is True
    assert ir.ready() is True


# --- exemplar generation --------------------------------------------------------

def test_exemplars_runnable_domains_get_run_only(monkeypatch):
    ex = ir.build_exemplars(["automation.morning"])
    actions = {a for (_, a, _) in ex}
    assert actions == {"run"}


def test_exemplars_fan_gets_cooling_paraphrases():
    ex = ir.build_exemplars(["switch.desk_fan"])
    phrases = [p for (_, a, p) in ex if a == "on"]
    assert "turn on the desk fan" in phrases            # generic template
    assert "it is hot in here" in phrases               # cooling-class paraphrase


def test_exemplars_unknown_device_generic_only():
    ex = ir.build_exemplars(["switch.garage_door_opener"])
    assert all("hot" not in p and "dark" not in p for (_, _, p) in ex)


def test_exemplars_are_built_from_the_friendly_name(monkeypatch):
    """Real hardware gets machine-generated ids. Building phrases from the id produced exemplars
    nobody would ever say ("turn on the 4node smart switch switch 3"), so the router could not
    match real speech to a real device."""
    eid = "switch.4node_smart_switch_switch_3"
    ex = ir.build_exemplars([eid], {eid: "Fan"})
    phrases = [p for (_, a, p) in ex if a == "on"]
    assert "turn on the Fan" in phrases
    assert not any("4node" in p for p in phrases)


def test_semantic_class_keys_on_the_friendly_name(monkeypatch):
    """The bigger half: the cooling/light classes decide whether "it is hot in here" can reach a
    device at all, and they match on the NAME. A device called "Fan" whose id is
    switch.4node_smart_switch_switch_3 got no cooling paraphrases whatsoever."""
    eid = "switch.4node_smart_switch_switch_3"
    assert not any("hot" in p for (_, _, p) in ir.build_exemplars([eid], {}))
    assert any("hot" in p for (_, _, p) in ir.build_exemplars([eid], {eid: "Fan"}))


# --- route() decisions ------------------------------------------------------------

def test_exact_phrase_acts(monkeypatch):
    _setup(monkeypatch, ["switch.desk_fan", "light.kitchen"])
    r = ir.route("turn on the desk fan", _embed_query)
    assert r and r["decision"] == "act" and r["entity"] == "switch.desk_fan" and r["action"] == "on"


def test_partial_overlap_confirms(monkeypatch):
    _setup(monkeypatch, ["switch.desk_fan"])
    # {on, desk, fan, now} vs "turn on the desk fan" {turn,on,the,desk,fan}: 3/sqrt(20)=0.67 → confirm
    r = ir.route("on desk fan now", _embed_query)
    assert r and r["decision"] == "confirm" and r["entity"] == "switch.desk_fan"


def test_unrelated_falls_through(monkeypatch):
    _setup(monkeypatch, ["switch.desk_fan"])
    assert ir.route("completely unrelated words entirely", _embed_query) is None


def test_runnable_never_auto_acts(monkeypatch):
    _setup(monkeypatch, ["automation.morning"])
    r = ir.route("run the morning", _embed_query)       # exact exemplar → sim 1.0
    assert r and r["decision"] == "confirm" and r["action"] == "run"


def test_two_similar_entities_confirm_not_act(monkeypatch):
    _setup(monkeypatch, ["light.kitchen", "light.bedroom"])
    # overlaps both "turn on the kitchen"/"turn on the bedroom" equally at 0.75 → ambiguity → confirm
    r = ir.route("turn on the light", _embed_query)
    assert r is not None and r["decision"] == "confirm"


def test_stale_index_is_not_used(monkeypatch):
    _setup(monkeypatch, ["switch.desk_fan"])
    monkeypatch.setattr(ha, "HA_ALLOWED_ENTITIES", ["switch.other"])   # allowlist changed
    assert ir.ready() is False
    assert ir.route("turn on the desk fan", _embed_query) is None


# --- the confirmation flow in main -------------------------------------------------

def _flow_setup(monkeypatch):
    import main
    actions = []
    monkeypatch.setattr(ha, "HA_URL", "http://ha.test:8123")
    monkeypatch.setattr(ha, "HA_TOKEN", "tok")
    monkeypatch.setattr(ha, "HA_ALLOWED_ENTITIES", ["switch.desk_fan"])
    monkeypatch.setattr(ha, "turn", lambda e, a: actions.append((a, e)) or True)
    # The act path now pre-flights the entity against HA (a 200-with-empty-body for an
    # entity HA does not have used to read as success). Fake it as present: these tests
    # fake actuation too, so they must fake existence to match.
    monkeypatch.setattr(ha, "probe_entity", lambda e: (ha.ENTITY_FOUND, {"state": "off"}))
    monkeypatch.setattr(deps, "can_control_devices", lambda r: True)
    monkeypatch.setattr(routes_devices, "REQUIRE_PRESENCE_FOR_CONTROL", False)
    monkeypatch.setattr(deps, "audit", lambda *a, **k: None)
    routes_devices._PENDING_HOME.clear()
    routes_devices._LAST_HOME_ENTITY.clear()
    return main, actions


def test_confirm_then_yes_executes(monkeypatch):
    main, actions = _flow_setup(monkeypatch)
    monkeypatch.setattr(ir, "ready", lambda: True)
    monkeypatch.setattr(ir, "route",
                        lambda text, f: {"decision": "confirm", "entity": "switch.desk_fan",
                                         "action": "on", "score": 0.7})
    reply = routes_devices._handle_home_command("i am kind of warm", _req(), "s1")
    assert reply is not None and reply.lower().startswith("should i turn on")
    assert routes_devices._PENDING_HOME.get("s1") is not None and actions == []   # asked, did NOT act

    reply = routes_devices._handle_home_command("yes please", _req(), "s1")
    assert "fan is now on" in reply.lower()
    assert actions == [("on", "switch.desk_fan")]
    assert "s1" not in routes_devices._PENDING_HOME                               # consumed


def test_confirm_then_no_cancels(monkeypatch):
    main, actions = _flow_setup(monkeypatch)
    monkeypatch.setattr(ir, "ready", lambda: True)
    monkeypatch.setattr(ir, "route",
                        lambda text, f: {"decision": "confirm", "entity": "switch.desk_fan",
                                         "action": "on", "score": 0.7})
    routes_devices._handle_home_command("i am kind of warm", _req(), "s1")
    reply = routes_devices._handle_home_command("no, leave it", _req(), "s1")
    assert "leaving it" in reply.lower() and actions == []


def test_unrelated_message_drops_the_proposal(monkeypatch):
    main, actions = _flow_setup(monkeypatch)
    routes_devices._PENDING_HOME["s1"] = ("switch.desk_fan", "on", time.monotonic())
    monkeypatch.setattr(ir, "ready", lambda: False)     # router quiet now
    reply = routes_devices._handle_home_command("what is the capital of france", _req(), "s1")
    assert reply is None and actions == []                              # goes to the LLM
    assert "s1" not in routes_devices._PENDING_HOME                               # proposal dropped


def test_act_decision_executes_immediately(monkeypatch):
    main, actions = _flow_setup(monkeypatch)
    monkeypatch.setattr(ir, "ready", lambda: True)
    monkeypatch.setattr(ir, "route",
                        lambda text, f: {"decision": "act", "entity": "switch.desk_fan",
                                         "action": "on", "score": 0.85})
    reply = routes_devices._handle_home_command("i'm melting in here", _req(), "s1")
    assert "fan is now on" in reply.lower()
    assert actions == [("on", "switch.desk_fan")]
