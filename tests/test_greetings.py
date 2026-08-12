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
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "orchestrator"))

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


def test_greeting_turns_are_withheld_from_the_model():
    """Stored and shown, but never replayed to the model — the same rule device acknowledgements
    live under. Feeding templates back is what taught it to emit "Okay - the Light is now off."
    for messages that were not commands; a screenful of "Sir." would teach the same trick."""
    src = (Path(__file__).resolve().parents[1] / "src" / "orchestrator" / "chat.py").read_text()
    assert "kind NOT IN ('device', 'greeting')" in src


def test_no_llm_call_for_a_greeting():
    """The point of the fast path: a greeting must not spend the single llama slot (12-17 s of
    prefill on this box) to say "Sir."."""
    import main
    src = (Path(__file__).resolve().parents[1] / "src" / "orchestrator" / "main.py").read_text()
    assert src.count("greeting_reply() if is_greeting(user_text) else None") == 2, \
        "both /inbox and /chat/stream must short-circuit, or the two paths answer differently"
    assert hasattr(main, "greeting_reply")
