-- SQLite schema for Jarvis memory.
-- This file is the single source of truth: every column the app uses is declared
-- here (the orchestrator also runs idempotent ALTERs in init_db() as a safety net
-- for already-deployed databases). Long-term recall is handled by ChromaDB vectors,
-- so the old FTS5 search tables / triggers and the unused semantic_facts table were
-- removed — they fired on every insert/delete but were never queried.

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT DEFAULT 'user',
    can_control_devices INTEGER DEFAULT 0,   -- may trigger device actions (lights/volume); admins always may
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    user_id INTEGER DEFAULT 1 REFERENCES users(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS conversation_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT DEFAULT 'default',
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    speaker TEXT CHECK(speaker IN ('user', 'jarvis')),
    content TEXT NOT NULL,
    facts_extracted BOOLEAN DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_history_session ON conversation_history(session_id, id);
CREATE INDEX IF NOT EXISTS idx_history_unextracted ON conversation_history(facts_extracted, speaker);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at DATETIME NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS api_keys (
    key_string TEXT PRIMARY KEY,          -- SHA-256 hash of the key (never the plaintext)
    key_prefix TEXT,                      -- short prefix shown in the admin UI
    user_id INTEGER NOT NULL,
    description TEXT,
    device_id TEXT,                       -- if set, key is bound to this device (pull/events scoped to it)
    usage_count INTEGER DEFAULT 0,
    last_used_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Persistent User Knowledge Base ("Jarvis Memory Core")
-- Stores personal facts as full self-contained sentences.
-- Survives chat deletion. No per-user limit.
CREATE TABLE IF NOT EXISTS user_knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category TEXT NOT NULL DEFAULT 'other',
    content TEXT NOT NULL,
    source TEXT DEFAULT 'auto',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_knowledge_user ON user_knowledge(user_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_category ON user_knowledge(user_id, category);

-- Vision/edge events posted by edge devices (Raspberry Pi camera agent). `data` is JSON.
CREATE TABLE IF NOT EXISTS vision_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    type TEXT NOT NULL,
    data TEXT,
    user_id INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_vision_events_recent ON vision_events(id DESC);

-- Outbound command queue for device agents (e.g. the Windows volume agent). Agents PULL their
-- pending commands (no inbound port on the device); the orchestrator only ever enqueues.
CREATE TABLE IF NOT EXISTS device_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    action TEXT NOT NULL,
    params TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    delivered_at DATETIME
);
CREATE INDEX IF NOT EXISTS idx_device_commands_pending ON device_commands(device_id, status, id);
