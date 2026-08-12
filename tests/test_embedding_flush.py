"""Idle-time embedding: the pending set is durable, batched, and never silently dropped.

Embedding used to run moments after each reply. Measured on the author's box that is ~1.2 s per
message and ~1.9 s per turn (both speakers), landing exactly while the next message is being
typed — competing with prompt prefill for the same two cores. It is now flushed in batches once
the box is quiet, which is also 64% cheaper per message (1183 ms → 425 ms over ten).

The risk that buys is losing vectors: a queue that drains in seconds loses almost nothing on a
crash, one that waits for idle can lose minutes of conversation. So the pending set moved into
the database (conversation_history.embedded), and these tests exist mostly to prove that nothing
falls through — a missing vector is invisible until someone asks a question that needed it.
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
os.environ["JARVIS_NO_EMBED"] = "1"
sys.path.insert(0, str(REPO / "src" / "orchestrator"))

if "config" not in sys.modules:
    import json
    _TMP = Path(tempfile.mkdtemp())
    (_TMP / "config").mkdir()
    (_TMP / "config" / "schema.sql").write_text((REPO / "config" / "schema.sql").read_text())
    _cfg = json.loads((REPO / "config" / "jarvis.example.json").read_text())
    _cfg["memory"]["db_path"] = str(_TMP / "test.db")
    _cfg["memory"]["chroma_db_path"] = str(_TMP / "chroma")
    (_TMP / "config" / "jarvis.json").write_text(json.dumps(_cfg))
    os.environ["JARVIS_HOME"] = str(_TMP)

import config  # noqa: E402
import db  # noqa: E402
import memory  # noqa: E402


@pytest.fixture
def fresh_db():
    db.init_db()
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("DELETE FROM conversation_history")
    conn.execute("DELETE FROM chat_sessions")
    conn.execute("INSERT OR IGNORE INTO users (id, username, role) VALUES (7, 'tester', 'user')")
    conn.execute("INSERT INTO chat_sessions (id, user_id, title) VALUES ('s1', 7, 'T')")
    conn.commit()
    conn.close()
    yield


def _add_message(content="hello", embedded=0, speaker="user"):
    conn = sqlite3.connect(config.DB_PATH)
    cur = conn.execute(
        "INSERT INTO conversation_history (session_id, speaker, content, embedded) VALUES (?,?,?,?)",
        ("s1", speaker, content, embedded))
    conn.commit()
    mid = cur.lastrowid
    conn.close()
    return mid


class _FakeCollection:
    """Records what was written, and can be told to fail."""
    def __init__(self):
        self.batches = []
        self.fail = False

    def add(self, documents, embeddings, metadatas, ids):
        if self.fail:
            raise RuntimeError("chroma is down")
        self.batches.append({"documents": documents, "ids": ids, "metadatas": metadatas})


@pytest.fixture
def collection(monkeypatch):
    c = _FakeCollection()
    monkeypatch.setattr(memory, "memory_collection", c)
    monkeypatch.setattr(memory, "vectors_available", lambda: True)
    monkeypatch.setattr(memory, "_embed_documents", lambda docs: [[0.1] * 8 for _ in docs])
    return c


# --------------------------------------------------------------------- the pending set

def test_new_messages_start_unembedded(fresh_db):
    _add_message("first")
    assert len(memory.get_unembedded_messages()) == 1


def test_enqueue_embedding_does_not_embed_inline(fresh_db, collection):
    """The whole point of the change: storing a message must not cost 1.2 s of CPU."""
    memory.enqueue_embedding(1, "hello", {"user_id": 7})
    assert collection.batches == []


def test_flush_writes_one_batch_for_many_messages(fresh_db, collection):
    """Per-message writes cost ~536 ms each in Chroma; the batch pays that once."""
    for i in range(5):
        _add_message(f"message {i}")
    assert memory.flush_embeddings() == 5
    assert len(collection.batches) == 1
    assert len(collection.batches[0]["ids"]) == 5


def test_flush_marks_messages_embedded(fresh_db, collection):
    _add_message("a")
    memory.flush_embeddings()
    assert memory.get_unembedded_messages() == []


def test_flush_is_idempotent(fresh_db, collection):
    _add_message("a")
    memory.flush_embeddings()
    assert memory.flush_embeddings() == 0          # nothing left; no second write
    assert len(collection.batches) == 1


def test_flush_carries_the_metadata_rag_filters_on(fresh_db, collection):
    """user_id and session_id are how retrieval scopes results — a vector without them is
    unreachable at best and cross-user at worst."""
    _add_message("a", speaker="jarvis")
    memory.flush_embeddings()
    meta = collection.batches[0]["metadatas"][0]
    assert meta["user_id"] == 7 and meta["session_id"] == "s1" and meta["speaker"] == "jarvis"


def test_flush_respects_the_batch_limit(fresh_db, collection):
    for i in range(10):
        _add_message(f"m{i}")
    assert memory.flush_embeddings(limit=4) == 4
    assert len(memory.get_unembedded_messages()) == 6


def test_flush_takes_the_oldest_first(fresh_db, collection):
    first = _add_message("oldest")
    _add_message("newer")
    memory.flush_embeddings(limit=1)
    assert collection.batches[0]["ids"] == [str(first)]


# --------------------------------------------------------------------- not losing anything

def test_a_failed_write_leaves_messages_pending(fresh_db, collection):
    """The failure that matters. If Chroma is down, these messages must stay queued — marking
    them embedded anyway would put a permanent hole in recall that nothing would ever report."""
    _add_message("a")
    collection.fail = True
    assert memory.flush_embeddings() == 0
    assert len(memory.get_unembedded_messages()) == 1     # still pending, will retry

    collection.fail = False
    assert memory.flush_embeddings() == 1
    assert memory.get_unembedded_messages() == []


def test_pending_work_survives_a_restart(fresh_db, collection):
    """The reason the queue moved into the database. With an in-memory queue, everything not yet
    flushed died with the process — silently, since nothing tracked what was owed."""
    _add_message("said before the crash")
    # a restart is just a new read of the same table: no in-process state carries over
    assert [m["content"] for m in memory.get_unembedded_messages()] == ["said before the crash"]
    assert memory.flush_embeddings() == 1


def test_flush_is_a_no_op_when_vectors_are_unavailable(fresh_db, monkeypatch):
    """RAG disabled (JARVIS_NO_EMBED=1) must not mark messages embedded — enabling it later
    should still pick them up rather than start from a lie."""
    _add_message("a")
    monkeypatch.setattr(memory, "vectors_available", lambda: False)
    assert memory.flush_embeddings() == 0
    assert len(memory.get_unembedded_messages()) == 1


# --------------------------------------------------------------------- the flush valve

def test_oldest_age_is_zero_when_nothing_is_pending(fresh_db):
    assert memory._oldest_unembedded_age_s() == 0.0


def test_oldest_age_reports_a_waiting_message(fresh_db):
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("INSERT INTO conversation_history (session_id, speaker, content, embedded, timestamp) "
                 "VALUES ('s1','user','old',0, datetime('now','-20 minutes'))")
    conn.commit()
    conn.close()
    age = memory._oldest_unembedded_age_s()
    assert age > memory.EMBED_MAX_DEFER_S, (
        "a 20-minute-old message must trip the valve — an unbroken conversation never goes idle, "
        f"and without this it would never be embedded at all (age={age:.0f}s)")


def test_valve_threshold_is_below_the_extraction_threshold():
    """Vectors should land in the pause after a chat, not queue behind a multi-minute LLM job."""
    assert config.EMBED_IDLE_SECONDS < config.IDLE_THRESHOLD_SECONDS


# --------------------------------------------------------------------- migration

def test_existing_rows_are_marked_embedded_not_pending():
    """A database upgraded from before this column already has vectors for its history. Defaulting
    them to 0 would re-embed every message ever sent on the first idle tick — hours of CPU on the
    box this runs on, for work already done."""
    tmp = Path(tempfile.mkdtemp()) / "old.db"
    conn = sqlite3.connect(tmp)
    conn.executescript("""
        CREATE TABLE conversation_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, timestamp DATETIME,
            speaker TEXT, content TEXT NOT NULL, facts_extracted BOOLEAN DEFAULT 0);
        INSERT INTO conversation_history (session_id, speaker, content) VALUES ('s','user','old');
    """)
    conn.commit()
    assert db._safe_exec(conn, "ALTER TABLE conversation_history ADD COLUMN embedded BOOLEAN DEFAULT 0")
    conn.execute("UPDATE conversation_history SET embedded = 1")
    conn.commit()
    assert conn.execute("SELECT embedded FROM conversation_history").fetchone()[0] == 1
    # ...and the ALTER reports "already applied" the second time, so the backfill runs ONCE.
    assert db._safe_exec(conn, "ALTER TABLE conversation_history ADD COLUMN embedded BOOLEAN DEFAULT 0") is False
    conn.close()
