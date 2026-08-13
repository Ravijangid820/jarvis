"""Greetings are answered by the server, never by the model.

The bug that produced this, from the first human test of the chat page:

    You:    I
    Jarvis: The tube light remains off, as do the switches and fan.
    You:    Hey Jarvis
    Jarvis: The tube light remains off, as do the switches and fan.

Handed a contentless turn, a 2B model reaches for whatever context is in front of it. Three
generations against the real model, same greeting:

    with the device block          "Sir, the system is monitoring the home. No devices are active."
    with a "BACKGROUND ONLY" hint  "sir, the tube light, switch board, and fan are all off."
    with NO device block at all    "Sir, I am already active. The lights, temperature, and
                                    security systems are running as configured."

So the device block is not the cause — removing it makes things worse, because the model then
invents hardware that does not exist. The system prompt already says "Never invent status,
readings, sensor values or events" and is ignored; instruction-following at this size cannot be
relied on. The fix is to stop asking: these never reach the model.
"""
import json
import sys
from pathlib import Path

import pytest

from test_api import _tok, main
from test_api import client as _app_client  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "orchestrator"))

import chat  # noqa: E402
from routes import chat as routes_chat  # noqa: E402
from intents import GREETING_REPLIES, greeting_reply, is_greeting  # noqa: E402


@pytest.mark.parametrize("text", [
    "hey jarvis", "Hey Jarvis", "hey jarvis!", "hi", "hello", "yo jarvis",
    "good morning", "good evening", "hello there", "jarvis",
    "are you there", "you there jarvis", "wake up jarvis", "jarvis are you there",
])
def test_greetings_are_recognised(text):
    assert is_greeting(text)


@pytest.mark.parametrize("text", ["I", "i", "uh", "um", "ok", "hmm"])
def test_content_free_noise_is_treated_as_a_greeting(text):
    """A bare "I" was one of the messages that produced a recitation of the whole house. There is
    nothing to answer, so answering briefly beats inventing something."""
    assert is_greeting(text)


@pytest.mark.parametrize("text", [
    "hey jarvis, turn off the fan",
    "jarvis turn off the fan",
    "hi, what's the weather",
    "hello, how are you",
    "is the fan on?",
    "what is a neural network",
    "turn off the light",
    "good morning, remind me to call mum at 9",
])
def test_anything_carrying_content_falls_through(text):
    """The strictness that makes this safe. A greeting that continues into a command must reach
    the command path, and a question must reach the model — swallowing either with "Sir." would
    be a far worse bug than the one this fixes."""
    assert not is_greeting(text)


def test_empty_input_is_not_a_greeting():
    """Empty text is rejected earlier as a 400; it must not be answered with a pleasantry."""
    assert not is_greeting("")
    assert not is_greeting("   ")


def test_replies_are_short_and_in_character():
    for reply in GREETING_REPLIES:
        assert len(reply) <= 20
        assert "sir" in reply.lower()


def test_reply_comes_from_the_fixed_set():
    assert greeting_reply() in GREETING_REPLIES


# --------------------------------------------------------------------------------------------
# The wiring, over HTTP.
#
# This half used to be two assertions that the SOURCE of chat.py and main.py contained particular
# strings. They passed whether or not the code ran — a fast path moved behind an unreachable
# branch, or a route that stopped calling it, would not have shown up. The requests below go
# through the real app instead.


@pytest.fixture(scope="module")
def client(_app_client):  # noqa: F811
    return _app_client


@pytest.fixture(autouse=True)
def _reset_limiters():
    """Both limiters are per-process module globals keyed by IP, and every test in the suite
    arrives from the same one. Without this the eighth login in a full run is a 429."""
    main._login_store.clear()
    main._rate_store.clear()
    yield


@pytest.fixture(scope="module")
def token(client):
    return _tok(client, "tony", "pw-admin")


@pytest.fixture()
def session(client, token):
    """A fresh chat session per test, so no transcript leaks between them."""
    sid = client.post("/sessions", headers={"Authorization": "Bearer " + token}).json()["id"]
    return token, sid


@pytest.fixture()
def no_llm(monkeypatch):
    """Every route into llama-server, wired to a recorder. The fast path must touch none of them."""
    calls = []

    def _spy(name):
        def _fn(*a, **k):
            calls.append(name)
            raise AssertionError(f"the greeting fast path reached the model via {name}")
        return _fn

    for name in ("request_llm", "request_llm_tools", "request_llm_stream"):
        monkeypatch.setattr(routes_chat, name, _spy(name))
    return calls


def _sse(response):
    """The content chunks of an SSE body, in order."""
    out = []
    for line in response.text.splitlines():
        if line.startswith("data: "):
            evt = json.loads(line[6:])
            if "content" in evt:
                out.append(evt["content"])
    return out


def test_inbox_answers_a_greeting_without_the_model(client, session, no_llm):
    """The point of the fast path: a greeting must not spend the single llama slot (12-17 s of
    prefill on this box) to say "Sir."."""
    tok, sid = session
    r = client.post("/inbox", json={"text": "hi", "session_id": sid},
                    headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 200
    assert r.json()["response"] in GREETING_REPLIES
    assert no_llm == []


def test_chat_stream_answers_a_greeting_without_the_model(client, session, no_llm):
    """The two routes must agree. /chat/stream is what the web UI actually calls, and for a while
    it was the one path where a fix like this could be added to /inbox alone and look done."""
    tok, sid = session
    r = client.post("/chat/stream", json={"text": "hi", "session_id": sid},
                    headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 200
    chunks = _sse(r)
    assert len(chunks) == 1 and chunks[0] in GREETING_REPLIES
    assert no_llm == []


def test_both_routes_give_the_same_kind_of_answer(client, session, no_llm):
    tok, sid = session
    inbox = client.post("/inbox", json={"text": "hey jarvis", "session_id": sid},
                        headers={"Authorization": "Bearer " + tok}).json()["response"]
    stream = _sse(client.post("/chat/stream", json={"text": "hey jarvis", "session_id": sid},
                              headers={"Authorization": "Bearer " + tok}))[0]
    assert inbox in GREETING_REPLIES and stream in GREETING_REPLIES


def test_greeting_turns_are_withheld_from_the_model(client, session, no_llm):
    """Stored and shown, but never replayed to the model — the same rule device acknowledgements
    live under. Feeding templates back is what taught it to emit "Okay - the Light is now off."
    for messages that were not commands; a screenful of "Sir." would teach the same trick."""
    tok, sid = session
    client.post("/inbox", json={"text": "hi", "session_id": sid},
                headers={"Authorization": "Bearer " + tok})
    # Both turns are in the transcript the UI reads...
    assert len(chat.get_recent_context(sid)) == 2
    # ...and neither is in the history the model is shown.
    assert chat.get_recent_context(sid, for_llm=True) == []


def test_a_message_with_content_still_reaches_the_model(client, session, monkeypatch):
    """The guard on all of the above. is_greeting() is strict on purpose, and a fast path that
    swallowed real questions would be a far worse bug than the one it fixes."""
    tok, sid = session
    asked = []

    def _fake_llm(messages, *a, **k):
        asked.append(messages)
        return {"choices": [{"message": {"content": "A neural network is a model."}}]}

    monkeypatch.setattr(routes_chat, "request_llm_tools", _fake_llm)
    r = client.post("/inbox", json={"text": "what is a neural network", "session_id": sid},
                    headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 200
    assert r.json()["response"] == "A neural network is a model."
    assert len(asked) == 1
    # ...and an ordinary turn IS replayed to the model next time.
    assert len(chat.get_recent_context(sid, for_llm=True)) == 2
