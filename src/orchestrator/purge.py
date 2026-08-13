"""Deleting an account or a household, and the id reuse that follows.

Shared by the admin routes and the demo-household sweeper, which is why it is a module rather than
a handful of helpers next to one of them: the two paths must delete exactly the same things, and a
purge that misses a table is invisible until someone finds the leftovers.
"""
from typing import List

import memory
from config import logger
from db import get_db


USER_REF_TABLES = ("chat_sessions", "auth_sessions", "api_keys", "user_knowledge",
                    "persons", "vision_events")


def purge_user(conn, user_id: int) -> List[str]:
    """Delete EVERYTHING tied to user_id so a freed id carries no residue. Personal data (chats,
    knowledge, keys, sessions) is removed; faces and camera events are UNLINKED (user_id→NULL) so the
    household's recognition data survives but no longer points at the account. Returns the message ids
    to drop from ChromaDB (caller commits, then calls memory.delete_vectors)."""
    msg_ids = [str(r["id"]) for r in conn.execute(
        "SELECT id FROM conversation_history WHERE session_id IN "
        "(SELECT id FROM chat_sessions WHERE user_id = ?)", (user_id,)).fetchall()]
    conn.execute("DELETE FROM conversation_history WHERE session_id IN "
                 "(SELECT id FROM chat_sessions WHERE user_id = ?)", (user_id,))
    conn.execute("DELETE FROM chat_sessions WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM api_keys WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM user_knowledge WHERE user_id = ?", (user_id,))
    conn.execute("UPDATE persons SET user_id = NULL WHERE user_id = ?", (user_id,))
    conn.execute("UPDATE vision_events SET user_id = NULL WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return msg_ids


# Tables carrying household_id that a household purge must clear. persons is listed even though
# purge_user unlinks (rather than deletes) faces: unlinking is right when ONE member leaves a home
# that continues to exist, but a demo household's faces belong to a member of the public and must
# actually be destroyed with it.
HOUSEHOLD_REF_TABLES = ("global_knowledge", "persons", "vision_events",
                         "device_commands", "device_heartbeats", "audit_log", "household_settings")


def purge_household(conn, household_id: int) -> tuple:
    """Delete a household and everything in it. Returns (chroma message ids, member user ids) so
    the caller can clear the vector store both ways.

    This is the demo reset primitive: logout and TTL expiry both route here, so "reset" means the
    same thing however it was triggered. Every member is purged through purge_user first (which
    owns the user-scoped tables), then the household-scoped rows, then the household itself.
    """
    member_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM users WHERE household_id = ?", (household_id,)).fetchall()]
    msg_ids: List[str] = []
    for uid in member_ids:
        msg_ids.extend(purge_user(conn, uid))
    # Faces are DESTROYED here, not unlinked: face_embeddings is biometric data belonging to a
    # member of the public, and purge_user's unlink semantics would leave it behind with a NULL
    # user_id. Delete the embeddings before the persons rows they hang off.
    conn.execute("DELETE FROM face_embeddings WHERE person_id IN "
                 "(SELECT id FROM persons WHERE household_id = ?)", (household_id,))
    for table in HOUSEHOLD_REF_TABLES:      # fixed allowlist, not user input
        conn.execute(f"DELETE FROM {table} WHERE household_id = ?", (household_id,))
    conn.execute("DELETE FROM households WHERE id = ?", (household_id,))
    return msg_ids, member_ids


def purge_household_now(household_id: int) -> int:
    """Purge a household in its own transaction and drop its vectors. Returns the number of
    messages removed. Used by demo logout and the TTL sweeper."""
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        msg_ids, member_ids = purge_household(conn, household_id)
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error("purge of household %s failed: %s", household_id, e)
        return 0
    finally:
        conn.close()
    # Both deletes, deliberately. The id list is precise but only covers what existed when it was
    # taken; an embedding still in the worker queue lands in Chroma AFTER the purge and would
    # survive it. The per-user sweep catches those, so a demo visitor's utterances are not
    # recallable once their session ends.
    memory.delete_vectors(msg_ids)
    memory.delete_vectors_for_users(member_ids)
    return len(msg_ids)


def id_has_residue(conn, uid: int) -> bool:
    """True if any user-scoped table still holds rows for uid (defense-in-depth before id reuse)."""
    for t in USER_REF_TABLES:   # table names are a fixed allowlist, not user input
        if conn.execute(f"SELECT 1 FROM {t} WHERE user_id = ? LIMIT 1", (uid,)).fetchone():
            return True
    return False


def lowest_free_user_id(conn) -> int:
    """Smallest positive id that's neither in use nor carrying residue — so a reused id is provably
    clean. (Reuse is the operator's choice; this makes it safe.)"""
    used = {r["id"] for r in conn.execute("SELECT id FROM users")}
    nid = 1
    while nid in used or id_has_residue(conn, nid):
        nid += 1
    return nid
