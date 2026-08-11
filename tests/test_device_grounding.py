"""Grounding the model in the smart home, and keeping template replies out of its head.

The behaviour under test comes from a real transcript in which the assistant:
  - denied it could control anything ("I don't have a body or access to real-world devices"),
  - invented status it had no source for ("the lights are on, the temperature is set"), and
  - twice emitted "Okay - the Light is now off." for utterances that were NOT commands, with no
    action behind either.

The first two were the prompt never mentioning the devices. The third was the deterministic
acknowledgements being fed back as assistant prose until the model learned to produce them.
"""
import sys
from pathlib import Path

import pytest

from test_api import _tok, main
from test_api import client as _app_client  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "orchestrator"))
import chat  # noqa: E402
import ha  # noqa: E402
from intents import command_residual, says_more_than_command  # noqa: E402


@pytest.fixture(scope="module")
def client(_app_client):  # noqa: F811
    return _app_client


@pytest.fixture()
def owner(client):
    """A real user id to hang test sessions off — chat_sessions.user_id is a foreign key."""
    _tok(client, "tony", "pw-admin")
    conn = main.get_db()
    try:
        return conn.execute("SELECT id FROM users WHERE username='tony'").fetchone()["id"]
    finally:
        conn.close()


# --- "did they say more than the command?" ---------------------------------------------------
# Pure, so it can be pinned exhaustively. Wrong in the permissive direction costs one LLM turn;
# wrong in the strict direction answers a person with a template when they said something real.

@pytest.mark.parametrize("text,device", [
    ("can you turn off the light?", "Light"),
    ("switch it back on.", "Light"),
    ("Turn off the fan.", "Fan"),
    ("turn on the tube light", "Tube Light"),
    ("please switch off the light", "Light"),
    ("toggle the fan now", "Fan"),
])
def test_bare_commands_keep_the_instant_reply(text, device):
    assert says_more_than_command(text, device) is False, command_residual(text, device)


@pytest.mark.parametrize("text,device", [
    ("I am feeling a bit cold so can you please turn off the fan.", "Fan"),
    ("turn off the light, I'm going to sleep now", "Light"),
    ("switch off the light and tell me a joke", "Light"),
    ("the room is too bright, kill the tube light", "Tube Light"),
])
def test_sentences_that_say_more_earn_a_composed_reply(text, device):
    assert says_more_than_command(text, device) is True


def test_the_device_name_itself_is_not_extra_content():
    """Without stripping the resolved name, every command for a multi-word device ("tube light")
    would look like it carried extra content and pay for an LLM turn it did not need."""
    assert command_residual("turn on the tube light", "Tube Light") == ""
    assert command_residual("turn on the tube light", "") != ""


# --- the live device block --------------------------------------------------------------------
def _fake_home(monkeypatch, rows, household=1):
    monkeypatch.setattr(ha, "configured", lambda: True)
    monkeypatch.setattr(ha, "owns", lambda hid: hid == household)
    monkeypatch.setattr(ha, "snapshot", lambda *a, **k: rows)


ROWS = [{"entity_id": "switch.a", "name": "Fan", "state": "on", "domain": "switch"},
        {"entity_id": "switch.b", "name": "Tube Light", "state": "off", "domain": "switch"}]


def test_the_model_is_told_which_devices_exist_and_their_state(client, monkeypatch):
    _fake_home(monkeypatch, ROWS)
    admin = _tok(client, "tony", "pw-admin")
    del admin
    msgs = chat.build_messages("grounding-1", 1, 1, "what's on?")
    turn = msgs[-1]["content"]
    assert "Fan — on" in turn and "Tube Light — off" in turn
    # It must also be told the boundary, or it offers to do things it will then be refused.
    assert "only devices" in turn


def test_a_household_without_the_smart_home_is_told_nothing(client, monkeypatch):
    """The device list names the rooms and appliances of one home. A household that does not own
    the smart home must not receive it in a prompt — the same boundary the HTTP surface enforces."""
    _fake_home(monkeypatch, ROWS, household=1)
    msgs = chat.build_messages("grounding-2", 1, 999, "what's on?")
    assert "Fan" not in msgs[-1]["content"]


def test_no_block_at_all_when_home_assistant_is_unreachable(client, monkeypatch):
    """An empty snapshot means "we could not ask", not "the house is empty". Printing an empty
    list would assert something false and invite the model to say the house has no devices."""
    _fake_home(monkeypatch, [])
    msgs = chat.build_messages("grounding-3", 1, 1, "what's on?")
    assert "DEVICES IN THIS HOME" not in msgs[-1]["content"]


def test_a_completed_action_is_stated_as_already_done(client, monkeypatch):
    _fake_home(monkeypatch, ROWS)
    msgs = chat.build_messages("grounding-4", 1, 1, "I'm cold, turn the fan off",
                               device_event="Okay — the Fan is now off.")
    turn = msgs[-1]["content"]
    assert "Already done" in turn and "the Fan is now off" in turn


# --- keeping templates out of the model's history ---------------------------------------------
def test_device_turns_are_hidden_from_the_model_but_kept_for_the_user(client, owner):
    sid = "kindsplit-1"
    conn = main.get_db()
    try:
        conn.execute("INSERT OR IGNORE INTO chat_sessions (id, title, user_id) VALUES (?,?,?)",
                     (sid, "t", owner))
        conn.commit()
    finally:
        conn.close()
    chat.store_message(sid, "user", "turn off the light", kind="device")
    chat.store_message(sid, "jarvis", "Okay — the Light is now off.", kind="device")
    chat.store_message(sid, "user", "what's the capital of France?")
    chat.store_message(sid, "jarvis", "Paris.")

    for_ui = chat.get_recent_context(sid)
    for_llm = chat.get_recent_context(sid, for_llm=True)
    assert any("Light is now off" in m["content"] for m in for_ui), "the transcript must be complete"
    assert not any("Light is now off" in m["content"] for m in for_llm), \
        "the template must never be offered back to the model as its own prose"
    # Both halves of the exchange go, so history stays a clean alternation rather than developing
    # a run of user messages nobody answered.
    assert not any("turn off the light" in m["content"] for m in for_llm)
    assert any("Paris" in m["content"] for m in for_llm), "ordinary conversation is untouched"


def test_history_older_than_the_cutoff_is_left_to_rag(client, owner):
    """A never-closed 'quick chat' session accumulated a month of turns, which both cost 194s to
    re-evaluate and taught the model to imitate replies written before the current persona."""
    sid = "agecut-1"
    conn = main.get_db()
    try:
        conn.execute("INSERT OR IGNORE INTO chat_sessions (id, title, user_id) VALUES (?,?,?)",
                     (sid, "t", owner))
        conn.execute("INSERT INTO conversation_history (session_id, speaker, content, kind, timestamp) "
                     "VALUES (?,?,?, 'chat', datetime('now','-40 days'))", (sid, "user", "ancient turn"))
        conn.execute("INSERT INTO conversation_history (session_id, speaker, content, kind) "
                     "VALUES (?,?,?, 'chat')", (sid, "user", "todays turn"))
        conn.commit()
    finally:
        conn.close()
    for_llm = chat.get_recent_context(sid, for_llm=True)
    assert any("todays turn" in m["content"] for m in for_llm)
    assert not any("ancient turn" in m["content"] for m in for_llm)
    # The user's own transcript is never truncated by this — only the model's working memory is.
    assert any("ancient turn" in m["content"] for m in chat.get_recent_context(sid))


# --- background embedding must yield to the LLM ------------------------------------------------
# Two no-AVX2 cores are the whole constraint of this box. Embedding a 300M model is hundreds of ms
# of CPU, and it was firing the instant a message was stored — precisely while the model was
# generating the reply to that same message.

import memory  # noqa: E402


def test_embedding_waits_while_a_generation_is_in_flight(monkeypatch):
    slept = []
    monkeypatch.setattr(memory.time, "sleep", lambda s: slept.append(s))
    busy = iter([True, True, True, False])
    monkeypatch.setattr(memory, "is_busy", lambda: next(busy, False))
    waited = memory._wait_for_llm_idle()
    assert waited > 0, "must not embed on top of a live generation"
    assert len(slept) == 3, "should stop waiting as soon as the LLM goes idle"


def test_embedding_is_never_blocked_forever_by_a_stuck_counter(monkeypatch):
    """is_busy() is a counter. If a request ever failed to decrement it, an uncapped wait would
    stop the vector store being written to again — silently, and for the life of the process."""
    monkeypatch.setattr(memory.time, "sleep", lambda s: None)
    monkeypatch.setattr(memory, "is_busy", lambda: True)      # never goes idle
    waited = memory._wait_for_llm_idle(max_wait=5.0)
    assert waited >= 5.0, "must give up and embed rather than lose the memory"


def test_an_idle_box_embeds_immediately(monkeypatch):
    monkeypatch.setattr(memory, "is_busy", lambda: False)
    calls = []
    monkeypatch.setattr(memory.time, "sleep", lambda s: calls.append(s))
    assert memory._wait_for_llm_idle() == 0.0
    assert calls == [], "no delay when there is nothing to yield to"


# --- fact extraction must not lose what it already understood ---------------------------------
# Found live: the extractor hit n_predict mid-object, the whole reply failed json.loads, every fact
# in it was discarded, and the messages were marked processed anyway — so they could never be
# retried. Two correct facts about the operator were destroyed that way, silently, because the
# "unextracted" counter reached zero either way.

TRUNCATED = (
    '```json\n[\n'
    '  {"category": "personal", "content": "The user\'s name is Ravi Jangid."},\n'
    '  {"category": "work", "content": "The user builds and operates Jarvis."},\n'
    '  {"category": "other", "content": "The user'
)


def test_a_truncated_reply_keeps_the_facts_it_finished():
    facts, complete = memory._parse_facts(TRUNCATED)
    assert complete is False, "the caller must be able to tell it did not see the whole batch"
    assert [f["content"] for f in facts] == [
        "The user's name is Ravi Jangid.",
        "The user builds and operates Jarvis.",
    ], "complete objects are salvageable; only the half-written one is dropped"


def test_a_whole_reply_parses_normally_and_reports_complete():
    facts, complete = memory._parse_facts('[{"category":"work","content":"The user likes Rust."}]')
    assert complete is True and len(facts) == 1


def test_a_fenced_reply_is_unwrapped():
    facts, complete = memory._parse_facts('```json\n[{"category":"work","content":"Uses uv."}]\n```')
    assert complete is True and facts[0]["content"] == "Uses uv."


def test_a_brace_inside_a_fact_does_not_end_it_early():
    """Salvage scans braces, so it has to respect strings — otherwise a fact mentioning a "}"
    would be cut in half and stored as garbage."""
    facts, _ = memory._parse_facts(
        '[{"category":"other","content":"He wrote a } brace."}, {"category":"work","content":"ok"}]'[:-1])
    assert [f["content"] for f in facts] == ["He wrote a } brace.", "ok"]


def test_an_empty_or_junk_reply_yields_nothing_rather_than_raising():
    for junk in ("", "no facts here", "```json\n```", "[[[["):
        facts, _ = memory._parse_facts(junk)
        assert facts == [], junk


def test_a_message_is_written_off_only_after_repeated_failures():
    """Unbounded retry would hand the same unparseable message to the LLM every idle cycle for the
    life of the process — on a box where one call costs tens of seconds."""
    memory._extract_attempts.clear()
    msg_id = 4242
    attempts = [memory._too_many_attempts(msg_id) for _ in range(memory.FACT_EXTRACTION_MAX_ATTEMPTS)]
    assert attempts[:-1] == [False] * (memory.FACT_EXTRACTION_MAX_ATTEMPTS - 1), "must retry first"
    assert attempts[-1] is True, "and eventually give up"
    memory._extract_attempts.clear()


# --- the extraction budget is derived, not guessed ---------------------------------------------
from budget import estimate_message_tokens  # noqa: E402
from config import (FACT_EXTRACTION_MIN_TOKENS, FACT_EXTRACTION_PROMPT,  # noqa: E402
                    FACT_EXTRACTION_TOKENS, MAX_CONTEXT_TOKENS, PROMPT_SAFETY_MARGIN)


def _plan(batch_size, chars=40):
    """What extract_facts_batch would actually send: the trimmed batch and its derived budget."""
    msgs = [{"id": i, "content": "x" * chars} for i in range(batch_size)]
    kept, text = memory._fit_batch(1, msgs)
    llm_messages = [{"role": "system", "content": FACT_EXTRACTION_PROMPT},
                    {"role": "user", "content": text}]
    prompt = sum(estimate_message_tokens(m) for m in llm_messages)
    return kept, prompt, min(FACT_EXTRACTION_TOKENS, MAX_CONTEXT_TOKENS - prompt - PROMPT_SAFETY_MARGIN)


def test_prompt_plus_reply_can_never_exceed_the_context_window():
    """llama.cpp does not error when prompt + completion overflow — it silently drops tokens. The
    original bug was a fixed 512 that fit one message and truncated a dozen; the batch is now
    trimmed so the arithmetic cannot come out negative in the first place."""
    for n, chars in [(1, 40), (6, 40), (12, 40), (40, 200), (6, 10000), (200, 2000)]:
        kept, prompt, n_predict = _plan(n, chars)
        assert kept, "at least one message must always be processable"
        assert n_predict >= FACT_EXTRACTION_MIN_TOKENS, f"{n}x{chars} left no room to answer"
        assert prompt + n_predict <= MAX_CONTEXT_TOKENS, f"{n}x{chars} would overflow the window"


def test_a_normal_batch_is_taken_whole_and_gets_the_full_allowance():
    kept, _, n_predict = _plan(6, 40)
    assert len(kept) == 6, "small batches must not be trimmed"
    assert n_predict == FACT_EXTRACTION_TOKENS, "nor needlessly throttled"


def test_oversized_batches_are_trimmed_rather_than_truncated_by_the_model():
    """Six admin-sized messages (10,000 chars each) exceed the window on their own. The surplus
    must stay queued for the next pass, not be sent and lost."""
    kept, _, _ = _plan(6, 10000)
    assert 0 < len(kept) < 6, f"expected a partial batch, got {len(kept)}"


def test_a_single_unprocessably_long_message_is_truncated_not_skipped_forever():
    kept, prompt, n_predict = _plan(1, 60000)
    assert len(kept) == 1, "one huge message must still make progress, or it blocks the queue"
    assert prompt + n_predict <= MAX_CONTEXT_TOKENS


# --- fact shape tolerance ----------------------------------------------------------------------
def _store_from(monkeypatch, reply_text):
    """Run extract_facts_batch against a canned LLM reply; return what it tried to store."""
    stored = []
    monkeypatch.setattr(memory, "request_llm", lambda *a, **k: {"choices": [{"message": {"content": reply_text}}]})
    monkeypatch.setattr(memory, "llm_content", lambda r: r["choices"][0]["message"]["content"])
    monkeypatch.setattr(memory, "store_fact", lambda uid, cat, content, source="auto": stored.append((cat, content)))
    monkeypatch.setattr(memory, "_mark_messages_processed", lambda ids: None)
    memory.extract_facts_batch([{"id": 1, "user_id": 7, "content": "I like Rust."}])
    return stored


def test_a_bare_array_of_sentences_is_kept_not_discarded(monkeypatch):
    """Observed live: the model answers with plain strings about as often as with objects, and the
    dict-only filter threw five correct facts away without logging a thing."""
    stored = _store_from(monkeypatch, '["The user likes Rust.", "The user wants to learn inference."]')
    assert [c for _, c in stored] == ["The user likes Rust.", "The user wants to learn inference."]
    assert all(cat == "other" for cat, _ in stored), "uncategorised facts land in 'other'"


def test_objects_still_carry_their_category(monkeypatch):
    stored = _store_from(monkeypatch, '[{"category":"work","content":"The user is an engineer."}]')
    assert stored == [("work", "The user is an engineer.")]


def test_a_mixed_reply_keeps_both_shapes(monkeypatch):
    stored = _store_from(monkeypatch,
                         '[{"category":"work","content":"The user is an engineer."}, "The user likes Rust."]')
    assert len(stored) == 2 and ("other", "The user likes Rust.") in stored


def test_entries_that_are_neither_are_skipped_without_crashing(monkeypatch):
    stored = _store_from(monkeypatch, '[123, null, {"content":"The user likes Rust."}, "ok"]')
    kept = [c for _, c in stored]
    assert "The user likes Rust." in kept
    assert not any(c in ("123", "None") for c in kept)


# --- absences are not facts --------------------------------------------------------------------
# The model produced three of these in a batch where it had already extracted the opposite, so they
# contradicted real facts sitting beside them in the same profile block.

@pytest.mark.parametrize("content", [
    "The user has not stated any rules about downloading models or binaries.",
    "The user has not specified which smart-home devices they have.",
    "The user wants to learn a programming language, but the specific language was not stated.",
    "The goal remains unclear.",
    "No information was provided about their location.",
])
def test_absence_shaped_facts_are_dropped(content):
    assert memory._is_absence(content) is True


@pytest.mark.parametrize("content", [
    "The user refuses to download models from third-party mirrors.",
    "The user avoids using Codespaces because it is billed hourly.",
    "The user does not use pip; they use uv.",
    "The user has no GPU in their home server.",
    "The user's home server has AVX but lacks AVX2 support.",
    "The user is not planning to rewrite Jarvis in Rust.",
])
def test_negative_preferences_are_real_facts_and_survive(content):
    """The filter must catch 'they did not SAY x', never 'they do not DO x' — half of what this
    user has told the assistant is a refusal, and those are among the most useful facts there are."""
    assert memory._is_absence(content) is False


def test_an_absence_never_reaches_storage(monkeypatch):
    stored = _store_from(monkeypatch,
                         '[{"category":"work","content":"The user is an engineer."},'
                         ' {"category":"other","content":"The user has not stated their location."}]')
    assert [c for _, c in stored] == ["The user is an engineer."]
