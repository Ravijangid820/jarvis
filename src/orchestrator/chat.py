"""Chat sessions, message persistence, and context-window-aware prompt assembly."""
import uuid
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

import ha
import memory
from budget import clamp_completion, estimate_message_tokens, fit_history, truncate_to_tokens
from config import (COMPLETION_RESERVE_DEFAULT, HISTORY_MAX_AGE_HOURS, KNOWLEDGE_TOKEN_CAP,
                    MAX_CONTEXT_MESSAGES,
                    MAX_CONTEXT_TOKENS, MIN_COMPLETION_TOKENS, PROMPT_SAFETY_MARGIN,
                    REASONING, SYSTEM_PROMPT)
from db import get_db


# --- Message persistence ----------------------------------------------------
def store_message(session_id: str, speaker: str, content: str, kind: str = "chat"):
    """Persist one turn. `kind="device"` marks a smart-home command and its templated
    acknowledgement: still shown in the transcript, but withheld from the model (see
    get_recent_context) so it cannot learn to imitate an acknowledgement it did not earn."""
    conn = get_db()
    try:
        cursor = conn.execute(
            "INSERT INTO conversation_history (session_id, speaker, content, kind) VALUES (?, ?, ?, ?)",
            (session_id, speaker, content, kind),
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


def get_recent_context(session_id: str, limit: Optional[int] = None,
                       for_llm: bool = False) -> List[Dict[str, str]]:
    """Recent turns, oldest first.

    `for_llm=True` drops device turns. They are template strings ("Okay - the Light is now off."),
    and a 2B model reading a month of them starts producing them as prose: it emitted that exact
    sentence twice for utterances that were not commands, with no action behind either. Dropping
    the pair — command and acknowledgement — keeps the history a clean alternation of real
    conversation, and the live device block in build_messages carries the truth those turns used
    to carry. The UI still asks without the flag and sees everything.
    """
    limit = limit or MAX_CONTEXT_MESSAGES
    where = "WHERE session_id = ?"
    params: List[Any] = [session_id]
    if for_llm:
        where += " AND kind != 'device'"
        if HISTORY_MAX_AGE_HOURS > 0:
            where += " AND timestamp >= datetime('now', ?)"
            params.append(f"-{HISTORY_MAX_AGE_HOURS} hours")
    params.append(limit)
    conn = get_db()
    try:
        rows = conn.execute(
            f"SELECT speaker, content FROM conversation_history {where} ORDER BY id DESC LIMIT ?",
            params,
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


# NOTE: a previous reset_demo_session() lived here. Under the old instance-wide JARVIS_MODE=demo it
# wiped, on every session read, both this user's older chats AND every chat in the database older
# than 30 minutes. Ephemerality is now a property of the demo HOUSEHOLD — created with an expiry,
# destroyed whole on logout or by the TTL sweeper — so this was redundant. It was also actively
# wrong once demo sessions got a 60-minute TTL: a visitor whose session passed the 30-minute mark
# would have their history deleted underneath them, breaking the guarantee that a refresh keeps
# the conversation. Deleted rather than fixed; the household purge is the single reset path.


def create_session(title: str, user_id: int) -> str:
    session_id = str(uuid.uuid4())
    conn = get_db()
    try:
        conn.execute("INSERT INTO chat_sessions (id, title, user_id) VALUES (?, ?, ?)", (session_id, title, user_id))
        conn.commit()
    finally:
        conn.close()
    return session_id


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
def build_messages(session_id: str, user_id: int, household_id: int, user_text: str,
                   custom_sys_prompt: Optional[str] = None,
                   completion_reserve: int = COMPLETION_RESERVE_DEFAULT,
                   reasoning: Optional[bool] = None,
                   voice: bool = False,
                   device_event: Optional[str] = None) -> List[Dict[str, str]]:
    """Assemble the prompt within the model's context window.

    Layout: [single system message] + [recent history…] + [current turn]. The system message holds
    only the STABLE parts (system prompt + user-profile block) so the server's KV cache prefix stays
    valid across turns; the per-turn RAG memories are attached to the CURRENT user turn instead of the
    leading system message — otherwise they'd change the very first token every turn and force a full
    re-eval of the whole context (Qwen also rejects multiple / non-leading system messages). History is
    added newest-first only while it fits the token budget.
    """
    sys_prompt = custom_sys_prompt if custom_sys_prompt else SYSTEM_PROMPT
    # Reasoning toggle (Qwen "/no_think"): True strips the token (thinking on), False ensures it
    # (thinking off), None leaves the prompt untouched. Lets users flip it from config or UI alone.
    active_reasoning = reasoning if reasoning is not None else REASONING
    if active_reasoning is True:
        sys_prompt = sys_prompt.replace("/no_think", "").strip()
    elif active_reasoning is False and "/no_think" not in sys_prompt:
        sys_prompt = (sys_prompt + " /no_think").strip()
    system_parts = [sys_prompt]
    # Spoken turns need to be shorter than written ones. Every token is read aloud by Piper at
    # roughly the speed the model produces it, so a paragraph that scans fine on screen is close to
    # half a minute of talking — and there is no skimming an answer you have to listen to.
    if voice:
        system_parts.append(
            "This reply will be SPOKEN ALOUD, so keep it to one or two short sentences. "
            "Write it the way you would say it: no lists, no headings, no markdown, no code, "
            "no URLs. If the full answer is long, give the short version and offer the detail.")

    # Household knowledge — shared by everyone IN THIS HOUSEHOLD, admin-curated. Stable across
    # turns, so it stays in the cache-friendly system prefix. (Capped; if it ever outgrows the cap
    # we'd switch to RAG.)
    global_kb = memory.get_global_knowledge(household_id)
    if global_kb:
        global_kb = truncate_to_tokens(global_kb, KNOWLEDGE_TOKEN_CAP)
        system_parts.append(
            "--- HOUSEHOLD KNOWLEDGE ---\n"
            f"{global_kb}\n"
            "---"
        )

    knowledge = memory.get_user_knowledge(user_id)
    if knowledge:
        knowledge = truncate_to_tokens(knowledge, KNOWLEDGE_TOKEN_CAP)
        system_parts.append(
            "--- USER PROFILE ---\n"
            f"{knowledge}\n"
            "---"
        )

    context_ids = _get_recent_message_ids(session_id)
    memories = memory.retrieve_long_term_memory(user_id, session_id, user_text, recent_context_ids=context_ids)
    turn_parts: List[str] = []
    present = memory.get_present_people(household_id)
    if present:
        turn_parts.append(f"[Seen by cameras: {', '.join(present)}]")

    # The devices, and what they are doing RIGHT NOW.
    #
    # Without this the model had no idea a smart home existed: asked to turn on a light it replied
    # that it had "no body or access to real-world devices", and asked how things were it invented
    # "the lights are on, the temperature is set" — both of which the system prompt forbids, and
    # neither of which it could avoid, because nothing in the prompt said otherwise.
    #
    # In the per-turn block rather than the system prefix on purpose: states change, and the system
    # message is deliberately stable so llama.cpp can reuse its KV cache across turns. Putting
    # volatile text there would re-evaluate the whole prefix on every message.
    if ha.configured() and ha.owns(household_id):
        devices = ha.snapshot()
        if devices:
            lines = "\n".join(f"  {d['name']} — {d['state']}" for d in devices)
            turn_parts.append(
                "--- DEVICES IN THIS HOME (live, right now) ---\n"
                f"{lines}\n"
                "These are the only devices there are. Refer to them naturally, by name — never "
                "mention this list, and never say a device is in a state other than the one given "
                "here. The system does the switching and confirms it; you never claim to have "
                "done it yourself.\n"
                "---")
    if memories:
        turn_parts.append(
            "--- RECALLED MEMORIES ---\n"
            f"{memories}\n"
            "---")
    # A smart-home action the system ALREADY carried out for this very message. Stated as
    # completed fact so the model answers the rest of the sentence ("I'm freezing" → sympathy)
    # without re-announcing the switch, and without having to guess whether it worked.
    if device_event:
        turn_parts.append(
            f"[Already done, by the system, for this message: {device_event} "
            "Acknowledge it briefly if it is worth mentioning, then answer the rest of what they "
            "said. Do not repeat it as if you were about to do it.]")

    turn_content = ("\n\n".join(turn_parts) + "\n\n" + user_text) if turn_parts else user_text

    front: List[Dict[str, str]] = [{"role": "system", "content": "\n\n".join(system_parts)}]
    current_turn = {"role": "user", "content": turn_content}

    prompt_budget = MAX_CONTEXT_TOKENS - max(completion_reserve, MIN_COMPLETION_TOKENS) - PROMPT_SAFETY_MARGIN
    prompt_budget = max(prompt_budget, MAX_CONTEXT_TOKENS // 2)
    fixed_tokens = sum(estimate_message_tokens(m) for m in front) + estimate_message_tokens(current_turn)

    history = get_recent_context(session_id, for_llm=True)  # chronological (oldest -> newest)
    included = fit_history(history, prompt_budget - fixed_tokens)
    return front + included + [current_turn]


def clamp_completion_for(messages: List[Dict[str, str]], requested: Optional[int]) -> int:
    """Clamp the requested completion length so prompt + completion fits the context window."""
    prompt_tokens = sum(estimate_message_tokens(m) for m in messages)
    return clamp_completion(prompt_tokens, requested or 0, MAX_CONTEXT_TOKENS,
                            PROMPT_SAFETY_MARGIN, MIN_COMPLETION_TOKENS, COMPLETION_RESERVE_DEFAULT)
