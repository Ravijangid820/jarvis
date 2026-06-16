"""SQLite access: connection factory + schema initialization."""
import sqlite3
from pathlib import Path

from config import DB_PATH


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Wait up to 5s for a competing writer instead of failing instantly with
    # "database is locked" (the background workers + request threads can overlap).
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _safe_exec(conn: sqlite3.Connection, sql: str):
    """Run a best-effort migration statement (e.g. ALTER that may already be applied)."""
    try:
        conn.execute(sql)
    except sqlite3.OperationalError:
        pass  # column/table already in the expected state


def init_db():
    schema_path = Path("/srv/jarvis/config/schema.sql")
    if not schema_path.exists():
        return
    conn = get_db()
    try:
        with open(schema_path, "r") as f:
            conn.executescript(f.read())
        # Safety-net migrations for databases created before these columns existed.
        _safe_exec(conn, "ALTER TABLE chat_sessions ADD COLUMN user_id INTEGER DEFAULT 1 REFERENCES users(id)")
        _safe_exec(conn, "ALTER TABLE api_keys ADD COLUMN usage_count INTEGER DEFAULT 0")
        _safe_exec(conn, "ALTER TABLE api_keys ADD COLUMN last_used_at DATETIME")
        _safe_exec(conn, "ALTER TABLE conversation_history ADD COLUMN facts_extracted BOOLEAN DEFAULT 0")
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
