"""Keeping llama-server's single KV-cache slot on the conversation.

The server runs with --parallel 1: one slot, one token sequence, and it reuses whatever prefix the
next prompt shares with it. A chat's ~630-token system message is therefore evaluated once — unless
some OTHER request lands in between, at which point the user's next message re-evaluates all of it.
Measured on the author's no-AVX2 box: 60.8 s of prompt eval for 722 tokens, versus 15.3 s for the
170 new tokens of a cache-hitting turn.

Two things sent unrelated prompts into that slot: title generation (on every new chat) and idle
fact extraction. Measured honestly, llama.cpp usually recovers — it restores the chat's prefix
from a context checkpoint on the next turn — so the cost of the title call is mostly its own
5.7 s of slot time, not a guaranteed re-prefill. Checkpoints are bounded though, and a miss costs
the full 57 s.

These tests pin both fixes: a title that needs no model at all, and a warm-up that puts the prefix
back after background work for the cases llama.cpp cannot rescue.
"""
import ast
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "orchestrator"))
os.environ.setdefault("JARVIS_NO_EMBED", "1")

import chat  # noqa: E402
import llm  # noqa: E402
import memory  # noqa: E402


# --------------------------------------------------------------- titles without the model

@pytest.mark.parametrize("text,expected", [
    ("In one sentence, what is a neural network?", "Neural Network"),
    ("what's the weather like today?", "Weather Like Today"),
    ("Hey Jarvis, turn off the kitchen light", "Turn Off Kitchen Light"),
    ("explain how RAG works", "RAG Works"),
])
def test_title_is_derived_from_the_first_message(text, expected):
    assert chat.title_from_text(text) == expected


def test_title_keeps_acronyms_uppercase():
    """"RAG" must not come back as "Rag" — capitalize() would have done that."""
    assert "RAG" in chat.title_from_text("explain how RAG works")


@pytest.mark.parametrize("text", ["", "   ", "!!!", "the a an of to"])
def test_title_always_returns_something_usable(text):
    """Empty or all-stopword input still has to name the session; the sidebar shows this string."""
    assert chat.title_from_text(text).strip()


def test_title_falls_back_to_raw_words_when_all_are_stopwords():
    assert chat.title_from_text("what is the") != "New Chat"  # uses the words rather than giving up


def test_title_is_bounded():
    assert len(chat.title_from_text("supercalifragilistic " * 40)) <= 60


def test_titles_do_not_call_the_llm(monkeypatch):
    """The whole point. A model call here evicts the conversation's cached prefix, and the next
    message pays for it — so this path must not touch the LLM at all."""
    def explode(*a, **k):
        raise AssertionError("title generation must not call the LLM")
    monkeypatch.setattr(llm, "request_llm", explode)
    from routes import chat as routes_chat
    monkeypatch.setattr(routes_chat, "request_llm", explode)
    monkeypatch.delenv("JARVIS_LLM_TITLES", raising=False)
    renamed = {}
    monkeypatch.setattr(chat, "rename_session", lambda s, t, u: renamed.update(t=t))
    assert routes_chat._maybe_title(True, "sid", 1, "what is a neural network?") == "Neural Network"
    assert renamed["t"] == "Neural Network"


def test_no_title_work_when_the_session_already_has_one(monkeypatch):
    from routes import chat as routes_chat
    monkeypatch.setattr(chat, "rename_session",
                        lambda *a: pytest.fail("must not rename an existing session"))
    assert routes_chat._maybe_title(False, "sid", 1, "hello") is None


# --------------------------------------------------------------- the warm-up

def test_warm_prefix_sends_the_system_message_alone_with_one_token(monkeypatch):
    """It must reproduce the head of a real chat prompt: same single system message, so llama.cpp
    templates it to the same tokens and the LCP match covers the whole prefix. max_tokens=1 keeps
    it to a prefill — generating here would waste the slot it is trying to prime."""
    seen = {}

    class _Resp:
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        import json as _json
        seen["body"] = _json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    assert llm.warm_prefix("You are JARVIS.") is True
    assert seen["body"]["messages"] == [{"role": "system", "content": "You are JARVIS."}]
    assert seen["body"]["max_tokens"] == 1
    assert seen["body"]["cache_prompt"] is True


def test_warm_prefix_is_a_no_op_without_a_prefix(monkeypatch):
    """Before the first chat there is nothing to restore, and an empty warm-up would evict the
    very cache it exists to protect."""
    monkeypatch.setattr(llm.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("must not call the LLM with no prefix"))
    assert llm.warm_prefix(None) is False
    assert llm.warm_prefix("") is False


def test_warm_prefix_never_raises(monkeypatch):
    """It runs on a background thread after extraction; a failure there must not kill the worker.
    A missed warm-up costs latency, never correctness."""
    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(llm.urllib.request, "urlopen", boom)
    assert llm.warm_prefix("You are JARVIS.") is False


def test_build_messages_records_the_prefix_it_used(monkeypatch):
    """The warm-up has to replay the EXACT system message, not a reconstruction of it — otherwise
    the tokens differ and the cache is missed anyway."""
    monkeypatch.setattr(chat.memory, "get_global_knowledge", lambda h: "")
    monkeypatch.setattr(chat.memory, "get_user_knowledge", lambda u: "")
    monkeypatch.setattr(chat.memory, "retrieve_long_term_memory", lambda *a, **k: "")
    monkeypatch.setattr(chat, "get_recent_context", lambda *a, **k: [])
    monkeypatch.setattr(chat.ha, "configured", lambda: False)
    msgs = chat.build_messages("sid", 1, 1, "hello", None)
    assert chat.last_system_prefix() == msgs[0]["content"]
    assert msgs[0]["role"] == "system"


# --------------------------------------------------------------- the dependency, inverted

def test_memory_never_reaches_into_chat():
    """The documented invariant: memory depends on config, db and llm, never on chat.

    It was false for a while — extract_facts_batch did a function-local `import chat` to find the
    prefix to warm. That worked, which is exactly why it is worth a test: nothing failed, the
    architecture doc simply stopped being true, and an invariant nobody can rely on buys nothing.
    Import graphs are checked here rather than by reading, because a local import inside a
    function is invisible to every other kind of review.
    """
    src = (Path(__file__).resolve().parents[1] / "src" / "orchestrator" / "memory.py").read_text()
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert "chat" not in imported, "memory imported chat — including inside a function"
    assert "main" not in imported


def test_extraction_warms_the_prefix_through_the_callback(monkeypatch):
    """And the behaviour that motivated the import is still there: after the idle worker has spent
    the slot, the conversation's prefix goes back into it."""
    warmed = []
    monkeypatch.setattr(chat, "warm_prefix", lambda prefix: warmed.append(prefix))
    monkeypatch.setattr(chat, "_LAST_SYSTEM_PREFIX", "You are JARVIS. /no_think")
    memory._notify_llm_displaced()
    assert warmed == ["You are JARVIS. /no_think"]


def test_a_failing_hook_cannot_lose_the_extraction_results(monkeypatch):
    """The warm-up runs after the facts have been written. A hook that raises must not propagate
    into the worker, where it would look like the extraction itself failed."""
    def boom():
        raise RuntimeError("llama-server is down")
    monkeypatch.setattr(memory, "_llm_displaced_hooks", [boom])
    memory._notify_llm_displaced()      # must not raise
