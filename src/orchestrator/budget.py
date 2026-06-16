"""Pure, dependency-free helpers for prompt token budgeting.

Extracted from the orchestrator so they can be unit-tested without importing the
FastAPI app (which loads ChromaDB and a 300M embedding model at import time).
All functions here are pure: no I/O, no globals, no side effects.
"""
from typing import Dict, List


def estimate_tokens(text: str) -> int:
    """Rough, deliberately conservative token estimate (~4 chars/token for English)."""
    return max(1, (len(text) + 3) // 4)


def estimate_message_tokens(msg: Dict[str, str]) -> int:
    # +4 approximates the per-message role/format overhead of the chat template.
    return estimate_tokens(msg.get("content", "")) + 4


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Trim text so its estimated token count fits max_tokens (keeps the head)."""
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + " …(truncated)"


def is_default_session(session_id: str) -> bool:
    """A user's lazily-created personal scratch session (not auto-titled)."""
    return session_id == "default" or session_id.endswith("-default")


def fit_history(history: List[Dict[str, str]], remaining_tokens: int) -> List[Dict[str, str]]:
    """Pick as many of the most-recent history messages as fit `remaining_tokens`.

    `history` is chronological (oldest -> newest). Returns a chronological sublist of the
    newest messages whose cumulative estimated token cost stays within the budget.
    """
    included: List[Dict[str, str]] = []
    for msg in reversed(history):           # newest first
        cost = estimate_message_tokens(msg)
        if cost > remaining_tokens:
            break
        remaining_tokens -= cost
        included.append(msg)
    included.reverse()                      # restore chronological order
    return included


def clamp_completion(prompt_tokens: int, requested: int, max_context_tokens: int,
                     safety_margin: int, min_completion: int, default: int) -> int:
    """Clamp a requested completion length so prompt + completion fits the context window."""
    want = requested if (requested and requested > 0) else default
    headroom = max_context_tokens - prompt_tokens - safety_margin
    return max(min_completion, min(want, headroom))
