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
    key_string TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    description TEXT,
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
