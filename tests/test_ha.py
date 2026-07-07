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
