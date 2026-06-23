"""Chat sessions, message persistence, and context-window-aware prompt assembly."""
import uuid
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

import memory
from budget import clamp_completion, estimate_message_tokens, fit_history, truncate_to_tokens
from config import (COMPLETION_RESERVE_DEFAULT, KNOWLEDGE_TOKEN_CAP, MAX_CONTEXT_MESSAGES,
                    MAX_CONTEXT_TOKENS, MIN_COMPLETION_TOKENS, PROMPT_SAFETY_MARGIN,
                    SYSTEM_PROMPT)
from db import get_db


# --- Message persistence ----------------------------------------------------
def store_message(session_id: str, speaker: str, content: str):
    conn = get_db()
    try:
        cursor = conn.execute(
            "INSERT INTO conversation_history (session_id, speaker, content) VALUES (?, ?, ?)",
            (session_id, speaker, content),
        )
        msg_id = cursor.lastrowid
        user_id_row = conn.execute("SELECT user_id FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()
        conn.commit()
    finally:
        conn.close()
    # Heavy embedding is handed off to the background worker (never blocks the response).
    if user_id_row is not None:
        metadata = {"session_id": session_id, "speaker": speaker, "user_id": int(user_id_row["user_id"])}
        memory.enqueue_embedding(msg_id, content, metadata)


def get_recent_context(session_id: str, limit: Optional[int] = None) -> List[Dict[str, str]]:
    limit = limit or MAX_CONTEXT_MESSAGES
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT speaker, content FROM conversation_history WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [{"role": "assistant" if r["speaker"] == "jarvis" else "user", "content": r["content"]}
                for r in reversed(rows)]
    finally:
        conn.close()


def _get_recent_message_ids(session_id: str) -> set:
    """IDs already in the recent context window so RAG can skip them."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id FROM conversation_history WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, MAX_CONTEXT_MESSAGES),
        ).fetchall()
        return {str(r["id"]) for r in rows}
    finally:
        conn.close()


# --- Session CRUD -----------------------------------------------------------
def get_sessions(user_id: int) -> List[Dict[str, Any]]:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, title, created_at FROM chat_sessions WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def create_session(title: str, user_id: int) -> str:
    session_id = str(uuid.uuid4())
    conn = get_db()
    try:
        conn.execute("INSERT INTO chat_sessions (id, title, user_id) VALUES (?, ?, ?)", (session_id, title, user_id))
        conn.commit()
        return session_id
    finally:
        conn.close()


def resolve_session(session_id: Optional[str], user_id: int) -> str:
    """Map a missing/'default' session to THIS user's own default session (created lazily),
    so every code path goes through the same ownership check with no special cases."""
    if not session_id or session_id == "default":
        sid = f"u{user_id}-default"
        conn = get_db()
        try:
            if not conn.execute("SELECT 1 FROM chat_sessions WHERE id = ?", (sid,)).fetchone():
                conn.execute("INSERT INTO chat_sessions (id, title, user_id) VALUES (?, ?, ?)",
                             (sid, "Quick Chat", user_id))
                conn.commit()
        finally:
            conn.close()
        return sid
    return session_id


def require_owned_session(session_id: str, user_id: int):
    """Raise 403 unless the session exists AND belongs to user_id. No fail-open on missing rows."""
    conn = get_db()
    try:
        row = conn.execute("SELECT user_id FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()
        if not row or row["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")
    finally:
        conn.close()


def rename_session(session_id: str, title: str, user_id: int):
    conn = get_db()
    try:
        conn.execute("UPDATE chat_sessions SET title = ? WHERE id = ? AND user_id = ?", (title, session_id, user_id))
        conn.commit()
    finally:
        conn.close()


def delete_session(session_id: str, user_id: int):
    # Authorize first: without this the history/vector deletes below run on any
    # session_id, letting one user wipe another user's messages (IDOR).
    require_owned_session(session_id, user_id)
    conn = get_db()
    try:
        msg_ids = [str(r["id"]) for r in conn.execute(
            "SELECT id FROM conversation_history WHERE session_id = ?", (session_id,)).fetchall()]
        conn.execute("DELETE FROM chat_sessions WHERE id = ? AND user_id = ?", (session_id, user_id))
        conn.execute("DELETE FROM conversation_history WHERE session_id = ?", (session_id,))
        conn.commit()
    finally:
        conn.close()
    memory.delete_vectors(msg_ids)  # outside the txn; best-effort vector cleanup


# --- Prompt assembly --------------------------------------------------------
def build_messages(session_id: str, user_id: int, user_text: str, custom_sys_prompt: Optional[str] = None,
                   completion_reserve: int = COMPLETION_RESERVE_DEFAULT) -> List[Dict[str, str]]:
    """Assemble the prompt within the model's context window.

    Layout: [single system message] + [recent history…] + [current turn]. The system message holds
    only the STABLE parts (system prompt + user-profile block) so the server's KV cache prefix stays
    valid across turns; the per-turn RAG memories are attached to the CURRENT user turn instead of the
    leading system message — otherwise they'd change the very first token every turn and force a full
    re-eval of the whole context (Qwen also rejects multiple / non-leading system messages). History is
    added newest-first only while it fits the token budget.
    """
    sys_prompt = custom_sys_prompt if custom_sys_prompt else SYSTEM_PROMPT
    system_parts = [sys_prompt]

    # Household/global knowledge — shared by everyone, admin-curated. Stable across turns, so it stays
    # in the cache-friendly system prefix. (Capped; if it ever outgrows the cap we'd switch to RAG.)
    global_kb = memory.get_global_knowledge()
    if global_kb:
        global_kb = truncate_to_tokens(global_kb, KNOWLEDGE_TOKEN_CAP)
        system_parts.append(
            "--- HOUSEHOLD KNOWLEDGE (shared, about this home) ---\n"
            f"{global_kb}\n"
            "(Common facts about the home and the people in it. Use naturally.)\n"
            "---"
        )

    knowledge = memory.get_user_knowledge(user_id)
    if knowledge:
        knowledge = truncate_to_tokens(knowledge, KNOWLEDGE_TOKEN_CAP)
        system_parts.append(
            "--- USER PROFILE (persistent knowledge) ---\n"
            f"{knowledge}\n"
            "(Use this information naturally. Do not repeat it back unless asked.)\n"
            "---"
        )

    context_ids = _get_recent_message_ids(session_id)
    memories = memory.retrieve_long_term_memory(user_id, session_id, user_text, recent_context_ids=context_ids)
    # Dynamic context (presence + recalled memories) rides with the current turn, NOT the system prefix,
    # so the KV cache stays reusable. Stored history keeps the clean user_text, so the prefix is stable.
    turn_parts: List[str] = []
    present = memory.get_present_people()
    if present:
        turn_parts.append(f"[Seen by the cameras right now: {', '.join(present)}. "
                          "Address the person naturally if relevant.]")
    if memories:
        turn_parts.append(
            "--- RECALLED MEMORIES ---\n"
            f"{memories}\n"
            "(If the current conversation contradicts these, prioritize the current conversation.)\n"
            "---")
    turn_content = ("\n\n".join(turn_parts) + "\n\n" + user_text) if turn_parts else user_text

    front: List[Dict[str, str]] = [{"role": "system", "content": "\n\n".join(system_parts)}]
    current_turn = {"role": "user", "content": turn_content}

    prompt_budget = MAX_CONTEXT_TOKENS - max(completion_reserve, MIN_COMPLETION_TOKENS) - PROMPT_SAFETY_MARGIN
    prompt_budget = max(prompt_budget, MAX_CONTEXT_TOKENS // 2)
    fixed_tokens = sum(estimate_message_tokens(m) for m in front) + estimate_message_tokens(current_turn)

    history = get_recent_context(session_id)  # chronological (oldest -> newest)
    included = fit_history(history, prompt_budget - fixed_tokens)
    return front + included + [current_turn]


def clamp_completion_for(messages: List[Dict[str, str]], requested: Optional[int]) -> int:
    """Clamp the requested completion length so prompt + completion fits the context window."""
    prompt_tokens = sum(estimate_message_tokens(m) for m in messages)
    return clamp_completion(prompt_tokens, requested or 0, MAX_CONTEXT_TOKENS,
                            PROMPT_SAFETY_MARGIN, MIN_COMPLETION_TOKENS, COMPLETION_RESERVE_DEFAULT)
