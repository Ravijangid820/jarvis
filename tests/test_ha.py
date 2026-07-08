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
