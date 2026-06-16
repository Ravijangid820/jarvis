"""Unit tests for the pure prompt-budgeting helpers (no app import, no model load)."""
import budget


def test_estimate_tokens_monotonic_and_min():
    assert budget.estimate_tokens("") == 1
    assert budget.estimate_tokens("a") == 1
    assert budget.estimate_tokens("a" * 40) == 10  # (40 + 3) // 4
    assert budget.estimate_tokens("x" * 100) > budget.estimate_tokens("x" * 10)


def test_estimate_message_tokens_includes_overhead():
    assert budget.estimate_message_tokens({"content": ""}) == 5  # 1 + 4 overhead
    assert budget.estimate_message_tokens({}) == 5


def test_truncate_to_tokens():
    short = "hello world"
    assert budget.truncate_to_tokens(short, 100) == short
    long = "x" * 1000
    out = budget.truncate_to_tokens(long, 10)  # ~40 chars budget
    assert out.endswith("…(truncated)")
    assert len(out) < len(long)


def test_is_default_session():
    assert budget.is_default_session("default")
    assert budget.is_default_session("u5-default")
    assert not budget.is_default_session("abc-123-uuid")


def test_fit_history_keeps_newest_within_budget():
    history = [{"content": f"msg {i}"} for i in range(10)]  # each ~ 2 + 4 = 6 tokens
    # Budget for ~2 messages.
    included = budget.fit_history(history, 13)
    assert included == [{"content": "msg 8"}, {"content": "msg 9"}]  # newest, chronological
    # Zero budget -> nothing.
    assert budget.fit_history(history, 0) == []
    # Huge budget -> everything, order preserved.
    assert budget.fit_history(history, 10_000) == history


def test_clamp_completion_respects_window():
    # Plenty of headroom: honor the request.
    assert budget.clamp_completion(100, 512, 4096, 96, 64, 512) == 512
    # No request -> default.
    assert budget.clamp_completion(100, 0, 4096, 96, 64, 512) == 512
    # Prompt nearly fills the window -> clamp down, but never below min.
    assert budget.clamp_completion(4000, 512, 4096, 96, 64, 512) == 64
    # Requested larger than headroom -> clamp to headroom.
    got = budget.clamp_completion(2000, 4000, 4096, 96, 64, 512)
    assert got == 4096 - 2000 - 96
