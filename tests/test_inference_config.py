import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "orchestrator"))

import llm  # noqa: E402


def test_sampling_defaults_fill_in_and_caller_overrides(monkeypatch):
    """Config sampling defaults apply when the caller passes None; an explicit value still wins."""
    monkeypatch.setattr(llm, "SAMPLING_DEFAULTS", {"top_k": 40, "repeat_penalty": 1.1, "max_tokens": 256})
    msgs = [{"role": "user", "content": "hi"}]

    p = llm._build_payload(msgs, None, None, None, None, None, None, None, None, None, False)
    assert p["top_k"] == 40 and p["repeat_penalty"] == 1.1 and p["max_tokens"] == 256

    p2 = llm._build_payload(msgs, None, 99, None, None, None, None, None, None, None, False)
    assert p2["top_k"] == 99  # caller wins over the config default


def test_no_sampling_defaults_keeps_payload_minimal(monkeypatch):
    """Empty defaults (the back-compat case) omit the optional keys entirely."""
    monkeypatch.setattr(llm, "SAMPLING_DEFAULTS", {})
    p = llm._build_payload([{"role": "user", "content": "hi"}], None, None, None, None, None, None, None, None, None, False)
    for k in ("top_k", "top_p", "repeat_penalty", "max_tokens", "seed"):
        assert k not in p


def _apply_reasoning(reasoning, prompt):
    # mirrors chat.build_messages' reasoning handling
    if reasoning is True:
        return prompt.replace("/no_think", "").strip()
    if reasoning is False and "/no_think" not in prompt:
        return (prompt + " /no_think").strip()
    return prompt


def test_reasoning_toggle():
    assert _apply_reasoning(True, "You are Jarvis. /no_think") == "You are Jarvis."
    assert _apply_reasoning(False, "You are Jarvis.") == "You are Jarvis. /no_think"
    assert _apply_reasoning(False, "You are Jarvis. /no_think") == "You are Jarvis. /no_think"  # idempotent
    assert _apply_reasoning(None, "You are Jarvis. /no_think") == "You are Jarvis. /no_think"  # untouched
