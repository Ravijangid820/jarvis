"""Server-microphone selection: enumeration, staging a choice, and the index-drift guard.

The device list is faked throughout. Real enumeration needs a sound card, and CI has none — but the
part worth testing isn't SDL, it's what we do with what SDL says: that the choice is admin-only,
that it is stored by NAME as well as index, and that an index which has come to mean a different
microphone is not blindly reused.
"""
import json

import pytest

from test_api import _tok, main
# Reuse test_api's booted app rather than standing up a second one. Aliased and re-exposed so the
# F811 that ruff raises for an imported fixture named in a parameter list is confined to this one
# line instead of every test below it.
from test_api import client as _app_client  # noqa: F401


@pytest.fixture(scope="module")
def client(_app_client):  # noqa: F811
    return _app_client

TWO_MICS = {
    "driver": "alsa",
    "devices": [{"id": 0, "name": "HDA Intel PCH"}, {"id": 1, "name": "Boya BY-M1"}],
    "error": None,
}


@pytest.fixture(autouse=True)
def _clean_selection():
    """Each test starts with no microphone chosen, and leaves none behind."""
    main.ACTIVE_MIC_PATH.unlink(missing_ok=True)
    yield
    main.ACTIVE_MIC_PATH.unlink(missing_ok=True)


def _fake_devices(monkeypatch, payload):
    monkeypatch.setattr(main, "_list_capture_devices", lambda: payload)


def test_listing_and_selecting_are_admin_only(client, monkeypatch):
    _fake_devices(monkeypatch, TWO_MICS)
    h = {"Authorization": "Bearer " + _tok(client, "pepper", "pw-user")}
    assert client.get("/voice/mics", headers=h).status_code == 403
    assert client.post("/voice/mics/select", headers=h, json={"capture_id": 1}).status_code == 403


def test_selecting_a_mic_stores_name_and_index_and_reports_it_active(client, monkeypatch):
    _fake_devices(monkeypatch, TWO_MICS)
    h = {"Authorization": "Bearer " + _tok(client, "tony", "pw-admin")}
    # Nothing chosen yet → no selection, nothing active.
    first = client.get("/voice/mics", headers=h).json()
    assert first["selected"] is None and [m["name"] for m in first["mics"]] == ["HDA Intel PCH", "Boya BY-M1"]
    assert not any(m["active"] for m in first["mics"])

    r = client.post("/voice/mics/select", headers=h, json={"capture_id": 1})
    assert r.status_code == 200 and r.json()["status"] == "restart_required"
    # The NAME is persisted, not just the index — that is what survives a device being unplugged.
    assert json.loads(main.ACTIVE_MIC_PATH.read_text()) == {"capture_id": 1, "name": "Boya BY-M1"}

    after = client.get("/voice/mics", headers=h).json()
    assert after["selected"]["name"] == "Boya BY-M1" and after["stale"] is False
    assert [m["name"] for m in after["mics"] if m["active"]] == ["Boya BY-M1"]


def test_active_follows_the_name_when_indices_shift(client, monkeypatch):
    """Unplug a device ahead of the chosen one and every index after it slides down. The selection
    must follow the microphone, not the number — otherwise the listener silently opens whatever
    inherited index 1."""
    _fake_devices(monkeypatch, TWO_MICS)
    h = {"Authorization": "Bearer " + _tok(client, "tony", "pw-admin")}
    client.post("/voice/mics/select", headers=h, json={"capture_id": 1})     # Boya, at index 1

    _fake_devices(monkeypatch, {"driver": "alsa", "error": None,
                                "devices": [{"id": 0, "name": "Boya BY-M1"}]})   # onboard card gone
    got = client.get("/voice/mics", headers=h).json()
    assert got["stale"] is False                                   # it IS here, just moved
    assert [m["id"] for m in got["mics"] if m["active"]] == [0]     # active follows the name to 0


def test_a_missing_mic_is_reported_stale_rather_than_silently_reassigned(client, monkeypatch):
    _fake_devices(monkeypatch, TWO_MICS)
    h = {"Authorization": "Bearer " + _tok(client, "tony", "pw-admin")}
    client.post("/voice/mics/select", headers=h, json={"capture_id": 1})

    _fake_devices(monkeypatch, {"driver": "alsa", "error": None,
                                "devices": [{"id": 0, "name": "Some Other Mic"}]})
    got = client.get("/voice/mics", headers=h).json()
    assert got["stale"] is True
    assert not any(m["active"] for m in got["mics"])   # the survivor must NOT inherit the selection


def test_selecting_a_device_that_is_not_there_is_rejected(client, monkeypatch):
    _fake_devices(monkeypatch, TWO_MICS)
    h = {"Authorization": "Bearer " + _tok(client, "tony", "pw-admin")}
    assert client.post("/voice/mics/select", headers=h, json={"capture_id": 7}).status_code == 404
    assert not main.ACTIVE_MIC_PATH.exists()
    # -1 is always valid: it means "the system default", and is the way back from a bad choice
    # even when the device list is empty or wrong.
    assert client.post("/voice/mics/select", headers=h, json={"capture_id": -1}).status_code == 200
    assert json.loads(main.ACTIVE_MIC_PATH.read_text())["capture_id"] == -1


def test_no_sound_card_is_an_answer_not_an_error(client, monkeypatch):
    """A container with no /dev/snd passthrough is the state every fresh deployment starts in. The
    endpoint must describe it rather than fail, so the UI can explain the passthrough."""
    _fake_devices(monkeypatch, {"driver": "alsa", "devices": [], "error": None})
    h = {"Authorization": "Bearer " + _tok(client, "tony", "pw-admin")}
    r = client.get("/voice/mics", headers=h)
    assert r.status_code == 200 and r.json()["mics"] == [] and r.json()["driver"] == "alsa"


def test_enumeration_failure_surfaces_as_text(client, monkeypatch):
    _fake_devices(monkeypatch, {"driver": None, "devices": [], "error": "audio enumeration timed out after 10s"})
    h = {"Authorization": "Bearer " + _tok(client, "tony", "pw-admin")}
    r = client.get("/voice/mics", headers=h)
    assert r.status_code == 200 and "timed out" in r.json()["error"]


# --- The listener side: voice_bridge resolves the stored choice at startup -------------------
# The endpoint above only records a decision. This is the code that acts on it, and it is where
# opening the wrong microphone would actually happen.

import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "scripts"))
import voice_bridge  # noqa: E402


def _bridge_sees(monkeypatch, tmp_path, chosen, devices):
    """Point voice_bridge at a temp selection file and fake `list_mics.py`'s output."""
    sel = tmp_path / "active_mic.json"
    if chosen is not None:
        sel.write_text(json.dumps(chosen))
    monkeypatch.setattr(voice_bridge, "ACTIVE_MIC", sel)
    monkeypatch.delenv("VOICE_CAPTURE_ID", raising=False)

    class _Proc:
        stdout = json.dumps({"driver": "alsa", "devices": devices, "error": None})
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())


def test_bridge_uses_the_system_default_when_nothing_is_chosen(monkeypatch, tmp_path):
    _bridge_sees(monkeypatch, tmp_path, None, [{"id": 0, "name": "Boya BY-M1"}])
    assert voice_bridge.resolve_capture_id() == -1


def test_bridge_follows_the_named_mic_to_its_new_index(monkeypatch, tmp_path):
    _bridge_sees(monkeypatch, tmp_path, {"capture_id": 2, "name": "Boya BY-M1"},
                 [{"id": 0, "name": "Webcam"}, {"id": 1, "name": "Boya BY-M1"}])
    assert voice_bridge.resolve_capture_id() == 1        # NOT the stored 2


def test_bridge_falls_back_rather_than_opening_a_reused_index(monkeypatch, tmp_path):
    """The failure this whole design exists to prevent: the chosen mic is gone, something else now
    sits at its index, and the assistant must not quietly listen through that instead."""
    _bridge_sees(monkeypatch, tmp_path, {"capture_id": 1, "name": "Boya BY-M1"},
                 [{"id": 0, "name": "Webcam"}, {"id": 1, "name": "Conference Speakerphone"}])
    assert voice_bridge.resolve_capture_id() == -1


def test_env_override_wins(monkeypatch, tmp_path):
    _bridge_sees(monkeypatch, tmp_path, {"capture_id": 0, "name": "Boya BY-M1"},
                 [{"id": 0, "name": "Boya BY-M1"}])
    monkeypatch.setenv("VOICE_CAPTURE_ID", "3")
    assert voice_bridge.resolve_capture_id() == 3


def test_unreadable_selection_does_not_stop_the_listener(monkeypatch, tmp_path):
    sel = tmp_path / "active_mic.json"
    sel.write_text("{ not json")
    monkeypatch.setattr(voice_bridge, "ACTIVE_MIC", sel)
    monkeypatch.delenv("VOICE_CAPTURE_ID", raising=False)
    assert voice_bridge.resolve_capture_id() == -1


# --- The user-facing source list + the server-mic audio stream ------------------------------
# /voice/mics (above) is the admin choosing the LISTENER's device. These are the separate, per-user
# feature: "capture my voice on the server's mic instead of my laptop's".

ALSA_TWO = {"driver": "alsa", "devices": [], "error": None,
            "alsa": [{"device": "plughw:0,0", "name": "Onboard Analog", "card": "PCH"},
                     {"device": "plughw:1,0", "name": "Boya BY-M1", "card": "Boya"}]}


def test_voice_inputs_is_open_to_ordinary_members(client, monkeypatch):
    """Unlike the listener's device, this one is NOT admin-only: which mic picks up your own voice
    is the speaker's call, which is the whole point of the feature."""
    _fake_devices(monkeypatch, ALSA_TWO)
    h = {"Authorization": "Bearer " + _tok(client, "pepper", "pw-user")}
    r = client.get("/voice/inputs", headers=h)
    assert r.status_code == 200
    assert [i["name"] for i in r.json()["inputs"]] == ["Onboard Analog", "Boya BY-M1"]


def test_streaming_an_unlisted_device_is_refused(client, monkeypatch):
    """The device string becomes an ffmpeg argument. Only values we enumerated are accepted, so no
    caller-supplied text can become a flag — note the second case looks like a plausible device."""
    _fake_devices(monkeypatch, ALSA_TWO)
    h = {"Authorization": "Bearer " + _tok(client, "pepper", "pw-user")}
    for bogus in ("plughw:9,9", "-f", "/dev/snd/pcmC0D0c", "plughw:0,0 -y /etc/passwd"):
        r = client.get(f"/voice/server-mic/stream?device={bogus}", headers=h)
        assert r.status_code == 404, f"{bogus!r} should not be accepted"


def test_only_one_session_may_hold_the_server_mic(client, monkeypatch):
    _fake_devices(monkeypatch, ALSA_TWO)
    h = {"Authorization": "Bearer " + _tok(client, "pepper", "pw-user")}
    assert main._SERVER_MIC_LOCK.acquire(blocking=False)      # pretend another session has it
    try:
        r = client.get("/voice/server-mic/stream?device=plughw:0,0", headers=h)
        assert r.status_code == 409
        assert client.get("/voice/inputs", headers=h).json()["busy"] is True
    finally:
        main._SERVER_MIC_LOCK.release()
    assert client.get("/voice/inputs", headers=h).json()["busy"] is False


def test_missing_ffmpeg_is_reported_not_crashed(client, monkeypatch):
    _fake_devices(monkeypatch, ALSA_TWO)
    monkeypatch.setattr(main.shutil, "which", lambda _n: None)
    h = {"Authorization": "Bearer " + _tok(client, "pepper", "pw-user")}
    r = client.get("/voice/server-mic/stream?device=plughw:0,0", headers=h)
    assert r.status_code == 503 and "ffmpeg" in r.json()["detail"]
    # A refused request must not strand the lock — the next session has to be able to take it.
    assert main._SERVER_MIC_LOCK.acquire(blocking=False)
    main._SERVER_MIC_LOCK.release()
