"""SQLite access: connection factory + schema initialization."""
import os
import sqlite3
from pathlib import Path

from auth import hash_token
from config import DB_PATH, SCHEMA_PATH


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Wait up to 30s for a competing writer instead of failing with "database is
    # locked": three writer sources (request threads + embedding + memory workers)
    # can overlap, and 5s was occasionally too short under load.
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _safe_exec(conn: sqlite3.Connection, sql: str):
    """Run a best-effort migration statement (e.g. ALTER that may already be applied).

    Only swallow the "already applied" cases (duplicate column / already-exists); re-raise
    anything else — including "no such table/column" — so a genuinely broken migration is not
    silently masked. (DROP ... IF EXISTS never raises on a missing object, so it needs no
    swallow here.)
    """
    try:
        conn.execute(sql)
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "duplicate column" in msg or "already exists" in msg:
            return  # already in the expected state — benign
        raise


def _migrate_plaintext_api_keys(conn: sqlite3.Connection):
    """One-time: hash any API keys still stored in plaintext, in place.

    Holders keep their existing keys (they present the plaintext; we hash and match),
    but the value at rest becomes a SHA-256 hash. A stored hash is 64 hex chars; any
    row whose key_string isn't already that shape is treated as a legacy plaintext key.
    """
    try:
        rows = conn.execute("SELECT rowid, key_string, key_prefix FROM api_keys").fetchall()
    except sqlite3.OperationalError:
        return
    hexset = set("0123456789abcdef")
    for r in rows:
        ks = r["key_string"] or ""
        already_hashed = len(ks) == 64 and all(c in hexset for c in ks.lower())
        if already_hashed:
            continue
        conn.execute("UPDATE api_keys SET key_string = ?, key_prefix = ? WHERE rowid = ?",
                     (hash_token(ks), r["key_prefix"] or ks[:10], r["rowid"]))


def init_db():
    if not SCHEMA_PATH.exists():
        # Fail loudly — a silent no-op leaves every query failing with "no such table".
        raise RuntimeError(f"schema.sql not found at {SCHEMA_PATH}; cannot initialize the database")
    # The data dir is gitignored, so it won't exist on a fresh checkout — create it.
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        with open(SCHEMA_PATH, "r") as f:
            conn.executescript(f.read())
        # Safety-net migrations for databases created before these columns existed.
        _safe_exec(conn, "ALTER TABLE chat_sessions ADD COLUMN user_id INTEGER DEFAULT 1 REFERENCES users(id)")
        _safe_exec(conn, "ALTER TABLE api_keys ADD COLUMN usage_count INTEGER DEFAULT 0")
        _safe_exec(conn, "ALTER TABLE api_keys ADD COLUMN last_used_at DATETIME")
        _safe_exec(conn, "ALTER TABLE api_keys ADD COLUMN key_prefix TEXT")
        _migrate_plaintext_api_keys(conn)
        _safe_exec(conn, "ALTER TABLE conversation_history ADD COLUMN facts_extracted BOOLEAN DEFAULT 0")
        _safe_exec(conn, "ALTER TABLE users ADD COLUMN can_control_devices INTEGER DEFAULT 0")
        _safe_exec(conn, "ALTER TABLE api_keys ADD COLUMN device_id TEXT")
        _safe_exec(conn, "ALTER TABLE enroll_requests ADD COLUMN user_id INTEGER REFERENCES users(id)")
        # Drop the legacy FTS5 search infra + unused table (superseded by ChromaDB vectors).
        for stmt in (
            "DROP TRIGGER IF EXISTS conversation_ai",
            "DROP TRIGGER IF EXISTS conversation_ad",
            "DROP TRIGGER IF EXISTS conversation_au",
            "DROP TABLE IF EXISTS conversation_fts",
            "DROP TABLE IF EXISTS semantic_facts",
        ):
            _safe_exec(conn, stmt)
        conn.commit()
    finally:
        conn.close()
    # The DB holds password hashes, hashed tokens, chat history and knowledge — keep it
    # owner-only (defence-in-depth; pair with UMask=0077 in the systemd unit). Best-effort:
    # also tighten the WAL/SHM siblings, which carry recently-written data.
    for p in (DB_PATH, f"{DB_PATH}-wal", f"{DB_PATH}-shm"):
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
