"""SQLite access: connection factory + schema initialization."""
import logging
import os
import sqlite3
from pathlib import Path

from auth import hash_password, hash_token
from config import DB_PATH, SCHEMA_PATH

logger = logging.getLogger("jarvis")


PRIMARY_HOUSEHOLD_ID = 1     # "Home" — owns everything that predates multi-tenancy


def _seed_primary_household(conn: sqlite3.Connection):
    """Ensure household 1 exists. It is the tenant every pre-multi-tenancy row is backfilled into,
    so a single-household deployment sees no behaviour change at all."""
    conn.execute(
        "INSERT INTO households (id, name, is_demo) VALUES (?, 'Home', 0) "
        "ON CONFLICT(id) DO NOTHING", (PRIMARY_HOUSEHOLD_ID,))


def _seed_initial_admin(conn: sqlite3.Connection):
    """Industry-standard bootstrapping: automatically seed an initial admin account if users table is empty."""
    try:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()
        if row and row["cnt"] == 0:
            admin_user = os.environ.get("ADMIN_USER", "admin")
            admin_pass = os.environ.get("ADMIN_PASS")
            if not admin_pass:
                admin_pass = "admin"
                logger.warning(
                    "No ADMIN_PASS env var set! Seeding default initial admin account ('%s' / '%s'). "
                    "Set ADMIN_PASS in your environment or change password via /admin UI for security!",
                    admin_user, admin_pass
                )
            else:
                logger.info("Seeding initial admin account ('%s') from environment variables.", admin_user)
            conn.execute(
                "INSERT INTO users (username, password_hash, role, can_control_devices, household_id) "
                "VALUES (?, ?, 'admin', 1, ?)",
                (admin_user, hash_password(admin_pass), PRIMARY_HOUSEHOLD_ID)
            )
    except Exception as e:
        logger.warning("Failed to check/seed initial admin account: %s", e)


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


def get_setting(key: str, default=None):
    """Read an admin-editable runtime setting from app_settings (see schema.sql)."""
    conn = get_db()
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row is not None else default
    finally:
        conn.close()


def set_setting(key: str, value: str) -> None:
    """Upsert an admin-editable runtime setting."""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
            (key, value))
        conn.commit()
    finally:
        conn.close()


def get_household_setting(household_id: int, key: str, default=None):
    """Read one household-scoped runtime setting (see household_settings in schema.sql).

    Separate from get_setting/app_settings on purpose: the values here (the Home Assistant URL and
    long-lived token) belong to ONE household, and a global lookup would hand them to any admin on
    the box.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT value FROM household_settings WHERE household_id = ? AND key = ?",
            (household_id, key)).fetchone()
        return row["value"] if row is not None else default
    finally:
        conn.close()


def set_household_setting(household_id: int, key: str, value: str) -> None:
    """Upsert a household-scoped runtime setting."""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO household_settings (household_id, key, value, updated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(household_id, key) DO UPDATE SET value = excluded.value, "
            "updated_at = CURRENT_TIMESTAMP",
            (household_id, key, value))
        conn.commit()
    finally:
        conn.close()


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


def _migrate_persons_unique(conn: sqlite3.Connection):
    """Rebuild `persons` if it still carries the global `name TEXT NOT NULL UNIQUE`.

    Uniqueness moved from global to per-household (two households may each know an "Alice", and a
    demo visitor enrolling their own name must not collide with — or thereby detect — a real one).
    SQLite cannot drop a column constraint with ALTER, so the table is rebuilt. face_embeddings
    references persons(id), so ids are preserved and the FK survives; the rebuild runs inside the
    caller's transaction with foreign_keys deferred by the pragma flip below.
    """
    sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='persons'").fetchone()
    if not sql_row or "UNIQUE" not in (sql_row["sql"] or "").upper():
        return   # already rebuilt (or fresh DB created from the current schema)
    logger.info("Migrating persons: name uniqueness is now per-household; rebuilding table")
    conn.executescript(
        """
        CREATE TABLE persons_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            household_id INTEGER REFERENCES households(id),
            name TEXT NOT NULL,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO persons_new (id, household_id, name, user_id, created_at)
            SELECT id, 1, name, user_id, created_at FROM persons;
        DROP TABLE persons;
        ALTER TABLE persons_new RENAME TO persons;
        """
    )


# Tables that gained household_id, and the column each one is backfilled through. A row whose
# owner can't be determined falls back to the primary household — the pre-multi-tenancy default,
# which is correct because every such row predates the feature.
_HOUSEHOLD_BACKFILL = (
    "global_knowledge", "persons", "vision_events", "enroll_requests",
    "device_commands", "device_heartbeats", "audit_log",
)


# Indexes spanning household_id. These live here rather than in schema.sql because schema.sql is
# applied with executescript() BEFORE the ALTERs below run — so on an upgraded database the column
# they index does not exist yet and the whole script would abort. Created once the column is
# guaranteed present. (The households table's own index stays in schema.sql: it's a new table, so
# its columns always exist.)
_HOUSEHOLD_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_users_household ON users(household_id)",
    "CREATE INDEX IF NOT EXISTS idx_global_knowledge_household ON global_knowledge(household_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_household ON audit_log(household_id, id DESC)",
    # Presence ("who is home right now") is derived from vision_events and injected into prompts,
    # so the household filter has to be cheap on the recent-events path.
    "CREATE INDEX IF NOT EXISTS idx_vision_events_household ON vision_events(household_id, id DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_persons_household_name ON persons(household_id, name)",
)


def _migrate_households(conn: sqlite3.Connection):
    """Add household_id everywhere it's missing and backfill existing rows into household 1."""
    _safe_exec(conn, "ALTER TABLE users ADD COLUMN household_id INTEGER REFERENCES households(id)")
    for table in _HOUSEHOLD_BACKFILL:
        _safe_exec(conn, f"ALTER TABLE {table} ADD COLUMN household_id INTEGER REFERENCES households(id)")
    _migrate_persons_unique(conn)
    # Backfill. Users first: rows below are attributed through their owning user where possible,
    # so the users table has to be correct before anything reads it.
    conn.execute("UPDATE users SET household_id = ? WHERE household_id IS NULL", (PRIMARY_HOUSEHOLD_ID,))
    for table in _HOUSEHOLD_BACKFILL:
        conn.execute(f"UPDATE {table} SET household_id = ? WHERE household_id IS NULL",
                     (PRIMARY_HOUSEHOLD_ID,))
    # Only now is every indexed column guaranteed to exist, and every row non-NULL — so the
    # unique index over (household_id, name) can't trip over a table full of NULL households.
    for stmt in _HOUSEHOLD_INDEXES:
        _safe_exec(conn, stmt)


def _migrate_ha_settings_to_household(conn: sqlite3.Connection):
    """Move the Home Assistant connection out of the instance-global app_settings into the
    primary household's settings.

    A smart home belongs to the household that owns it — leaving the token in a global table
    would mean any admin of any household (including a demo visitor) could reach it. Copied, not
    moved-and-deleted, on the first pass: if a rollback to an older build is needed the old keys
    still work. They are ignored by the new read path.
    """
    rows = conn.execute(
        "SELECT key, value FROM app_settings WHERE key IN ('ha_url', 'ha_token', 'ha_allowed_entities')"
    ).fetchall()
    for r in rows:
        conn.execute(
            "INSERT INTO household_settings (household_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(household_id, key) DO NOTHING",
            (PRIMARY_HOUSEHOLD_ID, r["key"], r["value"]))


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
        # Multi-tenancy: households + the household_id backfill. Must run before anything reads a
        # scoped table. The persons rebuild inside it drops and recreates the table, which needs
        # foreign_keys OFF (get_db turns it on) — SQLite only honours the flip outside a txn.
        _seed_primary_household(conn)
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            _migrate_households(conn)
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
        _migrate_ha_settings_to_household(conn)
        # Drop the legacy FTS5 search infra + unused table (superseded by ChromaDB vectors).
        for stmt in (
            "DROP TRIGGER IF EXISTS conversation_ai",
            "DROP TRIGGER IF EXISTS conversation_ad",
            "DROP TRIGGER IF EXISTS conversation_au",
            "DROP TABLE IF EXISTS conversation_fts",
            "DROP TABLE IF EXISTS semantic_facts",
        ):
            _safe_exec(conn, stmt)
        _seed_initial_admin(conn)
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
