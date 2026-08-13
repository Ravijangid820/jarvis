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
import threading
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

import chat  # noqa: E402
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


def test_storing_a_message_makes_it_pending(fresh_db, collection):
    """The join between the two halves. chat.store_message writes the row; the row's embedded=0 IS
    the queue (enqueue_embedding does nothing any more). Nothing else puts work into the pending
    set, so if this link breaks, embedding silently stops for the whole system."""
    chat.store_message("s1", "user", "remember the boiler code is 4417")
    pending = memory.get_unembedded_messages()
    assert [p["content"] for p in pending] == ["remember the boiler code is 4417"]
    # ...carrying the metadata the flush needs to scope the vector to its owner.
    assert pending[0]["user_id"] == 7 and pending[0]["session_id"] == "s1"


def test_storing_a_message_does_not_embed_it_inline(fresh_db, collection):
    """The measured reason the queue exists: ~1.2 s of a two-core box, spent while the user is
    typing their next message."""
    chat.store_message("s1", "user", "hello")
    assert collection.batches == []


def test_a_stored_message_is_picked_up_by_the_next_flush(fresh_db, collection):
    chat.store_message("s1", "user", "hello")
    chat.store_message("s1", "jarvis", "Sir.")
    assert memory.flush_embeddings() == 2
    assert memory.get_unembedded_messages() == []


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


# --------------------------------------------------------------------- the worker's trigger
#
# The condition the whole feature hangs on lives in _memory_worker, inside a `while` around a
# sleep — so it had no coverage at all, and an inverted comparison would have embedded nothing
# while every test above still passed. These drive ONE pass of the real loop.


class _Clock:
    """Stands in for the `time` module inside memory, so one worker pass runs without sleeping."""
    def __init__(self, now=1_000_000.0):
        self.now = now
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        memory._memory_worker_running = False    # this iteration finishes, then the loop exits


def _one_worker_pass(monkeypatch, *, idle_for, oldest_age, extract=None, flushed=1):
    """Run a single iteration of _memory_worker and report what it decided to do.

    The recorders below ignore calls from any other thread. Other test modules boot the real app
    through TestClient, whose lifespan starts a genuine memory-core thread — and it reads the same
    module globals these tests patch. Without the check, a background tick landing mid-test would
    show up as an extra flush here and fail an unrelated assertion once in a long while.
    """
    clock = _Clock()
    here = threading.current_thread()
    did = {"flush": 0, "extract": 0}
    monkeypatch.setattr(memory, "time", clock)
    monkeypatch.setattr(memory, "_last_activity_time", clock.now - idle_for)
    monkeypatch.setattr(memory, "is_busy", lambda: False)
    monkeypatch.setattr(memory, "_oldest_unembedded_age_s", lambda: oldest_age)

    def _flush(*a, **k):
        if threading.current_thread() is not here:
            return 0
        did["flush"] += 1
        return flushed                 # how many messages were pending

    def _extract(messages):
        if threading.current_thread() is not here:
            return
        did["extract"] += 1

    monkeypatch.setattr(memory, "flush_embeddings", _flush)
    monkeypatch.setattr(memory, "get_unprocessed_messages",
                        lambda batch_size=None: extract or [])
    monkeypatch.setattr(memory, "extract_facts_batch", _extract)
    memory._memory_worker()
    return did


def test_the_worker_flushes_once_the_box_goes_idle(monkeypatch):
    did = _one_worker_pass(monkeypatch, idle_for=memory.EMBED_IDLE_SECONDS + 1, oldest_age=0)
    assert did["flush"] == 1


def test_the_worker_does_not_flush_while_someone_is_typing(monkeypatch):
    """Embedding mid-conversation is exactly what this change removed: it competes with prefill
    for the same two cores, right when the user is waiting on a reply."""
    did = _one_worker_pass(monkeypatch, idle_for=memory.EMBED_IDLE_SECONDS - 1, oldest_age=0)
    assert did["flush"] == 0


def test_an_old_pending_message_trips_the_valve_even_mid_conversation(monkeypatch):
    """The second half of the condition, and the one that is easy to lose. An unbroken
    conversation never reaches the idle threshold, so without the age check "defer to idle" would
    mean "never embed"."""
    did = _one_worker_pass(monkeypatch, idle_for=0, oldest_age=memory.EMBED_MAX_DEFER_S + 1)
    assert did["flush"] == 1


def test_a_young_pending_message_does_not_trip_the_valve(monkeypatch):
    did = _one_worker_pass(monkeypatch, idle_for=0, oldest_age=memory.EMBED_MAX_DEFER_S - 1)
    assert did["flush"] == 0


def test_a_flush_defers_fact_extraction_to_the_next_pass(monkeypatch):
    """Both jobs want the same two cores. Having embedded something, the worker re-checks activity
    rather than starting a multi-minute LLM call on top of it."""
    did = _one_worker_pass(monkeypatch, idle_for=memory.IDLE_THRESHOLD_SECONDS + 1, oldest_age=0,
                           extract=[{"id": 1, "content": "x", "user_id": 7}])
    assert did == {"flush": 1, "extract": 0}


def test_extraction_still_runs_when_there_is_nothing_to_embed(monkeypatch):
    """...and the deferral must not become a block: with the queue empty, flush returns 0 and the
    idle pass goes on to the job it was there for."""
    did = _one_worker_pass(monkeypatch, idle_for=memory.IDLE_THRESHOLD_SECONDS + 1, oldest_age=0,
                           extract=[{"id": 1, "content": "x", "user_id": 7}], flushed=0)
    assert did == {"flush": 1, "extract": 1}


# --------------------------------------------------------------------- migration

def _old_schema_db(tmp_path) -> Path:
    """A database as it looked before the `embedded` column existed, with one message in it."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE conversation_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            speaker TEXT, content TEXT NOT NULL, facts_extracted BOOLEAN DEFAULT 0);
        INSERT INTO conversation_history (session_id, speaker, content)
            VALUES ('s','user','said before the upgrade');
    """)
    conn.commit()
    conn.close()
    return path


def _embedded_flag(path: Path, content: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT embedded FROM conversation_history WHERE content = ?", (content,)).fetchone()[0]
    finally:
        conn.close()


def test_upgrading_an_old_database_marks_existing_rows_embedded(monkeypatch, tmp_path):
    """A database upgraded from before this column already has vectors for its history. Defaulting
    them to 0 would re-embed every message ever sent on the first idle tick — hours of CPU on the
    box this runs on, for work already done.

    This drives db.init_db() itself against a genuinely pre-column database, because the backfill
    is one line inside it and an earlier version of this test re-implemented that line instead of
    calling it: deleting the line from db.py left the test green.
    """
    path = _old_schema_db(tmp_path)
    monkeypatch.setattr(db, "DB_PATH", str(path))
    db.init_db()                                   # exactly what startup runs
    assert _embedded_flag(path, "said before the upgrade") == 1


def test_the_backfill_does_not_run_a_second_time(monkeypatch, tmp_path):
    """The backfill is guarded by whether the ALTER actually ran. Unguarded, every restart would
    mark the whole pending queue embedded and throw away the vectors it still owed."""
    path = _old_schema_db(tmp_path)
    monkeypatch.setattr(db, "DB_PATH", str(path))
    db.init_db()

    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO conversation_history (session_id, speaker, content) "
                 "VALUES ('s','user','waiting for a vector')")
    conn.commit()
    conn.close()
    assert _embedded_flag(path, "waiting for a vector") == 0     # new rows start pending

    db.init_db()                                   # a restart
    assert _embedded_flag(path, "waiting for a vector") == 0, \
        "a second start re-ran the backfill and silently dropped a pending message's vector"
