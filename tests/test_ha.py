"""Home Assistant integration — the security-relevant pure logic.

No network, no TestClient: resolve_entity is pure (allowlist injected), and the client
functions are exercised with a monkeypatched urlopen — safehttp.urlopen, which is what ha.py
calls; patching urllib's would no longer intercept anything, and the tests would quietly
start making real requests instead of failing. The gates themselves
(deps.can_control_devices etc.) are covered by the existing API tests.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "orchestrator"))

import deps  # noqa: E402
import ha  # noqa: E402

ALLOW = ["input_boolean.test_light", "light.kitchen", "light.living_room", "switch.desk_fan"]

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



# --- resolve_entity: the allowlist guard the tool executor relies on ---------

def test_exact_entity_id_matches():
    assert ha.resolve_entity("light.kitchen", ALLOW) == "light.kitchen"


def test_natural_language_matches_object_name():
    assert ha.resolve_entity("kitchen light", ALLOW) == "light.kitchen"
    assert ha.resolve_entity("the test light", ALLOW) == "input_boolean.test_light"
    assert ha.resolve_entity("desk fan", ALLOW) == "switch.desk_fan"


def test_unknown_device_returns_none():
    assert ha.resolve_entity("garage door", ALLOW) is None


def test_ambiguous_never_guesses():
    # "light" alone word-matches kitchen and living_room equally -> must refuse, not actuate one
    assert ha.resolve_entity("light", ALLOW) is None


def test_bare_domain_word_resolves_only_when_unique():
    # one switch in the allowlist -> "the switch" is unambiguous; three light-ish things -> refuse
    assert ha.resolve_entity("switch", ALLOW) == "switch.desk_fan"
    assert ha.resolve_entity("light", ALLOW) is None


def test_two_switches_make_bare_domain_ambiguous():
    two = ALLOW + ["switch.heater"]
    assert ha.resolve_entity("switch", two) is None
    assert ha.resolve_entity("heater", two) == "switch.heater"


def test_empty_inputs():
    assert ha.resolve_entity("", ALLOW) is None
    assert ha.resolve_entity("kitchen light", []) is None


def test_entity_outside_allowlist_cannot_resolve():
    # even a perfectly-formed entity id is refused unless allowlisted
    assert ha.resolve_entity("lock.front_door", ALLOW) is None


# --- client: payloads + fail-soft ---------------------------------------------

class _FakeResp:
    def __init__(self, body):
        self._body = body
    def read(self):
        return json.dumps(self._body).encode()
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_turn_posts_generic_homeassistant_service(monkeypatch):
    seen = {}
    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["payload"] = json.loads(req.data.decode())
        seen["auth"] = req.headers.get("Authorization")
        return _FakeResp({})
    monkeypatch.setattr(ha, "HA_URL", "http://ha.test:8123")
    monkeypatch.setattr(ha, "HA_TOKEN", "tok123")
    monkeypatch.setattr(ha.safehttp, "urlopen", fake_urlopen)
    assert ha.turn("light.kitchen", "on") is True
    assert seen["url"] == "http://ha.test:8123/api/services/homeassistant/turn_on"
    assert seen["payload"] == {"entity_id": "light.kitchen"}
    assert seen["auth"] == "Bearer tok123"


def test_turn_rejects_unknown_action(monkeypatch):
    monkeypatch.setattr(ha, "HA_URL", "http://ha.test:8123")
    monkeypatch.setattr(ha, "HA_TOKEN", "tok123")
    assert ha.turn("light.kitchen", "explode") is False   # no service mapping -> no HTTP at all


def test_network_failure_is_failsoft(monkeypatch):
    def boom(req, timeout=None):
        raise OSError("connection refused")
    monkeypatch.setattr(ha, "HA_URL", "http://ha.test:8123")
    monkeypatch.setattr(ha, "HA_TOKEN", "tok123")
    monkeypatch.setattr(ha.safehttp, "urlopen", boom)
    assert ha.turn("light.kitchen", "on") is False
    assert ha.get_state("light.kitchen") is None
    assert ha.ping() is False


def test_unconfigured_is_off():
    # module was imported with no HA env/config -> feature off
    assert ha.configured() in (False,) if not (ha.HA_URL and ha.HA_TOKEN) else True


# --- configure(): runtime settings applied by the admin UI ---------------------

def test_configure_applies_live_values(monkeypatch):
    monkeypatch.setattr(ha, "HA_URL", "")
    monkeypatch.setattr(ha, "HA_TOKEN", "")
    monkeypatch.setattr(ha, "HA_ALLOWED_ENTITIES", [])
    ha.configure(url="http://ha.local:8123/", token="tok", allowed=["light.kitchen", " ", ""])
    assert ha.HA_URL == "http://ha.local:8123"      # trailing slash stripped
    assert ha.configured() is True
    assert ha.HA_ALLOWED_ENTITIES == ["light.kitchen"]   # blanks dropped
    # a None arg leaves that field untouched
    ha.configure(allowed=["switch.fan"])
    assert ha.HA_URL == "http://ha.local:8123" and ha.HA_ALLOWED_ENTITIES == ["switch.fan"]


def test_list_entities_filters_to_controllable(monkeypatch):
    states = [
        {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen"}},
        {"entity_id": "sensor.temperature", "state": "21", "attributes": {}},   # not controllable
        {"entity_id": "switch.fan", "state": "off", "attributes": {}},
    ]
    monkeypatch.setattr(ha, "_request", lambda *a, **k: states)
    monkeypatch.setattr(ha, "HA_ALLOWED_ENTITIES", ["light.kitchen"])
    ents = ha.list_entities()
    ids = [e["entity_id"] for e in ents]
    assert "sensor.temperature" not in ids            # sensors excluded
    assert {"light.kitchen", "switch.fan"} == set(ids)
    kitchen = next(e for e in ents if e["entity_id"] == "light.kitchen")
    assert kitchen["name"] == "Kitchen" and kitchen["allowed"] is True


def test_list_entities_empty_on_failure(monkeypatch):
    monkeypatch.setattr(ha, "_request", lambda *a, **k: None)
    assert ha.list_entities() == []


def test_test_connection_requires_both(monkeypatch):
    monkeypatch.setattr(ha, "HA_URL", "")
    monkeypatch.setattr(ha, "HA_TOKEN", "")
    ok, detail = ha.test_connection("", "")
    assert ok is False and "required" in detail.lower()


def test_settings_store_roundtrip(tmp_path, monkeypatch):
    # get_setting/set_setting against a throwaway DB
    import config
    import db
    dbfile = tmp_path / "s.db"
    monkeypatch.setattr(config, "DB_PATH", str(dbfile))
    monkeypatch.setattr(db, "DB_PATH", str(dbfile))
    db.init_db()
    assert db.get_setting("ha_url", "fallback") == "fallback"
    db.set_setting("ha_url", "http://x:8123")
    assert db.get_setting("ha_url") == "http://x:8123"
    db.set_setting("ha_url", "http://y:8123")           # upsert
    assert db.get_setting("ha_url") == "http://y:8123"


# --- the v2.5.0 regression: tools must reflect LIVE config, not import-time config ---

def test_ha_tools_offered_only_when_configured(monkeypatch):
    import main
    monkeypatch.setattr(ha, "HA_URL", "")
    monkeypatch.setattr(ha, "HA_TOKEN", "")
    names = [t["function"]["name"] for t in main._active_tools(_req())]
    assert "set_volume" in names and "home_control" not in names
    # configure at RUNTIME (what the admin UI does) -> tools appear on the next request
    monkeypatch.setattr(ha, "HA_URL", "http://ha.test:8123")
    monkeypatch.setattr(ha, "HA_TOKEN", "tok")
    names = [t["function"]["name"] for t in main._active_tools(_req())]
    assert "home_control" in names and "home_status" in names


# --- the deterministic fast-path parser (web chat's reliable route to devices) ---

def test_parse_home_command_phrasings():
    from intents import parse_home_command as p
    assert p("turn on the test light") == {"action": "on", "device": "test light"}
    assert p("i said turn the test light on") == {"action": "on", "device": "test light"}
    assert p("switch off the desk fan") == {"action": "off", "device": "desk fan"}
    assert p("toggle kitchen light") == {"action": "toggle", "device": "kitchen light"}
    assert p("is the test light on?") == {"action": "status", "device": "test light"}


def test_parse_home_command_never_hijacks():
    from intents import parse_home_command as p
    assert p("turn the volume up") is None          # audio belongs to the volume intent
    assert p("what is the weather today") is None
    assert p("turn my life around") is None
    assert p("") is None


def test_switch_it_off_uses_last_device(monkeypatch):
    """The real-conversation regression: 'switch on the fan' then 'switch it off' must act on
    the fan — and a pronoun with no referent must ASK, not fall through to the LLM."""
    import main
    turned = []
    monkeypatch.setattr(ha, "HA_URL", "http://ha.test:8123")
    monkeypatch.setattr(ha, "HA_TOKEN", "tok")
    monkeypatch.setattr(ha, "HA_ALLOWED_ENTITIES", ["input_boolean.test_light", "input_boolean.desk_fan"])
    monkeypatch.setattr(ha, "turn", lambda e, a: turned.append((e, a)) or True)
    # The act path now pre-flights the entity against HA (a 200-with-empty-body for an
    # entity HA does not have used to read as success). Fake it as present: these tests
    # fake actuation too, so they must fake existence to match.
    monkeypatch.setattr(ha, "probe_entity", lambda e: (ha.ENTITY_FOUND, {"state": "off"}))
    monkeypatch.setattr(deps, "can_control_devices", lambda r: True)
    monkeypatch.setattr(main, "REQUIRE_PRESENCE_FOR_CONTROL", False)
    monkeypatch.setattr(deps, "audit", lambda *a, **k: None)
    main._LAST_HOME_ENTITY.clear()

    # no referent yet -> asks, does NOT act, does NOT fall through (None would mean LLM)
    reply = main._handle_home_command("switch it off", _req(), "s1")
    assert reply is not None and "which device" in reply.lower() and turned == []

    # name the device -> acts + remembers
    assert "fan is now on" in main._handle_home_command("switch on the fan", _req(), "s1").lower()
    assert turned == [("input_boolean.desk_fan", "on")]

    # pronoun now resolves to the fan
    assert "fan is now off" in main._handle_home_command("switch it off", _req(), "s1").lower()
    assert turned[-1] == ("input_boolean.desk_fan", "off")

    # a DIFFERENT session has no referent -> asks again (no cross-session leakage)
    reply = main._handle_home_command("turn it on", _req(), "s2")
    assert reply is not None and "which device" in reply.lower()


def test_parse_home_command_with_trailing_context():
    """Real speech wraps commands in context — the device phrase ends at a clause boundary."""
    from intents import parse_home_command as p
    assert p("can you please turn on the fan, i am feeling a little hot in here") == {"action": "on", "device": "fan"}
    assert p("turn the fan on because it is hot") == {"action": "on", "device": "fan"}
    assert p("switch off the test light since we are leaving") == {"action": "off", "device": "test light"}
    assert p("turn on the fan please") == {"action": "on", "device": "fan"}
    assert p("is the fan on right now?") == {"action": "status", "device": "fan"}
    # still no hijacks
    assert p("turn the volume up because it is quiet") is None
    assert p("what should i do, i am feeling hot") is None


# --- run(): execute automations/scripts/scenes — leak-proof by construction ---

def test_run_maps_domain_to_service_with_hardcoded_payload(monkeypatch):
    calls = []
    def fake_urlopen(req, timeout=None):
        calls.append((req.full_url, json.loads(req.data.decode())))
        return _FakeResp({})
    monkeypatch.setattr(ha, "HA_URL", "http://ha.test:8123")
    monkeypatch.setattr(ha, "HA_TOKEN", "tok")
    monkeypatch.setattr(ha.safehttp, "urlopen", fake_urlopen)
    assert ha.run("automation.movie_night") is True
    assert calls[-1] == ("http://ha.test:8123/api/services/automation/trigger",
                         {"entity_id": "automation.movie_night", "skip_condition": False})
    assert ha.run("script.reset_all") is True
    assert calls[-1] == ("http://ha.test:8123/api/services/script/turn_on",
                         {"entity_id": "script.reset_all"})
    assert ha.run("scene.evening") is True
    assert calls[-1][0].endswith("/api/services/scene/turn_on")


def test_run_refuses_non_runnable_domains(monkeypatch):
    monkeypatch.setattr(ha, "HA_URL", "http://ha.test:8123")
    monkeypatch.setattr(ha, "HA_TOKEN", "tok")
    assert ha.run("light.kitchen") is False        # no HTTP at all


def test_run_via_fast_path_and_start_the_fan_means_on(monkeypatch):
    import main
    actions = []
    monkeypatch.setattr(ha, "HA_URL", "http://ha.test:8123")
    monkeypatch.setattr(ha, "HA_TOKEN", "tok")
    monkeypatch.setattr(ha, "HA_ALLOWED_ENTITIES", ["automation.movie_night", "switch.desk_fan"])
    monkeypatch.setattr(ha, "run", lambda e: actions.append(("run", e)) or True)
    # The act path now pre-flights the entity against HA (a 200-with-empty-body for an
    # entity HA does not have used to read as success). Fake it as present: these tests
    # fake actuation too, so they must fake existence to match.
    monkeypatch.setattr(ha, "probe_entity", lambda e: (ha.ENTITY_FOUND, {"state": "off"}))
    monkeypatch.setattr(ha, "turn", lambda e, a: actions.append((a, e)) or True)
    monkeypatch.setattr(deps, "can_control_devices", lambda r: True)
    monkeypatch.setattr(main, "REQUIRE_PRESENCE_FOR_CONTROL", False)
    monkeypatch.setattr(deps, "audit", lambda *a, **k: None)
    main._LAST_HOME_ENTITY.clear()

    reply = main._handle_home_command("run the movie night automation", _req(), "s1")
    assert "running the movie night automation now" in reply.lower()
    assert actions[-1] == ("run", "automation.movie_night")

    reply = main._handle_home_command("start the fan", _req(), "s1")   # run on a plain device = on
    assert "fan is now on" in reply.lower()
    assert actions[-1] == ("on", "switch.desk_fan")


def test_parse_run_phrasings():
    from intents import parse_home_command as p
    assert p("run the movie night automation") == {"action": "run", "device": "movie night automation"}
    assert p("trigger movie night") == {"action": "run", "device": "movie night"}
    assert p("execute the reset script please") == {"action": "run", "device": "reset script"}


# --- "stop X" + the anti-bluff guard ------------------------------------------

def test_stop_vs_disable_semantics(monkeypatch):
    """"stop" aborts the run but PRESERVES the automation's enabled state; only explicit
    enable/disable (or turn on/off) changes it. "stop the fan" still just switches it off."""
    import main
    actions = []
    monkeypatch.setattr(ha, "HA_URL", "http://ha.test:8123")
    monkeypatch.setattr(ha, "HA_TOKEN", "tok")
    monkeypatch.setattr(ha, "HA_ALLOWED_ENTITIES", ["automation.morning", "switch.desk_fan"])
    monkeypatch.setattr(ha, "turn", lambda e, a: actions.append((a, e)) or True)
    # The act path now pre-flights the entity against HA (a 200-with-empty-body for an
    # entity HA does not have used to read as success). Fake it as present: these tests
    # fake actuation too, so they must fake existence to match.
    monkeypatch.setattr(ha, "probe_entity", lambda e: (ha.ENTITY_FOUND, {"state": "off"}))
    monkeypatch.setattr(ha, "stop", lambda e: actions.append(("stop", e)) or True)
    monkeypatch.setattr(deps, "can_control_devices", lambda r: True)
    monkeypatch.setattr(main, "REQUIRE_PRESENCE_FOR_CONTROL", False)
    monkeypatch.setattr(deps, "audit", lambda *a, **k: None)
    main._LAST_HOME_ENTITY.clear()

    reply = main._handle_home_command("stop morning automation", _req(), "s1")
    assert "stopped the morning automation" in reply.lower() and "stays enabled" in reply.lower()
    assert actions[-1] == ("stop", "automation.morning")     # NOT ("off", ...) — stays enabled

    assert "morning automation is disabled" in main._handle_home_command("disable the morning automation", _req(), "s1").lower()
    assert actions[-1] == ("off", "automation.morning")      # explicit disable -> off

    assert "morning automation is enabled" in main._handle_home_command("enable morning automation", _req(), "s1").lower()
    assert actions[-1] == ("on", "automation.morning")

    assert "fan is now off" in main._handle_home_command("stop the fan", _req(), "s1").lower()
    assert actions[-1] == ("off", "switch.desk_fan")          # plain device: stop = off


def test_ha_stop_service_sequence(monkeypatch):
    """automation stop = turn_off(stop_actions) THEN turn_on (re-arm); script stop = script.turn_off."""
    calls = []
    def fake_urlopen(req, timeout=None):
        calls.append((req.full_url, json.loads(req.data.decode())))
        return _FakeResp({})
    monkeypatch.setattr(ha, "HA_URL", "http://ha.test:8123")
    monkeypatch.setattr(ha, "HA_TOKEN", "tok")
    monkeypatch.setattr(ha.safehttp, "urlopen", fake_urlopen)
    assert ha.stop("automation.morning") is True
    assert calls[-2] == ("http://ha.test:8123/api/services/automation/turn_off",
                         {"entity_id": "automation.morning", "stop_actions": True})
    assert calls[-1] == ("http://ha.test:8123/api/services/automation/turn_on",
                         {"entity_id": "automation.morning"})
    assert ha.stop("script.reset_all") is True
    assert calls[-1][0].endswith("/api/services/script/turn_off")
    assert ha.stop("light.kitchen") is False                  # callers map device-stop to off


def test_parse_enable_disable():
    from intents import parse_home_command as p
    assert p("enable the morning automation") == {"action": "on", "device": "morning automation"}
    assert p("disable morning automation") == {"action": "off", "device": "morning automation"}
    assert p("stop morning automation") == {"action": "stop", "device": "morning automation"}


def _home_ready(monkeypatch, allowed=("automation.morning",)):
    """Configure a reachable-looking smart home owned by the caller's household (1)."""
    import main
    monkeypatch.setattr(ha, "HA_URL", "http://ha.test:8123")
    monkeypatch.setattr(ha, "HA_TOKEN", "tok")
    monkeypatch.setattr(ha, "HA_ALLOWED_ENTITIES", list(allowed))
    monkeypatch.setattr(deps, "HA_HOUSEHOLD_ID", 1)
    return main


def test_antibluff_guard_asks_instead_of_reaching_the_llm(monkeypatch):
    """Device named + control verb, but unparseable phrasing -> a clarification, NOT None
    (None would fall through to the streaming LLM, which is offered no tools and bluffs acks).

    The tool round-trip is stubbed out: this asserts the ROUTING, and a unit test must not depend
    on llama-server being up or on what a 2B model happens to emit today.
    """
    main = _home_ready(monkeypatch)
    monkeypatch.setattr(main, "_home_tool_roundtrip", lambda *a, **k: None)   # model calls nothing
    reply = main._handle_home_command("morning automation stop please now thanks", _req(), "s1")
    assert reply is not None and "morning" in reply.lower()

    # ordinary sentences (no allowlisted device) still reach the LLM untouched
    assert main._handle_home_command("stop telling me jokes", _req(), "s1") is None
    # device named but NO control verb (just chatting about it) -> LLM is fine
    assert main._handle_home_command("the morning automation is my favorite", _req(), "s1") is None


def test_tool_roundtrip_answers_when_the_rules_and_router_both_miss(monkeypatch):
    """The layer the web UI never had. /inbox always called the LLM with tools; /chat/stream called
    it with none, so a phrasing the rules and the semantic router both missed reached a toolless
    model that answered as if it had acted. A tool result now wins over the canned question."""
    main = _home_ready(monkeypatch)
    monkeypatch.setattr(main, "_home_tool_roundtrip", lambda *a, **k: "Okay — running the morning automation now.")
    reply = main._handle_home_command("morning automation stop please now thanks", _req(), "s1")
    assert reply == "Okay — running the morning automation now."


def test_tool_roundtrip_never_fires_on_ordinary_chat(monkeypatch):
    """It costs a full generation on a box that does ~6 tok/s, so it must be reachable ONLY when
    the utterance already carries a control verb AND names an allowlisted device."""
    main = _home_ready(monkeypatch)
    calls = []

    def _spy(*a, **k):
        calls.append(a)
        return "acted"

    monkeypatch.setattr(main, "_home_tool_roundtrip", _spy)
    assert main._handle_home_command("stop telling me jokes", _req(), "s1") is None
    assert main._handle_home_command("what is the weather like today", _req(), "s1") is None
    assert main._handle_home_command("the morning automation is my favorite", _req(), "s1") is None
    assert calls == []


# --- friendly names: the layer real hardware actually needs ----------------------------------

# A real 4-gang switch as Home Assistant exposes it: machine-generated ids, human names only in
# the friendly_name attribute. Resolving on ids alone made "turn on the fan" match NOTHING, and
# threw away the model's own correct tool call (device: "fan").
REAL = ["switch.4node_smart_switch_switch_1", "switch.4node_smart_switch_switch_2",
        "switch.4node_smart_switch_switch_3"]
REAL_NAMES = {REAL[0]: "Light", REAL[1]: "Tube Light", REAL[2]: "Fan"}


def test_friendly_name_resolves_what_the_id_cannot():
    assert ha.resolve_entity("turn off the fan", REAL, REAL_NAMES) == REAL[2]
    assert ha.resolve_entity("fan", REAL, REAL_NAMES) == REAL[2]
    # the id is opaque, so without names this is unreachable — that was the bug
    assert ha.resolve_entity("turn off the fan", REAL, {}) is None


def test_the_more_completely_named_device_wins():
    """'Light' and 'Tube Light' both contain 'light'. Naming one COMPLETELY picks it; naming the
    longer one completely picks that instead. Neither may silently actuate the wrong device."""
    assert ha.resolve_entity("turn on the light", REAL, REAL_NAMES) == REAL[0]
    assert ha.resolve_entity("turn on the tube light", REAL, REAL_NAMES) == REAL[1]
    assert ha.resolve_entity("tube light", REAL, REAL_NAMES) == REAL[1]


def test_friendly_names_do_not_weaken_the_allowlist():
    """A name must never widen what can be actuated: unknown devices and non-allowlisted ids are
    still refused, and a bare shared domain word is still ambiguous."""
    assert ha.resolve_entity("garage door", REAL, REAL_NAMES) is None
    assert ha.resolve_entity("lock.front_door", REAL, REAL_NAMES) is None
    assert ha.resolve_entity("switch", REAL, REAL_NAMES) is None      # all three are switches


def test_display_name_falls_back_to_the_id():
    assert ha.display_name(REAL[2], REAL_NAMES) == "Fan"
    assert ha.display_name("light.kitchen", {}) == "kitchen"


def test_stale_allowlist_entries_are_listed_so_they_can_be_removed(monkeypatch):
    """An allowlisted entity HA no longer knows must still appear in the picker.

    It used to be invisible there (the picker only rendered what /api/states returned) while the
    UI kept saving it back on every write — so it could never be removed. That is not cosmetic:
    see the ambiguity test below.
    """
    live = [{"entity_id": "switch.4node_smart_switch_switch_3",
             "attributes": {"friendly_name": "Fan"}, "state": "on"}]
    monkeypatch.setattr(ha, "_request", lambda *a, **k: live)
    monkeypatch.setattr(ha, "HA_ALLOWED_ENTITIES",
                        ["switch.4node_smart_switch_switch_3", "input_boolean.fan"])
    rows = {e["entity_id"]: e for e in ha.list_entities()}
    assert rows["switch.4node_smart_switch_switch_3"]["available"] is True
    ghost = rows["input_boolean.fan"]
    assert ghost["available"] is False and ghost["allowed"] is True   # rendered, ticked, removable


def test_a_stale_entry_makes_a_real_device_unresolvable():
    """Why the above matters. A dead `input_boolean.fan` and a real switch named "Fan" tie, and
    resolve_entity refuses to guess — so "turn on the fan" stops working entirely, which reads as
    "the allowlist didn't save". Removing the stale entry restores it."""
    real = "switch.4node_smart_switch_switch_3"
    names = {real: "Fan"}
    assert ha.resolve_entity("turn on the fan", [real, "input_boolean.fan"], names) is None
    assert ha.resolve_entity("turn on the fan", [real], names) == real


# --- acting on an entity Home Assistant no longer has ----------------------------------------

def test_act_on_a_missing_entity_reports_failure_not_success(monkeypatch):
    """The corrosive one. HA's generic turn_off answers 200 with an empty body for an entity_id it
    does not have, so Jarvis said "Okay — the fan is now off" having done nothing at all. It must
    now say so, and must NOT call the service at all."""
    import main
    called = []
    monkeypatch.setattr(ha, "probe_entity", lambda e: (ha.ENTITY_MISSING, None))
    monkeypatch.setattr(ha, "turn", lambda e, a: called.append((e, a)) or True)
    ok, eff, err = main._ha_act("input_boolean.fan", "off")
    assert ok is False
    assert called == []                      # never actuated a device HA doesn't have
    assert "doesn't have" in err and "fan" in err


def test_missing_entity_and_unreachable_ha_are_different_sentences(monkeypatch):
    """A stale allowlist entry is fixable in the admin page; an outage is not. Collapsing the two
    into one message sends the user looking in the wrong place."""
    import main
    monkeypatch.setattr(ha, "turn", lambda e, a: True)
    monkeypatch.setattr(ha, "probe_entity", lambda e: (ha.ENTITY_MISSING, None))
    _, _, missing = main._ha_act("switch.gone", "off")
    monkeypatch.setattr(ha, "probe_entity", lambda e: (ha.HA_UNREACHABLE, None))
    _, _, down = main._ha_act("switch.gone", "off")
    assert missing != down
    assert "couldn't reach" in down


def test_a_present_entity_still_acts_normally(monkeypatch):
    import main
    monkeypatch.setattr(ha, "probe_entity", lambda e: (ha.ENTITY_FOUND, {"state": "on"}))
    monkeypatch.setattr(ha, "turn", lambda e, a: True)
    ok, eff, err = main._ha_act("switch.desk_fan", "off")
    assert (ok, eff, err) == (True, "off", None)


def test_probe_entity_maps_404_to_missing_and_other_errors_to_unreachable(monkeypatch):
    """404 means HA answered "no such entity"; anything else means it didn't answer usefully.
    Inferring "deleted" from a timeout would tell users to edit an allowlist that is fine."""
    import urllib.error

    def _raise(exc):
        def _open(*a, **k):
            raise exc
        return _open

    monkeypatch.setattr(ha, "HA_URL", "http://ha.test:8123")
    monkeypatch.setattr(ha, "HA_TOKEN", "tok")
    monkeypatch.setattr(ha.safehttp, "urlopen",
                        _raise(urllib.error.HTTPError("u", 404, "Not Found", None, None)))
    assert ha.probe_entity("switch.gone")[0] == ha.ENTITY_MISSING
    monkeypatch.setattr(ha.safehttp, "urlopen",
                        _raise(urllib.error.HTTPError("u", 500, "Boom", None, None)))
    assert ha.probe_entity("switch.gone")[0] == ha.HA_UNREACHABLE
    monkeypatch.setattr(ha.safehttp, "urlopen", _raise(OSError("no route to host")))
    assert ha.probe_entity("switch.gone")[0] == ha.HA_UNREACHABLE


def test_refresh_names_keeps_the_old_cache_when_ha_is_unreachable(monkeypatch):
    """A transient HA blip must not un-name every device and silently drop resolution back to
    machine ids — that would look exactly like the bug this fixes, intermittently."""
    monkeypatch.setattr(ha, "_FRIENDLY", dict(REAL_NAMES))
    monkeypatch.setattr(ha, "_request", lambda *a, **k: None)         # HA down
    assert ha.refresh_names() == 0
    assert ha.friendly_names() == REAL_NAMES



def test_reply_wording_is_domain_aware():
    """Replies state what actually happened in the entity's own terms."""
    import main
    assert main._ha_reply("automation.morning", "off") == \
        "Okay — the morning automation is disabled. It won't run until you enable it again."
    assert "stays enabled" in main._ha_reply("automation.morning", "stop")
    assert main._ha_reply("input_boolean.test_light", "on") == "Okay — the test light is now on."
    assert main._ha_reply("script.reset_all", "run") == "Okay — I ran the reset all script."
    assert main._ha_state_phrase("automation.morning", {"state": "off"}) == \
        "The morning automation is disabled."
    assert main._ha_state_phrase("input_boolean.test_light",
                                 {"state": "on", "attributes": {"friendly_name": "Test Light"}}) == \
        "Test Light is on."
