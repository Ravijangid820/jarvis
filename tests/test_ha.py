"""Home Assistant integration — the security-relevant pure logic.

No network, no TestClient: resolve_entity is pure (allowlist injected), and the client
functions are exercised with a monkeypatched urlopen. The gates themselves
(_can_control_devices etc.) are covered by the existing API tests.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "orchestrator"))

import ha  # noqa: E402

ALLOW = ["input_boolean.test_light", "light.kitchen", "light.living_room", "switch.desk_fan"]


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
    monkeypatch.setattr(ha.urllib.request, "urlopen", fake_urlopen)
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
    monkeypatch.setattr(ha.urllib.request, "urlopen", boom)
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
    names = [t["function"]["name"] for t in main._active_tools()]
    assert "set_volume" in names and "home_control" not in names
    # configure at RUNTIME (what the admin UI does) -> tools appear on the next request
    monkeypatch.setattr(ha, "HA_URL", "http://ha.test:8123")
    monkeypatch.setattr(ha, "HA_TOKEN", "tok")
    names = [t["function"]["name"] for t in main._active_tools()]
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
    monkeypatch.setattr(main, "_can_control_devices", lambda r: True)
    monkeypatch.setattr(main, "REQUIRE_PRESENCE_FOR_CONTROL", False)
    monkeypatch.setattr(main, "_audit", lambda *a, **k: None)
    main._LAST_HOME_ENTITY.clear()

    # no referent yet -> asks, does NOT act, does NOT fall through (None would mean LLM)
    reply = main._handle_home_command("switch it off", None, "s1")
    assert reply is not None and "which device" in reply.lower() and turned == []

    # name the device -> acts + remembers
    assert "fan on" in main._handle_home_command("switch on the fan", None, "s1").lower()
    assert turned == [("input_boolean.desk_fan", "on")]

    # pronoun now resolves to the fan
    assert "fan off" in main._handle_home_command("switch it off", None, "s1").lower()
    assert turned[-1] == ("input_boolean.desk_fan", "off")

    # a DIFFERENT session has no referent -> asks again (no cross-session leakage)
    reply = main._handle_home_command("turn it on", None, "s2")
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
    monkeypatch.setattr(ha.urllib.request, "urlopen", fake_urlopen)
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
    monkeypatch.setattr(ha, "turn", lambda e, a: actions.append((a, e)) or True)
    monkeypatch.setattr(main, "_can_control_devices", lambda r: True)
    monkeypatch.setattr(main, "REQUIRE_PRESENCE_FOR_CONTROL", False)
    monkeypatch.setattr(main, "_audit", lambda *a, **k: None)
    main._LAST_HOME_ENTITY.clear()

    reply = main._handle_home_command("run the movie night automation", None, "s1")
    assert "ran movie night" in reply.lower()
    assert actions[-1] == ("run", "automation.movie_night")

    reply = main._handle_home_command("start the fan", None, "s1")   # run on a plain device = on
    assert "fan on" in reply.lower()
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
    monkeypatch.setattr(ha, "stop", lambda e: actions.append(("stop", e)) or True)
    monkeypatch.setattr(main, "_can_control_devices", lambda r: True)
    monkeypatch.setattr(main, "REQUIRE_PRESENCE_FOR_CONTROL", False)
    monkeypatch.setattr(main, "_audit", lambda *a, **k: None)
    main._LAST_HOME_ENTITY.clear()

    reply = main._handle_home_command("stop morning automation", None, "s1")
    assert "stopped morning" in reply.lower()
    assert actions[-1] == ("stop", "automation.morning")     # NOT ("off", ...) — stays enabled

    assert "morning off" in main._handle_home_command("disable the morning automation", None, "s1").lower()
    assert actions[-1] == ("off", "automation.morning")      # explicit disable -> off

    assert "morning on" in main._handle_home_command("enable morning automation", None, "s1").lower()
    assert actions[-1] == ("on", "automation.morning")

    assert "fan off" in main._handle_home_command("stop the fan", None, "s1").lower()
    assert actions[-1] == ("off", "switch.desk_fan")          # plain device: stop = off


def test_ha_stop_service_sequence(monkeypatch):
    """automation stop = turn_off(stop_actions) THEN turn_on (re-arm); script stop = script.turn_off."""
    calls = []
    def fake_urlopen(req, timeout=None):
        calls.append((req.full_url, json.loads(req.data.decode())))
        return _FakeResp({})
    monkeypatch.setattr(ha, "HA_URL", "http://ha.test:8123")
    monkeypatch.setattr(ha, "HA_TOKEN", "tok")
    monkeypatch.setattr(ha.urllib.request, "urlopen", fake_urlopen)
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


def test_antibluff_guard_asks_instead_of_reaching_the_llm(monkeypatch):
    """Device named + control verb, but unparseable phrasing -> a clarification, NOT None
    (None would fall through to the toolless streaming LLM, which bluffs acks)."""
    import main
    monkeypatch.setattr(ha, "HA_URL", "http://ha.test:8123")
    monkeypatch.setattr(ha, "HA_TOKEN", "tok")
    monkeypatch.setattr(ha, "HA_ALLOWED_ENTITIES", ["automation.morning"])
    reply = main._handle_home_command("morning automation stop please now thanks", None, "s1")
    assert reply is not None and "morning" in reply.lower()

    # ordinary sentences (no allowlisted device) still reach the LLM untouched
    assert main._handle_home_command("stop telling me jokes", None, "s1") is None
    # device named but NO control verb (just chatting about it) -> LLM is fine
    assert main._handle_home_command("the morning automation is my favorite", None, "s1") is None
