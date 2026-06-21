import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "orchestrator"))

from intents import is_gesture_volume, parse_volume  # noqa: E402


def test_set_absolute():
    for text, val in [
        ("set volume to 50%", 50),
        ("set the volume to 30 percent", 30),
        ("jarvis volume 40", 40),
        ("change volume to 0", 0),
        ("volume at 75", 75),
    ]:
        assert parse_volume(text) == {"action": "set", "value": val}, text


def test_set_words():
    assert parse_volume("max volume") == {"action": "set", "value": 100}
    assert parse_volume("volume all the way up")["value"] == 100   # max wins over 'up'
    assert parse_volume("set volume to minimum") == {"action": "set", "value": 0}
    assert parse_volume("set volume to half") == {"action": "set", "value": 50}


def test_step():
    assert parse_volume("volume up") == {"action": "step", "value": 10}
    assert parse_volume("turn the volume down") == {"action": "step", "value": -10}
    assert parse_volume("louder") == {"action": "step", "value": 10}
    assert parse_volume("make it quieter") == {"action": "step", "value": -10}
    assert parse_volume("volume up by 20") == {"action": "step", "value": 20}
    assert parse_volume("turn the volume down 15") == {"action": "step", "value": -15}


def test_mute():
    assert parse_volume("mute") == {"action": "mute"}
    assert parse_volume("mute the volume") == {"action": "mute"}
    assert parse_volume("silence") == {"action": "mute"}
    assert parse_volume("unmute") == {"action": "unmute"}
    assert parse_volume("turn the sound back on") == {"action": "unmute"}


def test_clamps():
    assert parse_volume("set volume to 250") == {"action": "set", "value": 100}
    assert parse_volume("volume up by 999") == {"action": "step", "value": 100}


def test_gesture_volume_trigger():
    for text in ["volume", "volume control", "control the volume", "gesture volume",
                 "control the volume with gestures", "hand volume", "volume mode"]:
        assert is_gesture_volume(text), text
    # concrete commands are NOT gesture mode (parse_volume handles them first anyway)
    for text in ["set volume to 50", "volume up", "mute", "what is the volume", "lower the blinds"]:
        assert not is_gesture_volume(text), text


def test_non_volume_falls_through():
    # ambiguous / unrelated → None so it goes to the LLM
    for text in ["turn up the heat", "lower the blinds", "what's the weather",
                 "what is the volume", "tell me a joke", "", "set a timer for 10 minutes"]:
        assert parse_volume(text) is None, text
