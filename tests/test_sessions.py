"""Authorization tests for session ownership (the IDOR fix in chat.delete_session).

The app hardcodes /srv/jarvis paths and loads a 300M embedding model at import, so we
stub `config` and `memory` before importing `chat`/`db` and point the DB at a temp file.
This keeps the test runnable anywhere (incl. CI), while exercising the real chat logic.
"""
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "orchestrator"
sys.path.insert(0, str(SRC))

# --- stub heavy/path-bound modules before importing the code under test ---
_cfg = types.ModuleType("config")
_cfg.DB_PATH = ":memory:"
_cfg.SCHEMA_PATH = Path("/nonexistent/schema.sql")  # init_db isn't exercised here
_cfg.COMPLETION_RESERVE_DEFAULT = 512
_cfg.KNOWLEDGE_TOKEN_CAP = 512
_cfg.MAX_CONTEXT_MESSAGES = 100
_cfg.MAX_CONTEXT_TOKENS = 4096
_cfg.MIN_COMPLETION_TOKENS = 64
_cfg.PROMPT_SAFETY_MARGIN = 96
_cfg.SYSTEM_PROMPT = "You are Jarvis."
sys.modules["config"] = _cfg

_mem = types.ModuleType("memory")
_mem.delete_vectors = lambda ids: None
sys.modules["memory"] = _mem

import chat  # noqa: E402
from fastapi import HTTPException  # noqa: E402


@pytest.fixture()
def db(monkeypatch):
    """A temp SQLite DB with two users' sessions; chat.get_db points at it."""
    fd, path = tempfile.mkstemp(suffix=".db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE chat_sessions(id TEXT PRIMARY KEY, user_id INT, title TEXT);
        CREATE TABLE conversation_history(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT);
        INSERT INTO chat_sessions VALUES ('owned', 1, 'Mine');
        INSERT INTO chat_sessions VALUES ('victim', 2, 'Theirs');
        INSERT INTO conversation_history(session_id, role, content) VALUES ('victim','user','secret');
        INSERT INTO conversation_history(session_id, role, content) VALUES ('owned','user','hi');
    """)
    conn.commit()

    def get_db():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c
    monkeypatch.setattr(chat, "get_db", get_db)

    yield get_db
    conn.close()


def _count(get_db, session_id):
    c = get_db()
    n = c.execute("SELECT COUNT(*) AS n FROM conversation_history WHERE session_id=?", (session_id,)).fetchone()["n"]
    c.close()
    return n


def test_require_owned_session_allows_owner(db):
    chat.require_owned_session("owned", 1)  # no raise


def test_require_owned_session_blocks_non_owner(db):
    with pytest.raises(HTTPException) as ei:
        chat.require_owned_session("victim", 1)
    assert ei.value.status_code == 403


def test_require_owned_session_blocks_missing(db):
    with pytest.raises(HTTPException) as ei:
        chat.require_owned_session("does-not-exist", 1)
    assert ei.value.status_code == 403


def test_delete_session_blocks_cross_user_and_preserves_data(db, monkeypatch):
    # User 1 attempts to delete user 2's session (IDOR attempt).
    deleted = []
    monkeypatch.setattr(chat.memory, "delete_vectors", lambda ids: deleted.append(ids))
    with pytest.raises(HTTPException) as ei:
        chat.delete_session("victim", 1)
    assert ei.value.status_code == 403
    assert _count(db, "victim") == 1          # victim's messages untouched
    assert deleted == []                        # no vectors removed either


def test_delete_session_owner_removes_messages(db, monkeypatch):
    deleted = []
    monkeypatch.setattr(chat.memory, "delete_vectors", lambda ids: deleted.append(ids))
    chat.delete_session("owned", 1)
    assert _count(db, "owned") == 0             # owner's messages gone
    assert len(deleted) == 1                     # vectors cleaned up once


def test_rename_session_only_affects_owner(db):
    chat.rename_session("victim", "HACKED", 1)   # user 1 tries to rename user 2's session
    c = db()
    title = c.execute("SELECT title FROM chat_sessions WHERE id='victim'").fetchone()["title"]
    c.close()
    assert title == "Theirs"                      # unchanged — guarded by user_id
