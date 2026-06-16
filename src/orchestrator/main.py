"""
Jarvis AI Orchestrator
Secure FastAPI service coordinating LLM inference, memory, and voice pipeline.
All config loaded from /srv/jarvis/config/jarvis.json — no hardcoded secrets.
"""

import json
import logging
import sqlite3
import time
import threading
import urllib.request
import urllib.error
import uuid
import subprocess
import base64
import hashlib
import hmac
import secrets
from collections import defaultdict
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import chromadb

from logging.handlers import RotatingFileHandler

# Rotating file handler so the log can't grow without bound (5 MB x 3 backups).
# stdout also goes to journald via systemd; rely on journald's own rotation there.
_LOG_DIR = Path("/srv/jarvis/logs")
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(_LOG_DIR / "orchestrator.log", maxBytes=5_000_000, backupCount=3),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("jarvis")

CONFIG_PATH = Path("/srv/jarvis/config/jarvis.json")
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        logger.error("Config file not found at %s", CONFIG_PATH)
        raise SystemExit("FATAL: Config file missing. Cannot start without config.")
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)

CONFIG = load_config()

# Extract frequently used values
MASTER_API_KEY: str = CONFIG["api_key"] # Now used only as an emergency bypass
LLM_URL: str = CONFIG["llm"]["fast_brain_url"]
REQUEST_TIMEOUT: int = CONFIG["llm"]["request_timeout_seconds"]
TEMPERATURE: float = CONFIG["llm"]["default_temperature"]
MAX_INPUT_LENGTH: int = CONFIG["orchestrator"]["max_input_length"]
RATE_LIMIT_RPM: int = CONFIG["orchestrator"]["rate_limit_requests_per_minute"]
DB_PATH: str = CONFIG["memory"]["db_path"]
MAX_CONTEXT_MESSAGES: int = CONFIG["memory"]["max_context_messages"]
SYSTEM_PROMPT: str = CONFIG["system_prompt"]

# --- Prompt token budgeting -------------------------------------------------
# The llama-server is launched with a fixed context window (-c). The total of
# (prompt tokens + generated tokens) must fit inside it, or llama.cpp silently
# evicts the oldest prompt tokens (dropping the system prompt / current question).
# We never have a tokenizer here, so we budget with a conservative char-based
# estimate and clamp the requested completion length to whatever the window has
# left after the prompt is assembled. See build_messages() / _clamp_completion().
MAX_CONTEXT_TOKENS: int = CONFIG["llm"].get("max_context_tokens", 4096)
COMPLETION_RESERVE_DEFAULT: int = 512   # tokens reserved for the answer if caller gives none
PROMPT_SAFETY_MARGIN: int = 96          # slack for the char-based token estimate + chat template
KNOWLEDGE_TOKEN_CAP: int = 512          # max tokens the injected user-profile block may consume
MIN_COMPLETION_TOKENS: int = 64         # never squeeze the answer below this

# Pure token-budgeting helpers live in budget.py (unit-tested without the heavy app import).
from budget import (
    estimate_message_tokens as _estimate_message_tokens,
    truncate_to_tokens as _truncate_to_tokens,
    is_default_session,
    fit_history,
    clamp_completion,
)

REACT_DIST_DIR = Path("/srv/jarvis/frontend/dist")
STATIC_DIR = Path(__file__).parent / "static"
INDEX_HTML = REACT_DIST_DIR / "index.html"
ADMIN_HTML = STATIC_DIR / "admin.html"

_ALLOWED_STATIC_EXT = {".css", ".js", ".html", ".ico", ".svg", ".png", ".jpg", ".woff2"}

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup. (init_db / start_* are defined later in the module; they resolve at call time.)
    init_db()
    start_embedding_worker()
    start_memory_worker()
    logger.info("Jarvis Orchestrator started with Auth + Memory Core")
    yield
    # Shutdown: signal the embedding worker to drain and exit.
    _embed_queue.put(None)

app = FastAPI(title="Jarvis Orchestrator", docs_url=None, redoc_url=None, lifespan=lifespan)

# CORS: the SPA and admin panel are served same-origin, so cross-origin access is
# only needed if you call the API from another site. Restrict it via config; default
# to no cross-origin (most secure). Set orchestrator.allowed_origins in jarvis.json
# (e.g. ["*"] to allow all, or an explicit list) to widen it.
ALLOWED_ORIGINS: List[str] = CONFIG["orchestrator"].get("allowed_origins", [])
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "GET", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

_rate_store: Dict[str, List[float]] = defaultdict(list)
def check_rate_limit(client_ip: str) -> bool:
    now = time.time()
    window_start = now - 60.0
    _rate_store[client_ip] = [t for t in _rate_store[client_ip] if t > window_start]
    if len(_rate_store[client_ip]) >= RATE_LIMIT_RPM:
        return False
    _rate_store[client_ip].append(now)
    return True

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Wait up to 5s for a competing writer instead of failing instantly with
    # "database is locked" (the background workers + request threads can overlap).
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

# AUTHENTICATION HELPERS
def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        salt, key = password_hash.split(':')
        new_key = hashlib.pbkdf2_hmac('sha256', plain_password.encode('utf-8'), salt.encode('utf-8'), 100000)
        return secrets.compare_digest(key, new_key.hex())
    except Exception:
        return False

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000)
    return f"{salt}:{key.hex()}"

@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path
    if request.method == "OPTIONS" or path in ["/health", "/", "/admin"] or path.startswith("/static/") or path.startswith("/assets/"):
        response = await call_next(request)
        return _apply_security_headers(response)



    # Auth for /auth/login is special
    if path == "/auth/login":
        response = await call_next(request)
        return _apply_security_headers(response)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return Response(content=json.dumps({"error": "Auth required"}), status_code=401)

    token = auth_header[7:]

    # 1. Master Bypass (bootstrap/emergency) — accepted ONLY from the local host.
    # The voice listener (run_listener.sh) runs on the box and uses it over loopback;
    # remote callers (LAN/Tailscale) must use a login token or a per-user API key, so a
    # sniffed or leaked master key can't grant remote admin.
    if hmac.compare_digest(token.encode(), MASTER_API_KEY.encode()):
        if not _is_local_request(request):
            return Response(content=json.dumps({"error": "Master key is local-only"}), status_code=403)
        request.state.user_id = 1 # Force admin user ID
        request.state.is_admin = True
        return await call_next(request)

    conn = get_db()
    is_authenticated = False
    try:
        # 2. Check auth_sessions (Web UI Logins)
        cursor = conn.execute("SELECT user_id, u.role FROM auth_sessions s JOIN users u ON s.user_id = u.id WHERE s.token = ? AND s.expires_at > datetime('now')", (token,))
        row = cursor.fetchone()
        if row:
            request.state.user_id = row["user_id"]
            request.state.is_admin = (row["role"] == "admin")
            is_authenticated = True
        else:
            # 3. Check api_keys (Machine Integrations)
            cursor = conn.execute("SELECT user_id, u.role FROM api_keys k JOIN users u ON k.user_id = u.id WHERE k.key_string = ?", (token,))
            row = cursor.fetchone()
            if row:
                request.state.user_id = row["user_id"]
                request.state.is_admin = (row["role"] == "admin")
                is_authenticated = True
                try:
                    conn.execute("UPDATE api_keys SET usage_count = usage_count + 1, last_used_at = datetime('now') WHERE key_string = ?", (token,))
                    conn.commit()
                except Exception as e:
                    logger.warning("api_keys usage bump failed: %s", e)
    finally:
        conn.close()

    if not is_authenticated:
        return Response(content=json.dumps({"error": "Invalid or expired token"}), status_code=403)

    # Rate-limit ALL authenticated callers (admins included). A shared or compromised
    # admin/API key could otherwise flood the single ~3.7 tok/s LLM backend unbounded.
    # Key the limiter on the user id so it can't be defeated by IP changes / shared NAT.
    rl_key = f"user:{request.state.user_id}"
    if not check_rate_limit(rl_key):
        return Response(content=json.dumps({"error": "Rate limit exceeded"}), status_code=429)

    response = await call_next(request)
    return _apply_security_headers(response)

def _apply_security_headers(response: Response) -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store"
    return response

def _is_local_request(request: Request) -> bool:
    """True if the request originates from the loopback interface."""
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")

def _safe_exec(conn: sqlite3.Connection, sql: str):
    """Run a best-effort migration statement (e.g. ALTER that may already be applied)."""
    try:
        conn.execute(sql)
    except sqlite3.OperationalError:
        pass  # column/table already in the expected state

def init_db():
    schema_path = Path("/srv/jarvis/config/schema.sql")
    if not schema_path.exists(): return
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
        # These triggers fired on every insert/delete but were never queried.
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

# ================================================================
# EMBEDDINGS + VECTOR STORE
# embeddinggemma-300m is ASYMMETRIC: documents and queries must be encoded with
# different prompt templates (per the model card), or retrieval quality drops a lot.
# We therefore drive the model ourselves (instead of Chroma's generic embedding
# function, which can't tell a document from a query) and store explicit vectors.
# ================================================================
from sentence_transformers import SentenceTransformer

EMBED_MODEL_NAME = "google/embeddinggemma-300m"
EMBED_DOC_PREFIX = "title: none | text: "
EMBED_QUERY_PREFIX = "task: search result | query: "

CHROMA_DB_PATH = CONFIG["memory"].get("chroma_db_path", "/srv/jarvis/memory/chroma_db")
chroma_client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
try:
    _embed_model = SentenceTransformer(EMBED_MODEL_NAME)
    # Cosine space with normalized vectors. A fresh collection name is used because the
    # previous "jarvis_memory" collection was created with the default L2 space, which
    # cannot be converted in place. The deploy step re-embeds existing messages into this.
    memory_collection = chroma_client.get_or_create_collection(
        name="jarvis_memory_cos", metadata={"hnsw:space": "cosine"}
    )
except Exception as e:
    logger.error("Failed to initialize ChromaDB / embedding model: %s", e)
    _embed_model = None
    memory_collection = None

def _embed_documents(texts: List[str]) -> List[List[float]]:
    vecs = _embed_model.encode([EMBED_DOC_PREFIX + t for t in texts], normalize_embeddings=True)
    return [v.tolist() for v in vecs]

def _embed_query(text: str) -> List[List[float]]:
    vec = _embed_model.encode([EMBED_QUERY_PREFIX + text], normalize_embeddings=True)
    return [vec[0].tolist()]

# RAG config. With cosine distance = 1 - similarity (range 0..2): keep close matches only.
RAG_DISTANCE_THRESHOLD = 0.6  # discard results with cosine distance > this (similarity < ~0.4)
RAG_MAX_RESULTS = 5

# Background embedding: embedding a 300M model on a no-AVX2 CPU is hundreds of ms, so it
# must NOT run inline in the chat request path. store_message() enqueues; this worker drains.
import queue
_embed_queue: "queue.Queue" = queue.Queue()

def _embedding_worker():
    while True:
        item = _embed_queue.get()
        try:
            if item is None:
                return
            msg_id, content, metadata = item
            if memory_collection is not None and _embed_model is not None:
                memory_collection.add(
                    documents=[content], embeddings=_embed_documents([content]),
                    metadatas=[metadata], ids=[str(msg_id)],
                )
        except Exception as e:
            logger.error("Embedding worker error: %s", e)
        finally:
            _embed_queue.task_done()

def start_embedding_worker():
    t = threading.Thread(target=_embedding_worker, daemon=True, name="embedding-worker")
    t.start()
    return t

# ================================================================
# JARVIS MEMORY CORE — Persistent User Knowledge Base
# Extracts personal facts during idle time, stores permanently.
# ================================================================
IDLE_THRESHOLD_SECONDS = 120   # Extract facts after 2 min of inactivity
IDLE_CHECK_INTERVAL = 30       # Check for idle every 30 seconds
_last_activity_time = time.time()
_memory_worker_running = False

# In-flight chat requests. The fact-extraction worker shares the single LLM slot and 2
# CPU cores, so it must NOT run while a (possibly multi-minute) generation is active —
# tracking idle time alone isn't enough because one long stream can exceed the threshold.
_inflight_lock = threading.Lock()
_inflight_requests = 0

class _Inflight:
    """Context manager marking a chat request as active for the whole call."""
    def __enter__(self):
        global _inflight_requests
        with _inflight_lock:
            _inflight_requests += 1
        return self
    def __exit__(self, *exc):
        global _inflight_requests
        with _inflight_lock:
            _inflight_requests -= 1
        _update_activity()  # reset idle clock when the request truly finishes
        return False

def _is_busy() -> bool:
    with _inflight_lock:
        return _inflight_requests > 0

FACT_EXTRACTION_PROMPT = """Analyze this conversation and extract any personal facts about the user.
Return a JSON array. Each fact must be a complete, self-contained sentence that would make sense on its own.

Categories: personal, family, preferences, location, work, education, interests, technical, other

Rules:
- Only extract FACTS the user explicitly stated about themselves. Do NOT infer or guess.
- Each fact must be a full sentence with context (e.g. "The user's name is Ravi" not just "Ravi").
- Include details, nicknames, relationships mentioned.
- If the user corrects previous info, extract the CORRECTED version.
- Skip greetings, questions, or generic statements.
- If no personal facts found, return exactly: []

Examples of good extractions:
[{"category": "personal", "content": "The user's name is Ravi, also called Ravi bhai by friends"},
 {"category": "location", "content": "The user currently lives in Pune, Maharashtra"},
 {"category": "family", "content": "The user has a younger sister named Priya who is studying medicine"},
 {"category": "preferences", "content": "The user's favourite car is the Tesla Model 3"},
 {"category": "work", "content": "The user works as a backend developer at Infosys"},
 {"category": "technical", "content": "The user prefers Python and FastAPI for building APIs"}]

Return ONLY the JSON array, nothing else."""

def _update_activity():
    """Called on every user interaction to track idle time."""
    global _last_activity_time
    _last_activity_time = time.time()

# --- Knowledge CRUD ---
def get_user_knowledge(user_id: int) -> str:
    """Fetch all stored facts for this user, formatted for system prompt injection."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT category, content FROM user_knowledge WHERE user_id = ? ORDER BY category, updated_at DESC",
            (user_id,)
        ).fetchall()
        if not rows:
            return ""
        # Group by category
        by_cat = {}
        for r in rows:
            by_cat.setdefault(r["category"].upper(), []).append(r["content"])
        lines = []
        for cat, facts in by_cat.items():
            lines.append(f"[{cat}]")
            for f in facts:
                lines.append(f"  - {f}")
        return "\n".join(lines)
    finally:
        conn.close()

def get_user_knowledge_list(user_id: int) -> List[Dict[str, Any]]:
    """Get all facts as a list (for API responses)."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, category, content, source, created_at, updated_at FROM user_knowledge WHERE user_id = ? ORDER BY category, updated_at DESC",
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

# Fact dedup thresholds. Semantic similarity is preferred; the word-overlap path is only
# a fallback when the embedding model failed to load. The old word-overlap-at-0.6 merged
# DIFFERENT facts that share boilerplate ("The user lives in Pune" vs "...in Delhi" ≈ 0.8).
FACT_DEDUP_SIM = 0.90
FACT_DEDUP_WORD = 0.85

def _find_duplicate_fact(content: str, existing_rows: List[Any]) -> Optional[int]:
    """Return the id of an existing fact that's a restatement of `content`, else None."""
    if not existing_rows:
        return None
    if _embed_model is not None:
        # One batched embedding call: [new, *existing]; vectors are normalized so dot = cosine.
        vecs = _embed_documents([content] + [r["content"] for r in existing_rows])
        new_vec = vecs[0]
        best_id, best_sim = None, 0.0
        for r, v in zip(existing_rows, vecs[1:]):
            sim = float(sum(x * y for x, y in zip(new_vec, v)))
            if sim > best_sim:
                best_id, best_sim = r["id"], sim
        return best_id if best_sim >= FACT_DEDUP_SIM else None
    # Fallback: word-overlap (embeddings unavailable)
    new_words = set(content.lower().split())
    for r in existing_rows:
        old_words = set(r["content"].lower().split())
        if new_words and old_words:
            overlap = len(new_words & old_words) / max(len(new_words), len(old_words))
            if overlap >= FACT_DEDUP_WORD:
                return r["id"]
    return None

def store_fact(user_id: int, category: str, content: str, source: str = "auto") -> int:
    """Store a fact, updating an existing one if this is a semantic restatement of it."""
    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT id, content FROM user_knowledge WHERE user_id = ? AND category = ?",
            (user_id, category)
        ).fetchall()

        dup_id = _find_duplicate_fact(content, existing)
        if dup_id is not None:
            conn.execute(
                "UPDATE user_knowledge SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (content, dup_id)
            )
            conn.commit()
            logger.info("Memory Core: Updated fact #%d in [%s]", dup_id, category)
            return dup_id

        cursor = conn.execute(
            "INSERT INTO user_knowledge (user_id, category, content, source) VALUES (?, ?, ?, ?)",
            (user_id, category, content, source)
        )
        conn.commit()
        fact_id = cursor.lastrowid
        logger.info("Memory Core: Stored new fact #%d in [%s]: %s", fact_id, category, content[:80])
        return fact_id
    finally:
        conn.close()

def delete_fact(fact_id: int, user_id: int) -> bool:
    conn = get_db()
    try:
        conn.execute("DELETE FROM user_knowledge WHERE id = ? AND user_id = ?", (fact_id, user_id))
        conn.commit()
        return True
    finally:
        conn.close()

def update_fact(fact_id: int, user_id: int, content: str, category: str = None) -> bool:
    conn = get_db()
    try:
        if category:
            conn.execute(
                "UPDATE user_knowledge SET content = ?, category = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
                (content, category, fact_id, user_id)
            )
        else:
            conn.execute(
                "UPDATE user_knowledge SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
                (content, fact_id, user_id)
            )
        conn.commit()
        return True
    finally:
        conn.close()

# --- Idle-Time Fact Extraction ---
def _get_unprocessed_messages(batch_size: int = 20) -> List[Dict]:
    """Get messages that haven't been processed for fact extraction yet."""
    conn = get_db()
    try:
        # INNER JOIN: only extract from messages that belong to a real, owned session,
        # so cs.user_id is always present (no NULL -> default-to-user-1 misattribution).
        rows = conn.execute("""
            SELECT ch.id, ch.session_id, ch.speaker, ch.content, cs.user_id
            FROM conversation_history ch
            JOIN chat_sessions cs ON ch.session_id = cs.id
            WHERE ch.facts_extracted = 0 AND ch.speaker = 'user'
            ORDER BY ch.id ASC
            LIMIT ?
        """, (batch_size,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        # Column might not exist yet
        return []
    finally:
        conn.close()

def _mark_messages_processed(msg_ids: List[int]):
    """Mark exactly the given user messages as fact-extracted.

    Previously this also bulk-marked every other unprocessed row in the same
    sessions, which silently skipped fact extraction for later user messages.
    We only ever select user messages for extraction, so marking just these ids
    is both correct and sufficient.
    """
    if not msg_ids:
        return
    conn = get_db()
    try:
        placeholders = ",".join("?" for _ in msg_ids)
        conn.execute(f"UPDATE conversation_history SET facts_extracted = 1 WHERE id IN ({placeholders})", msg_ids)
        conn.commit()
    except Exception as e:
        logger.error("Memory Core: failed to mark messages processed: %s", e)
    finally:
        conn.close()

def _extract_facts_batch(messages: List[Dict]):
    """Process a batch of unprocessed user messages through the LLM for fact extraction."""
    if not messages:
        return
    
    # Group messages by their real owning user. Skip any without a user_id rather
    # than misattributing them to user 1.
    by_user = {}
    for m in messages:
        uid = m.get("user_id")
        if not uid:
            continue
        by_user.setdefault(uid, []).append(m)
    
    for user_id, user_msgs in by_user.items():
        # Build a combined block of user messages for extraction
        exchange_text = "\n".join(
            f"User said: {m['content']}" for m in user_msgs
        )
        
        try:
            llm_messages = [
                {"role": "system", "content": FACT_EXTRACTION_PROMPT},
                {"role": "user", "content": exchange_text}
            ]
            result = request_llm(llm_messages, temperature=0.1, n_predict=512)
            response_text = _llm_content(result).strip()
            
            # Parse JSON from response (handle markdown code blocks)
            if response_text.startswith("```"):
                response_text = response_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            
            facts = json.loads(response_text)
            if not isinstance(facts, list):
                facts = []
            
            valid_categories = {"personal", "family", "preferences", "location", "work", "education", "interests", "technical", "other"}
            
            for fact in facts:
                if not isinstance(fact, dict):
                    continue
                category = fact.get("category", "other").lower().strip()
                content = fact.get("content", "").strip()
                if not content or len(content) < 5:
                    continue
                if category not in valid_categories:
                    category = "other"
                store_fact(int(user_id), category, content, source="auto")
            
            if facts:
                logger.info("Memory Core: Extracted %d facts from %d messages for user %d", len(facts), len(user_msgs), user_id)
            
        except json.JSONDecodeError:
            logger.warning("Memory Core: LLM returned non-JSON for fact extraction")
        except Exception as e:
            logger.error("Memory Core: Extraction error: %s", e)
    
    # Mark all processed
    _mark_messages_processed([m["id"] for m in messages])

def _memory_worker():
    """Background thread: periodically checks for idle time, then extracts facts."""
    global _memory_worker_running
    _memory_worker_running = True
    logger.info("Memory Core: Background worker started (idle threshold=%ds, check interval=%ds)",
                IDLE_THRESHOLD_SECONDS, IDLE_CHECK_INTERVAL)
    
    while _memory_worker_running:
        try:
            time.sleep(IDLE_CHECK_INTERVAL)
            
            # Never run while a chat request is in flight, or before the idle threshold.
            if _is_busy():
                continue
            idle_duration = time.time() - _last_activity_time
            if idle_duration < IDLE_THRESHOLD_SECONDS:
                continue  # User is still active, skip

            # System is idle — check for unprocessed messages
            unprocessed = _get_unprocessed_messages(batch_size=20)
            if not unprocessed:
                continue  # Nothing to process
            
            logger.info("Memory Core: System idle for %.0fs, processing %d unextracted messages",
                       idle_duration, len(unprocessed))
            _extract_facts_batch(unprocessed)
            
        except Exception as e:
            logger.error("Memory Core: Worker error: %s", e)
            time.sleep(60)  # Back off on errors

def start_memory_worker():
    """Start the background memory extraction worker thread."""
    t = threading.Thread(target=_memory_worker, daemon=True, name="memory-core")
    t.start()
    return t

# DB Methods
def store_message(session_id: str, speaker: str, content: str):
    conn = get_db()
    try:
        cursor = conn.execute("INSERT INTO conversation_history (session_id, speaker, content) VALUES (?, ?, ?)", (session_id, speaker, content))
        msg_id = cursor.lastrowid
        user_id_row = conn.execute("SELECT user_id FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()
        conn.commit()
    finally:
        conn.close()

    # Hand the heavy embedding off to the background worker so it never blocks the
    # chat response. The owning user_id comes from the (now always-present) session row.
    if memory_collection is not None and user_id_row is not None:
        metadata = {"session_id": session_id, "speaker": speaker, "user_id": int(user_id_row["user_id"])}
        _embed_queue.put((msg_id, content, metadata))

def get_recent_context(session_id: str, limit: Optional[int] = None) -> List[Dict[str, str]]:
    limit = limit or MAX_CONTEXT_MESSAGES
    conn = get_db()
    try:
        cursor = conn.execute("SELECT speaker, content FROM conversation_history WHERE session_id = ? ORDER BY id DESC LIMIT ?", (session_id, limit))
        rows = cursor.fetchall()
        messages = [{"role": "assistant" if r["speaker"] == "jarvis" else "user", "content": r["content"]} for r in reversed(rows)]
        return messages
    finally: conn.close()

def get_sessions(user_id: int) -> List[Dict[str, Any]]:
    conn = get_db()
    try:
        cursor = conn.execute("SELECT id, title, created_at FROM chat_sessions WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
        return [dict(row) for row in cursor.fetchall()]
    finally: conn.close()

def create_session(title: str, user_id: int) -> str:
    session_id = str(uuid.uuid4())
    conn = get_db()
    try:
        conn.execute("INSERT INTO chat_sessions (id, title, user_id) VALUES (?, ?, ?)", (session_id, title, user_id))
        conn.commit()
        return session_id
    finally: conn.close()

def resolve_session(session_id: Optional[str], user_id: int) -> str:
    """Map a missing/'default' session to THIS user's own default session.

    The old code treated the literal string 'default' as a single shared, unowned
    bucket that every user (and the voice listener) wrote into — causing cross-user
    data mixing, fail-open ownership checks, and fact misattribution to user 1.
    Now 'default' resolves to a real, per-user chat_sessions row (created lazily),
    so every code path goes through the same ownership check with no special cases.
    """
    if not session_id or session_id == "default":
        sid = f"u{user_id}-default"
        conn = get_db()
        try:
            if not conn.execute("SELECT 1 FROM chat_sessions WHERE id = ?", (sid,)).fetchone():
                conn.execute(
                    "INSERT INTO chat_sessions (id, title, user_id) VALUES (?, ?, ?)",
                    (sid, "Quick Chat", user_id),
                )
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
    finally: conn.close()

def delete_session(session_id: str, user_id: int):
    conn = get_db()
    try:
        # Get message IDs before deleting so we can clean ChromaDB
        msg_ids = [str(r["id"]) for r in conn.execute(
            "SELECT id FROM conversation_history WHERE session_id = ?", (session_id,)
        ).fetchall()]
        
        conn.execute("DELETE FROM chat_sessions WHERE id = ? AND user_id = ?", (session_id, user_id))
        conn.execute("DELETE FROM conversation_history WHERE session_id = ?", (session_id,))
        conn.commit()
        
        # Clean ChromaDB vectors for deleted session
        if memory_collection and msg_ids:
            try:
                memory_collection.delete(ids=msg_ids)
                logger.info("Cleaned %d vectors from ChromaDB for session %s", len(msg_ids), session_id[:8])
            except Exception as e:
                logger.error("ChromaDB cleanup error: %s", e)
    finally: conn.close()

# LLM Logic (unchanged logic)
def request_llm(messages: List[Dict[str, str]], temperature=None, top_k=None, top_p=None, min_p=None, repeat_penalty=None, presence_penalty=None, frequency_penalty=None, n_predict=None, seed=None) -> Dict[str, Any]:
    temperature = temperature if temperature is not None else TEMPERATURE
    data = {"messages": messages, "temperature": temperature, "stream": False}
    if top_k is not None: data["top_k"] = top_k
    if top_p is not None: data["top_p"] = top_p
    if min_p is not None: data["min_p"] = min_p
    if repeat_penalty is not None: data["repeat_penalty"] = repeat_penalty
    if presence_penalty is not None: data["presence_penalty"] = presence_penalty
    if frequency_penalty is not None: data["frequency_penalty"] = frequency_penalty
    if n_predict is not None: data["max_tokens"] = n_predict
    if seed is not None: data["seed"] = seed

    req = urllib.request.Request(LLM_URL, data=json.dumps(data).encode("utf-8"), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        logger.error("LLM error: %s", e)
        raise HTTPException(status_code=503, detail="AI backend error")

def request_llm_stream(messages: List[Dict[str, str]], temperature=None, top_k=None, top_p=None, min_p=None, repeat_penalty=None, presence_penalty=None, frequency_penalty=None, n_predict=None, seed=None):
    temperature = temperature if temperature is not None else TEMPERATURE
    data = {"messages": messages, "temperature": temperature, "stream": True}
    if top_k is not None: data["top_k"] = top_k
    if top_p is not None: data["top_p"] = top_p
    if min_p is not None: data["min_p"] = min_p
    if repeat_penalty is not None: data["repeat_penalty"] = repeat_penalty
    if presence_penalty is not None: data["presence_penalty"] = presence_penalty
    if frequency_penalty is not None: data["frequency_penalty"] = frequency_penalty
    if n_predict is not None: data["max_tokens"] = n_predict
    if seed is not None: data["seed"] = seed

    req = urllib.request.Request(LLM_URL, data=json.dumps(data).encode("utf-8"), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            for line in response:
                line = line.decode('utf-8').strip()
                if line.startswith("data: ") and line != "data: [DONE]":
                    try:
                        chunk = json.loads(line[6:])
                        if "choices" in chunk and chunk["choices"]:
                            content = chunk["choices"][0]["delta"].get("content", "")
                            if content:
                                yield content
                    except json.JSONDecodeError:
                        pass
    except Exception as e:
        logger.error("LLM streaming error: %s", e)
        raise

def _llm_content(resp: Dict[str, Any]) -> str:
    """Safely pull the assistant text out of an OpenAI-style response.

    llama-server can return an error object or an empty choices list; indexing it
    positionally would surface as an unhandled 500. We validate and raise 503 instead.
    """
    try:
        return resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.error("Unexpected LLM response shape: %s", str(resp)[:300])
        raise HTTPException(status_code=503, detail="AI backend returned an unexpected response")

def retrieve_long_term_memory(user_id: int, current_session_id: str, user_text: str, recent_context_ids: Optional[set] = None) -> str:
    """Retrieve relevant memories from vector DB. Searches ALL sessions for this user,
    but deduplicates against messages already in the recent context window."""
    if memory_collection is None or _embed_model is None:
        return ""
    try:
        # Recall the USER's own past statements across all their sessions. We restrict to
        # speaker='user' because the assistant's replies are verbose, model-generated, and
        # semantically near the question — they tend to crowd out the actual facts.
        results = memory_collection.query(
            query_embeddings=_embed_query(user_text),
            n_results=RAG_MAX_RESULTS,
            include=["documents", "metadatas", "distances"],
            where={"$and": [{"user_id": int(user_id)}, {"speaker": "user"}]},
        )

        if not results["documents"] or not results["documents"][0]:
            logger.debug("RAG: No results found for user %d", user_id)
            return ""

        docs = results["documents"][0]
        metas = results["metadatas"][0]
        dists = results["distances"][0]
        ids = results["ids"][0] if results.get("ids") else [None] * len(docs)

        memory_blocks = []
        seen_content = set()  # Deduplicate

        for i, (doc, meta, dist) in enumerate(zip(docs, metas, dists)):
            # Filter by distance threshold — discard irrelevant matches
            if dist > RAG_DISTANCE_THRESHOLD:
                continue

            # Skip if this exact message is already in the recent context window.
            # Index by position (ids[i]) — NOT docs.index(doc), which mis-maps duplicates.
            msg_id = ids[i]
            if recent_context_ids and msg_id and msg_id in recent_context_ids:
                continue

            # Deduplicate by content
            content_key = doc[:100].strip().lower()
            if content_key in seen_content:
                continue
            seen_content.add(content_key)

            session_label = "(current)" if meta.get("session_id") == current_session_id else "(past)"
            memory_blocks.append(f"User {session_label}: {doc}")
        
        if memory_blocks:
            logger.info("RAG: Retrieved %d relevant memories (of %d candidates, threshold=%.1f)",
                       len(memory_blocks), len(results["documents"][0]), RAG_DISTANCE_THRESHOLD)
        else:
            logger.debug("RAG: All %d candidates filtered out (distance > %.1f or duplicates)",
                        len(results["documents"][0]), RAG_DISTANCE_THRESHOLD)
        
        return "\n".join(memory_blocks)
    except Exception as e:
        logger.error("Vector DB Search Error: %s", e)
        return ""

def _get_recent_message_ids(session_id: str) -> set:
    """Get the IDs of messages already in the recent context window so RAG can skip them."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id FROM conversation_history WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, MAX_CONTEXT_MESSAGES)
        ).fetchall()
        return {str(r["id"]) for r in rows}
    finally:
        conn.close()

def build_messages(session_id: str, user_id: int, user_text: str, custom_sys_prompt: Optional[str] = None,
                   completion_reserve: int = COMPLETION_RESERVE_DEFAULT) -> List[Dict[str, str]]:
    """Assemble the prompt within the model's context window.

    Layout: [single system message] + [recent history…] + [current turn].
    The system prompt, the user-profile block, and recalled RAG memories are merged into ONE
    system message (the Qwen chat template rejects more than one / a non-leading system message).
    The system message and current turn are always kept; recent history is added newest-first
    only while it fits the remaining token budget — replacing the old behaviour of blindly
    stacking up to 100 messages, which overflowed the -c window.
    """
    sys_prompt = custom_sys_prompt if custom_sys_prompt else SYSTEM_PROMPT
    system_parts = [sys_prompt]

    # Inject persistent user knowledge (Memory Core), capped so it can't dominate the window.
    knowledge = get_user_knowledge(user_id)
    if knowledge:
        knowledge = _truncate_to_tokens(knowledge, KNOWLEDGE_TOKEN_CAP)
        system_parts.append(
            "--- USER PROFILE (persistent knowledge) ---\n"
            f"{knowledge}\n"
            "(Use this information naturally. Do not repeat it back unless asked.)\n"
            "---"
        )

    # Inject RAG memories (contextual recall from past conversations)
    context_ids = _get_recent_message_ids(session_id)
    memories = retrieve_long_term_memory(user_id, session_id, user_text, recent_context_ids=context_ids)
    if memories:
        system_parts.append(
            "--- RECALLED MEMORIES ---\n"
            f"{memories}\n"
            "(If the current conversation contradicts these, prioritize the current conversation.)\n"
            "---"
        )

    front: List[Dict[str, str]] = [{"role": "system", "content": "\n\n".join(system_parts)}]
    current_turn = {"role": "user", "content": user_text}

    # Token budget for the prompt = window - reserved completion - safety margin.
    prompt_budget = MAX_CONTEXT_TOKENS - max(completion_reserve, MIN_COMPLETION_TOKENS) - PROMPT_SAFETY_MARGIN
    prompt_budget = max(prompt_budget, MAX_CONTEXT_TOKENS // 2)  # always leave room for some history
    fixed_tokens = sum(_estimate_message_tokens(m) for m in front) + _estimate_message_tokens(current_turn)

    history = get_recent_context(session_id)  # chronological (oldest -> newest)
    included = fit_history(history, prompt_budget - fixed_tokens)

    return front + included + [current_turn]

def _clamp_completion(messages: List[Dict[str, str]], requested: Optional[int]) -> int:
    """Clamp the requested completion length so prompt + completion fits the context window."""
    prompt_tokens = sum(_estimate_message_tokens(m) for m in messages)
    return clamp_completion(
        prompt_tokens, requested or 0, MAX_CONTEXT_TOKENS,
        PROMPT_SAFETY_MARGIN, MIN_COMPLETION_TOKENS, COMPLETION_RESERVE_DEFAULT,
    )

PIPER_BIN = Path("/srv/jarvis/piper/piper")
PIPER_MODEL = Path("/srv/jarvis/piper/voices/en_GB-alan-medium.onnx")

def synthesize_tts(text: str) -> Optional[str]:
    """Render text to speech via Piper, returning base64 WAV (or None if unavailable/failed)."""
    if not text or not (PIPER_BIN.exists() and PIPER_MODEL.exists()):
        return None
    try:
        proc = subprocess.run(
            [str(PIPER_BIN), "--model", str(PIPER_MODEL), "--output_file", "-"],
            input=text.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
        )
        if proc.returncode == 0 and proc.stdout:
            return base64.b64encode(proc.stdout).decode("utf-8")
    except Exception as e:
        logger.warning("Piper TTS failed: %s", e)
    return None

ADMIN_MAX_INPUT = 10000
REGULAR_MAX_INPUT = 500

class QueryRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=ADMIN_MAX_INPUT)
    session_id: str = Field(default="default")
    temperature: Optional[float] = None
    top_k: Optional[int] = None
    top_p: Optional[float] = None
    min_p: Optional[float] = None
    repeat_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    n_predict: Optional[int] = Field(default=None, ge=1, le=8192)
    seed: Optional[int] = None
    system_prompt: Optional[str] = Field(default=None, max_length=2000)
    voice_feedback: bool = False

class SessionRenameRequest(BaseModel):
    title: str

class LoginRequest(BaseModel):
    username: str
    password: str

# ----------------- ENDPOINTS -----------------
@app.post("/auth/login")
def login(req: LoginRequest):
    conn = get_db()
    try:
        cursor = conn.execute("SELECT id, password_hash, role FROM users WHERE username = ?", (req.username,))
        row = cursor.fetchone()
        if not row or not verify_password(req.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid username or password")
        
        token = secrets.token_hex(32)
        # 30 days expiry
        expires = (datetime.now(timezone.utc) + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        conn.execute("INSERT INTO auth_sessions (token, user_id, expires_at) VALUES (?, ?, ?)", (token, row["id"], expires))
        # Opportunistic cleanup of expired tokens so the table can't grow forever.
        conn.execute("DELETE FROM auth_sessions WHERE expires_at <= datetime('now')")
        conn.commit()
        return {"token": token, "role": row["role"]}
    finally:
        conn.close()

@app.post("/auth/logout")
def logout(request: Request):
    """Revoke the caller's current session token server-side (real logout)."""
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if token:
        conn = get_db()
        try:
            conn.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))
            conn.commit()
        finally:
            conn.close()
    return {"status": "ok"}

@app.get("/sessions")
def list_sessions(request: Request):
    return {"sessions": get_sessions(request.state.user_id)}

@app.post("/sessions")
def new_session(request: Request):
    s_id = create_session("New Chat", request.state.user_id)
    return {"id": s_id, "title": "New Chat"}

@app.put("/sessions/{session_id}")
def update_session(session_id: str, req: SessionRenameRequest, request: Request):
    rename_session(session_id, req.title, request.state.user_id)
    return {"status": "ok"}

@app.delete("/sessions/{session_id}")
def remove_session(session_id: str, request: Request):
    delete_session(session_id, request.state.user_id)
    return {"status": "ok"}

@app.get("/history/{session_id}")
def get_session_history(session_id: str, request: Request):
    session_id = resolve_session(session_id, request.state.user_id)
    require_owned_session(session_id, request.state.user_id)
    context = get_recent_context(session_id, limit=100)
    return {"messages": context, "count": len(context)}

@app.get("/health")
def health_check() -> Dict[str, str]:
    return {"status": "ok", "model": "qwen3.5-2b"}

@app.post("/inbox")
def process_input(request: QueryRequest, raw_request: Request):
    _update_activity()  # Track activity for idle-time extraction
    user_text = request.text.strip()
    if not user_text: raise HTTPException(status_code=400, detail="Empty input")

    is_admin = getattr(raw_request.state, "is_admin", False)
    if not is_admin and len(user_text) > REGULAR_MAX_INPUT:
        raise HTTPException(status_code=400, detail="Input too long (max 500 chars)")

    user_id = raw_request.state.user_id
    session_id = resolve_session(request.session_id, user_id)
    require_owned_session(session_id, user_id)

    existing_context = get_recent_context(session_id)
    needs_title = (len(existing_context) == 0) and not is_default_session(session_id)
    completion_reserve = request.n_predict if (request.n_predict and request.n_predict > 0) else COMPLETION_RESERVE_DEFAULT
    messages = build_messages(session_id, user_id, user_text, request.system_prompt,
                              completion_reserve=completion_reserve)
    max_tokens = _clamp_completion(messages, request.n_predict)

    t0 = time.time()
    with _Inflight():
        llm_resp = request_llm(
            messages, temperature=request.temperature, top_k=request.top_k, top_p=request.top_p,
            min_p=request.min_p, repeat_penalty=request.repeat_penalty, presence_penalty=request.presence_penalty,
            frequency_penalty=request.frequency_penalty, n_predict=max_tokens, seed=request.seed
        )
    t1 = time.time()

    answer = _llm_content(llm_resp).strip()
    comp_tokens = llm_resp.get("usage", {}).get("completion_tokens", 0)
    speed_str = ""
    timings = llm_resp.get("timings", {})
    if "predicted_per_second" in timings:
        speed_str = f"{timings['predicted_per_second']:.1f} tok/s"
    elif comp_tokens > 0 and (t1 - t0) > 0:
        speed_str = f"{(comp_tokens / (t1 - t0)):.1f} tok/s (wall)"

    audio_b64 = synthesize_tts(answer) if request.voice_feedback else None

    store_message(session_id, "user", user_text)
    store_message(session_id, "jarvis", answer)

    new_title = None
    if needs_title:
        try:
            title_resp = request_llm([{"role": "system", "content": "Reply with a very short title (1-4 words). No quotes."}, {"role": "user", "content": user_text}], temperature=0.3, n_predict=10)
            gen_title = _llm_content(title_resp).replace('"', '').replace('.', '').strip()
            if gen_title:
                rename_session(session_id, gen_title, user_id)
                new_title = gen_title
        except Exception as e:
            logger.warning("Title generation failed: %s", e)

    return {"response": answer, "speed": speed_str, "new_title": new_title, "audio": audio_b64}

@app.post("/chat/stream")
def chat_stream(request: QueryRequest, raw_request: Request):
    _update_activity()  # Track activity for idle-time extraction
    user_text = request.text.strip()
    if not user_text: raise HTTPException(status_code=400, detail="Empty input")

    is_admin = getattr(raw_request.state, "is_admin", False)
    if not is_admin and len(user_text) > REGULAR_MAX_INPUT:
        raise HTTPException(status_code=400, detail="Input too long (max 500 chars)")

    user_id = raw_request.state.user_id
    session_id = resolve_session(request.session_id, user_id)
    require_owned_session(session_id, user_id)

    existing_context = get_recent_context(session_id)
    needs_title = (len(existing_context) == 0) and not is_default_session(session_id)
    completion_reserve = request.n_predict if (request.n_predict and request.n_predict > 0) else COMPLETION_RESERVE_DEFAULT
    messages = build_messages(session_id, user_id, user_text, request.system_prompt,
                              completion_reserve=completion_reserve)
    max_tokens = _clamp_completion(messages, request.n_predict)

    def event_generator():
        full_answer = []
        error_occurred = False
        # Mark the request in-flight for the whole generation so the fact-extraction
        # worker won't contend for the single LLM slot mid-stream.
        with _Inflight():
            try:
                for chunk in request_llm_stream(
                    messages, temperature=request.temperature, top_k=request.top_k, top_p=request.top_p,
                    min_p=request.min_p, repeat_penalty=request.repeat_penalty, presence_penalty=request.presence_penalty,
                    frequency_penalty=request.frequency_penalty, n_predict=max_tokens, seed=request.seed
                ):
                    full_answer.append(chunk)
                    yield f"data: {json.dumps({'content': chunk})}\n\n"
            except Exception as e:
                error_occurred = True
                logger.error("Error generating stream: %s", e)
                yield f"data: {json.dumps({'error': 'AI backend error'})}\n\n"

            answer_text = "".join(full_answer)

            # Persist the user turn even on failure so the user's input is never lost.
            # The assistant turn is stored only when we actually produced content — an
            # error string is never written into history as if it were a real reply.
            store_message(session_id, "user", user_text)
            if answer_text:
                store_message(session_id, "jarvis", answer_text)

            if not answer_text:
                yield f"data: {json.dumps({'done': True, 'error': error_occurred})}\n\n"
                return

            new_title = None
            if needs_title:
                try:
                    title_resp = request_llm([{"role": "system", "content": "Reply with a very short title (1-4 words). No quotes."}, {"role": "user", "content": user_text}], temperature=0.3, n_predict=10)
                    gen_title = _llm_content(title_resp).replace('"', '').replace('.', '').strip()
                    if gen_title:
                        rename_session(session_id, gen_title, user_id)
                        new_title = gen_title
                except Exception as e:
                    logger.warning("Title generation failed: %s", e)

            audio_b64 = synthesize_tts(answer_text) if request.voice_feedback else None

            done_payload: Dict[str, Any] = {"done": True}
            if new_title:
                done_payload["new_title"] = new_title
            if audio_b64:
                done_payload["audio"] = audio_b64
            yield f"data: {json.dumps(done_payload)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

# ----------------- ADMIN UI & ENDPOINTS -----------------
class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "user"

@app.post("/admin/users")
def admin_create_user(req: CreateUserRequest, request: Request):
    if not getattr(request.state, "is_admin", False): raise HTTPException(status_code=403, detail="Admin only")
    conn = get_db()
    try:
        p_hash = hash_password(req.password)
        conn.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)", (req.username, p_hash, req.role))
        conn.commit()
        return {"status": "ok"}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Username exists")
    finally: conn.close()

@app.get("/admin/users")
def admin_list_users(request: Request):
    if not getattr(request.state, "is_admin", False): raise HTTPException(status_code=403, detail="Admin only")
    conn = get_db()
    try:
        query = """
            SELECT 
                u.id, u.username, u.role, u.created_at,
                COUNT(DISTINCT c.id) as total_chats,
                COUNT(m.id) as total_messages
            FROM users u
            LEFT JOIN chat_sessions c ON u.id = c.user_id
            LEFT JOIN conversation_history m ON c.id = m.session_id
            GROUP BY u.id
        """
        users = conn.execute(query).fetchall()
        return {"users": [dict(u) for u in users]}
    finally: conn.close()

@app.delete("/admin/users/{user_id}")
def admin_delete_user(user_id: int, request: Request):
    if not getattr(request.state, "is_admin", False): raise HTTPException(status_code=403, detail="Admin only")
    if user_id == request.state.user_id: raise HTTPException(status_code=400, detail="Cannot delete self")
    conn = get_db()
    try:
        sessions = conn.execute("SELECT id FROM chat_sessions WHERE user_id = ?", (user_id,)).fetchall()
        all_msg_ids = []
        for (sid,) in sessions:
            msg_rows = conn.execute("SELECT id FROM conversation_history WHERE session_id = ?", (sid,)).fetchall()
            all_msg_ids.extend([str(r["id"]) for r in msg_rows])
            conn.execute("DELETE FROM conversation_history WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM chat_sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        
        # Clean ChromaDB vectors for deleted user
        if memory_collection and all_msg_ids:
            try:
                # ChromaDB delete in batches of 500
                for i in range(0, len(all_msg_ids), 500):
                    batch = all_msg_ids[i:i+500]
                    memory_collection.delete(ids=batch)
                logger.info("Cleaned %d vectors from ChromaDB for deleted user %d", len(all_msg_ids), user_id)
            except Exception as e:
                logger.error("ChromaDB cleanup error for user %d: %s", user_id, e)
        
        return {"status": "ok"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally: conn.close()

class CreateKeyRequest(BaseModel):
    user_id: int
    description: str

@app.post("/admin/api_keys")
def admin_create_key(req: CreateKeyRequest, request: Request):
    if not getattr(request.state, "is_admin", False): raise HTTPException(status_code=403, detail="Admin only")
    conn = get_db()
    try:
        new_key = "jk-" + secrets.token_hex(16)
        conn.execute("INSERT INTO api_keys (key_string, user_id, description) VALUES (?, ?, ?)", (new_key, req.user_id, req.description))
        conn.commit()
        return {"key": new_key}
    finally: conn.close()

@app.get("/admin/api_keys")
def admin_list_keys(request: Request):
    if not getattr(request.state, "is_admin", False): raise HTTPException(status_code=403, detail="Admin only")
    conn = get_db()
    try:
        # Gracefully handle missing columns before init_db fires
        try:
            keys = conn.execute("SELECT key_string, user_id, description, created_at, usage_count, last_used_at FROM api_keys").fetchall()
        except sqlite3.OperationalError:
            keys = conn.execute("SELECT key_string, user_id, description, created_at, 0 as usage_count, NULL as last_used_at FROM api_keys").fetchall()
            
        masked = []
        for k in keys:
            km = dict(k)
            km["full_key"] = km["key_string"]
            km["key_string"] = km["key_string"][:6] + "..." + km["key_string"][-4:]
            masked.append(km)
        return {"keys": masked}
    finally: conn.close()

@app.delete("/admin/api_keys/{key_string}")
def admin_delete_key(key_string: str, request: Request):
    if not getattr(request.state, "is_admin", False): raise HTTPException(status_code=403, detail="Admin only")
    conn = get_db()
    try:
        conn.execute("DELETE FROM api_keys WHERE key_string = ?", (key_string,))
        conn.commit()
        return {"status": "ok"}
    finally: conn.close()

@app.get("/admin/stats")
def admin_stats(request: Request):
    if not getattr(request.state, "is_admin", False): raise HTTPException(status_code=403, detail="Admin only")
    conn = get_db()
    try:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        chats = conn.execute("SELECT COUNT(*) FROM chat_sessions").fetchone()[0]
        msgs = conn.execute("SELECT COUNT(*) FROM conversation_history").fetchone()[0]
        return {"users": users, "chats": chats, "messages": msgs}
    finally: conn.close()

@app.get("/")
def serve_ui():
    if not INDEX_HTML.exists(): raise HTTPException(status_code=404)
    return FileResponse(INDEX_HTML, media_type="text/html")

@app.get("/admin")
def serve_admin():
    if not ADMIN_HTML.exists(): raise HTTPException(status_code=404)
    return FileResponse(ADMIN_HTML, media_type="text/html")

if REACT_DIST_DIR.exists():
    # React build puts CSS/JS in dist/assets
    assets_dir = REACT_DIST_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

# Still need /static for admin panel
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ---- Knowledge API Endpoints ----
class KnowledgeFactRequest(BaseModel):
    content: str
    category: str = "other"

@app.get("/knowledge")
def list_knowledge(request: Request):
    """Get all stored facts for the current user."""
    facts = get_user_knowledge_list(request.state.user_id)
    return {"facts": facts, "count": len(facts)}

@app.post("/knowledge")
def add_knowledge(req: KnowledgeFactRequest, request: Request):
    """Manually add a fact to the knowledge base."""
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Empty content")
    valid_categories = {"personal", "family", "preferences", "location", "work", "education", "interests", "technical", "other"}
    category = req.category.lower().strip()
    if category not in valid_categories:
        category = "other"
    fact_id = store_fact(request.state.user_id, category, content, source="manual")
    return {"id": fact_id, "status": "ok"}

@app.put("/knowledge/{fact_id}")
def edit_knowledge(fact_id: int, req: KnowledgeFactRequest, request: Request):
    """Edit an existing fact."""
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Empty content")
    update_fact(fact_id, request.state.user_id, content, req.category.lower().strip() if req.category else None)
    return {"status": "ok"}

@app.delete("/knowledge/{fact_id}")
def remove_knowledge(fact_id: int, request: Request):
    """Delete a fact from the knowledge base."""
    delete_fact(fact_id, request.state.user_id)
    return {"status": "ok"}

@app.post("/knowledge/extract-now")
def force_extraction(request: Request):
    """Admin: Force immediate extraction of all unprocessed messages."""
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin only")
    unprocessed = _get_unprocessed_messages(batch_size=50)
    if not unprocessed:
        return {"status": "ok", "processed": 0, "message": "No unprocessed messages"}
    _extract_facts_batch(unprocessed)
    return {"status": "ok", "processed": len(unprocessed)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=CONFIG["orchestrator"]["host"], port=CONFIG["orchestrator"]["port"])