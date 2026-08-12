"""Jarvis Memory Core: embeddings + vector store, the user knowledge base,
idle-time fact extraction, and request-activity tracking.

Depends on config, db, and llm — never on chat/main (keeps the import graph acyclic).
"""
import json
import os
import queue
import re
import threading
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from config import (
    BASE_DIR, CHROMA_DB_PATH, EMBED_DOC_PREFIX, EMBED_MODEL_NAME, EMBED_ONNX_DIR, EMBED_QUERY_PREFIX,
    FACT_DEDUP_SIM, FACT_DEDUP_WORD, FACT_EXTRACTION_BATCH, FACT_EXTRACTION_MAX_ATTEMPTS,
    FACT_EXTRACTION_MIN_TOKENS, FACT_EXTRACTION_PROMPT, FACT_EXTRACTION_TOKENS,
    IDLE_CHECK_INTERVAL, MAX_CONTEXT_TOKENS, PROMPT_SAFETY_MARGIN, WARM_CACHE_AFTER_EXTRACTION,
    IDLE_THRESHOLD_SECONDS, RAG_DISTANCE_THRESHOLD,
    RAG_MAX_RESULTS, VALID_FACT_CATEGORIES, logger,
)
from budget import estimate_message_tokens, estimate_tokens, truncate_to_tokens
from db import get_db
from llm import llm_content, request_llm, warm_prefix

# --- Embeddings + vector store ----------------------------------------------
# Initialized lazily by init_embeddings() (called once at startup), NOT at import:
# the model is ~1.2 GB cached on disk and takes seconds to load, so importing this
# module stays cheap (lets the app/tests import without pulling the model).
chroma_client = None
_embed_model = None        # a SentenceTransformer once init_embeddings() has run
memory_collection = None
_embed_init_done = False


def init_embeddings():
    """Load the embedding model (from the local HF cache) and open the ChromaDB
    collection. Idempotent; call once at startup. Set JARVIS_NO_EMBED=1 to skip it
    entirely (RAG disabled) — used by tests so they don't load the model.
    """
    global chroma_client, _embed_model, memory_collection, _embed_init_done
    if _embed_init_done:
        return
    _embed_init_done = True
    if os.environ.get("JARVIS_NO_EMBED") == "1":
        logger.info("JARVIS_NO_EMBED=1 — embeddings/RAG disabled")
        return
    # Torch-free fast path: an exported ONNX pipeline (src/scripts/export_embed_onnx.py). Only used
    # when its meta.json model MATCHES the configured EMBED_MODEL_NAME — a mismatch would silently
    # produce vectors from a different space. Any failure falls back to sentence-transformers below.
    onnx_model = None
    try:
        meta_path = os.path.join(EMBED_ONNX_DIR, "meta.json")
        if not os.path.isfile(meta_path):
            # Fetching models is SETUP work, not something a request-serving process should do:
            # it means network egress, a multi-hundred-MB write and an arbitrary-length stall
            # inside the app's own startup — and under the hardened unit it cannot succeed anyway
            # (ProtectSystem=strict). setup.sh and download_models.sh are where this belongs, and
            # the images bake the bundle at /opt/jarvis/embed_onnx, so nothing needs it by default.
            # JARVIS_AUTO_DOWNLOAD_MODELS=1 opts back in for a hands-off first run.
            if os.environ.get("JARVIS_AUTO_DOWNLOAD_MODELS") == "1":
                logger.info("ONNX embedding bundle not found at %s. JARVIS_AUTO_DOWNLOAD_MODELS=1 "
                            "— fetching via download_models.sh...", EMBED_ONNX_DIR)
                try:
                    import subprocess
                    script_path = BASE_DIR / "src" / "scripts" / "download_models.sh"
                    if script_path.exists():
                        subprocess.run(["bash", str(script_path)], check=True)
                except Exception as e:
                    logger.warning("Auto-download of embedding model failed: %s", e)
            else:
                logger.warning("No ONNX embedding bundle at %s — run 'bash src/scripts/download_models.sh'. "
                               "(Set JARVIS_AUTO_DOWNLOAD_MODELS=1 to fetch it automatically at startup.)",
                               EMBED_ONNX_DIR)

        if os.path.isfile(meta_path):
            with open(meta_path) as f:
                onnx_meta_model = json.load(f).get("model")
            if onnx_meta_model == EMBED_MODEL_NAME:
                from onnx_embed import OnnxEmbedder
                onnx_model = OnnxEmbedder(EMBED_ONNX_DIR)
            else:
                logger.warning("ONNX embed dir %s holds '%s' but EMBED_MODEL is '%s' — ignoring it "
                               "(re-export or fix EMBED_MODEL)", EMBED_ONNX_DIR, onnx_meta_model, EMBED_MODEL_NAME)
    except Exception as e:
        logger.warning("ONNX embedding init failed (%s: %s) — falling back to sentence-transformers",
                       type(e).__name__, e)
        onnx_model = None

    try:
        # Imported here, not at module top: torch/sentence-transformers are heavy to
        # import (tens of seconds on this CPU), so deferring keeps `import memory` cheap.
        import chromadb
        chroma_client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
        if onnx_model is not None:
            _embed_model = onnx_model
        else:
            # Legacy torch path — sentence-transformers is NO LONGER a project dependency (the ONNX
            # bundle is the supported runtime). This only works if someone installed it manually.
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError:
                raise RuntimeError(
                    f"no ONNX embedding bundle at {EMBED_ONNX_DIR} — run "
                    "'bash src/scripts/download_models.sh' to fetch it (no token needed), or export "
                    "one for a custom model with src/scripts/export_embed_onnx.py"
                ) from None
            # trust_remote_code=False: never execute code shipped in the model repo (supply-chain RCE
            # guard). Pin EMBED_MODEL_REVISION=<commit> for a tamper-evident load (else uses the cache).
            _embed_model = SentenceTransformer(
                EMBED_MODEL_NAME, trust_remote_code=False,
                revision=os.environ.get("EMBED_MODEL_REVISION") or None)
        # Cosine space with normalized vectors (the "jarvis_memory_cos" collection).
        memory_collection = chroma_client.get_or_create_collection(
            name="jarvis_memory_cos", metadata={"hnsw:space": "cosine"}
        )
        # Surface the embedding dimension + collection size. If EMBED_MODEL_NAME ever
        # changes dimension, get_or_create returns the OLD collection and the first add()
        # would fail in the worker — logging this makes that diagnosable.
        # method was renamed get_sentence_embedding_dimension → get_embedding_dimension; support both
        _dim = (_embed_model.get_embedding_dimension if hasattr(_embed_model, "get_embedding_dimension")
                else _embed_model.get_sentence_embedding_dimension)()
        logger.info("Embeddings: %s via %s (dim=%d), collection 'jarvis_memory_cos' has %d vectors",
                    EMBED_MODEL_NAME, getattr(_embed_model, "runtime", "torch/sentence-transformers"),
                    _dim, memory_collection.count())
    except Exception as e:
        logger.error("Failed to initialize ChromaDB / embedding model: %s", e)
        _embed_model = None
        memory_collection = None


def vectors_available() -> bool:
    return memory_collection is not None and _embed_model is not None


def embedding_status() -> Dict[str, Any]:
    """Embedding model name, vector dimension, and stored-memory count — for the admin health board.
    Best-effort: never raises (returns available=False if the model didn't load)."""
    if not vectors_available():
        return {"available": False, "model": EMBED_MODEL_NAME}
    try:
        dim = (_embed_model.get_embedding_dimension if hasattr(_embed_model, "get_embedding_dimension")
               else _embed_model.get_sentence_embedding_dimension)()
    except Exception:
        dim = None
    try:
        count = memory_collection.count()
    except Exception:
        count = None
    return {"available": True, "model": EMBED_MODEL_NAME, "dim": dim, "count": count,
            "runtime": getattr(_embed_model, "runtime", "torch")}


def _embed_documents(texts: List[str]) -> List[List[float]]:
    vecs = _embed_model.encode([EMBED_DOC_PREFIX + t for t in texts], normalize_embeddings=True)
    return [v.tolist() for v in vecs]


def _embed_query(text: str) -> List[List[float]]:
    vec = _embed_model.encode([EMBED_QUERY_PREFIX + text], normalize_embeddings=True)
    return [vec[0].tolist()]


# Public wrappers for other consumers (the semantic intent router) — same prefixes/normalization as
# RAG, so similarity thresholds calibrated once hold everywhere. Raise if embeddings are disabled.
def embed_documents(texts: List[str]) -> List[List[float]]:
    if _embed_model is None:
        raise RuntimeError("embeddings unavailable")
    return _embed_documents(texts)


def embed_query(text: str) -> List[List[float]]:
    if _embed_model is None:
        raise RuntimeError("embeddings unavailable")
    return _embed_query(text)


# --- Background embedding ---------------------------------------------------
# Embedding a 300M model on a no-AVX2 CPU is hundreds of ms, so it must NOT run
# inline in the chat request path. enqueue_embedding() hands off; this worker drains.
embed_queue: "queue.Queue" = queue.Queue()


def enqueue_embedding(msg_id, content: str, metadata: dict):
    if vectors_available():
        embed_queue.put((msg_id, content, metadata))


# Longest we will hold an embedding back waiting for the LLM to finish. A cap rather than an
# unbounded wait because is_busy() is a counter: if one request ever failed to decrement it, an
# uncapped wait would stop the vector store being written to again, silently and permanently.
# Exceeding it degrades to the old behaviour (embed anyway), which is worse for latency but never
# loses a memory.
EMBED_DEFER_MAX_S = 180.0
_EMBED_DEFER_POLL_S = 0.5


def _wait_for_llm_idle(max_wait: float = EMBED_DEFER_MAX_S) -> float:
    """Block while a generation is in flight. Returns how long we waited.

    Embedding a 300M model on two no-AVX2 cores is hundreds of ms of pure CPU, and it was running
    the instant a message was stored — which is exactly when the model is generating the reply to
    that message. The two were competing for the same cores at the worst possible moment.

    Deferring costs nothing in recall: anything not yet embedded is by definition recent, and
    recent turns are already in the verbatim window. RAG only has to cover what has aged out.
    """
    waited = 0.0
    while is_busy() and waited < max_wait:
        time.sleep(_EMBED_DEFER_POLL_S)
        waited += _EMBED_DEFER_POLL_S
    return waited


def _embedding_worker():
    while True:
        item = embed_queue.get()
        try:
            if item is None:
                return
            msg_id, content, metadata = item
            if vectors_available():
                waited = _wait_for_llm_idle()
                if waited >= EMBED_DEFER_MAX_S:
                    logger.warning("Embedding deferred %.0fs and the LLM still reports busy; "
                                   "embedding anyway so recall is not lost", waited)
                memory_collection.add(
                    documents=[content], embeddings=_embed_documents([content]),
                    metadatas=[metadata], ids=[str(msg_id)],
                )
        except Exception as e:
            logger.error("Embedding worker error: %s", e)
        finally:
            embed_queue.task_done()


def start_embedding_worker():
    t = threading.Thread(target=_embedding_worker, daemon=True, name="embedding-worker")
    t.start()
    return t


def delete_vectors(ids: List[str]):
    """Best-effort removal of vectors by id (batched for large deletes)."""
    if not (memory_collection and ids):
        return
    try:
        for i in range(0, len(ids), 500):
            memory_collection.delete(ids=ids[i:i + 500])
    except Exception as e:
        logger.error("ChromaDB cleanup error: %s", e)


def delete_vectors_for_users(user_ids: List[int]) -> None:
    """Remove every vector belonging to these users, by metadata rather than by id.

    delete_vectors(ids) is precise but only covers rows that still existed when the id list was
    taken; an embedding still sitting in the worker queue lands in Chroma *after* the purge and
    would survive it. This sweeps by user_id so a purged account leaves nothing recallable — which
    is what makes a demo reset actually a reset, and what stops a recycled id from serving the
    previous holder's memories.
    """
    if not (memory_collection and user_ids):
        return
    try:
        memory_collection.delete(where={"user_id": {"$in": [int(u) for u in user_ids]}})
    except Exception as e:
        logger.error("ChromaDB per-user cleanup error: %s", e)


# --- Request-activity / in-flight tracking ----------------------------------
# The fact-extraction worker shares the single LLM slot and 2 CPU cores, so it must
# NOT run while a (possibly multi-minute) generation is active. Idle time alone isn't
# enough because one long stream can exceed the threshold.
_last_activity_time = time.time()
_inflight_lock = threading.Lock()
_inflight_requests = 0
_memory_worker_running = False


def update_activity():
    global _last_activity_time
    _last_activity_time = time.time()


class Inflight:
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
        update_activity()  # reset idle clock when the request truly finishes
        return False


def is_busy() -> bool:
    with _inflight_lock:
        return _inflight_requests > 0


# --- Knowledge CRUD ---------------------------------------------------------
def get_user_knowledge(user_id: int) -> str:
    """Fetch all stored facts for this user, formatted for system-prompt injection."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT category, content FROM user_knowledge WHERE user_id = ? ORDER BY category, updated_at DESC",
            (user_id,)
        ).fetchall()
        if not rows:
            return ""
        by_cat: Dict[str, List[str]] = {}
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
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, category, content, source, created_at, updated_at FROM user_knowledge WHERE user_id = ? ORDER BY category, updated_at DESC",
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---- Presence (who the cameras have recognized recently) ------------------------------------------
def get_present_people(household_id: int, ttl_s: int = 180) -> List[str]:
    """Recognized people seen by one household's cameras within the last ttl_s seconds (deduped,
    most-recent first). Derived from face_seen vision events; 'unknown' faces are ignored.

    household_id is required: the result is injected into the prompt as "[Seen by cameras: …]", so
    an unscoped query would tell every user of the deployment who is currently inside someone
    else's home, by name, in real time.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT data FROM vision_events WHERE household_id = ? AND type='face_seen' "
            "AND created_at > datetime('now', ?) ORDER BY id DESC",
            (household_id, f"-{int(ttl_s)} seconds")).fetchall()
        seen, names = set(), []
        for r in rows:
            try:
                nm = (json.loads(r["data"]) or {}).get("name")
            except (ValueError, TypeError):
                nm = None
            if nm and nm != "unknown" and nm not in seen:
                seen.add(nm)
                names.append(nm)
        return names
    finally:
        conn.close()


# ---- Household knowledge (shared by every user IN ONE HOUSEHOLD; admin-curated) -------------------
# Every function here takes household_id and there is no unscoped variant on purpose: this table
# holds the home address, family names and room layout, and it is injected verbatim into the system
# prompt. An unscoped read would put one household's private facts into another's conversation.
def get_global_knowledge(household_id: int) -> str:
    """This household's facts, formatted for system-prompt injection."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT category, content FROM global_knowledge WHERE household_id = ? "
            "ORDER BY category, updated_at DESC", (household_id,)).fetchall()
        if not rows:
            return ""
        by_cat: Dict[str, List[str]] = {}
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


def get_global_knowledge_list(household_id: int) -> List[Dict[str, Any]]:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, category, content, source, created_at, updated_at FROM global_knowledge "
            "WHERE household_id = ? ORDER BY category, updated_at DESC", (household_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def store_global_fact(household_id: int, category: str, content: str, source: str = "manual") -> int:
    """Add a household fact (admin-curated). Exact duplicates within a category are coalesced."""
    conn = get_db()
    try:
        dup = conn.execute(
            "SELECT id FROM global_knowledge WHERE household_id = ? AND category = ? AND content = ?",
            (household_id, category, content)).fetchone()
        if dup:
            return dup["id"]
        cur = conn.execute(
            "INSERT INTO global_knowledge (household_id, category, content, source) VALUES (?, ?, ?, ?)",
            (household_id, category, content, source))
        conn.commit()
        logger.info("Memory Core: stored household fact #%d in [%s] for household %d: %s",
                    cur.lastrowid, category, household_id, content[:80])
        return cur.lastrowid
    finally:
        conn.close()


# The fact_id in update/delete comes straight from a client, so the household_id in the WHERE is
# the authorization check, not an optimization: without it any admin could edit or delete another
# household's facts by guessing an id (IDOR). rowcount==0 then reads as "not yours / not found",
# which is also the right thing to tell the caller.
def update_global_fact(household_id: int, fact_id: int, content: str, category: str = None) -> bool:
    conn = get_db()
    try:
        if category:
            cur = conn.execute("UPDATE global_knowledge SET content = ?, category = ?, "
                               "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND household_id = ?",
                               (content, category, fact_id, household_id))
        else:
            cur = conn.execute("UPDATE global_knowledge SET content = ?, updated_at = CURRENT_TIMESTAMP "
                               "WHERE id = ? AND household_id = ?", (content, fact_id, household_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_global_fact(household_id: int, fact_id: int) -> bool:
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM global_knowledge WHERE id = ? AND household_id = ?",
                           (fact_id, household_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _find_duplicate_fact(content: str, existing_rows: List[Any], use_embeddings: bool = True) -> Optional[int]:
    """Return the id of an existing fact that's a restatement of `content`, else None.

    use_embeddings=False forces the cheap word-overlap path — used on the request
    thread (POST /knowledge) so we never run the 300M model inline (it would burn a
    threadpool worker and contend with the LLM for CPU). The background fact worker
    keeps the embedding-based semantic dedup.
    """
    if not existing_rows:
        return None
    if use_embeddings and _embed_model is not None:
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


def store_fact(user_id: int, category: str, content: str, source: str = "auto",
               use_embeddings: bool = True) -> int:
    """Store a fact, updating an existing one if this is a semantic restatement of it.

    use_embeddings=False (request path) skips inline embedding for dedup; see _find_duplicate_fact.
    """
    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT id, content FROM user_knowledge WHERE user_id = ? AND category = ?",
            (user_id, category)
        ).fetchall()
        dup_id = _find_duplicate_fact(content, existing, use_embeddings=use_embeddings)
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
    """Delete the fact; returns False if no such fact is owned by user_id (caller should 404)."""
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM user_knowledge WHERE id = ? AND user_id = ?", (fact_id, user_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def update_fact(fact_id: int, user_id: int, content: str, category: str = None) -> bool:
    """Update the fact; returns False if no such fact is owned by user_id (caller should 404)."""
    conn = get_db()
    try:
        if category:
            cur = conn.execute(
                "UPDATE user_knowledge SET content = ?, category = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
                (content, category, fact_id, user_id)
            )
        else:
            cur = conn.execute(
                "UPDATE user_knowledge SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
                (content, fact_id, user_id)
            )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# --- Long-term recall (RAG) -------------------------------------------------
def retrieve_long_term_memory(user_id: int, current_session_id: str, user_text: str,
                              recent_context_ids: Optional[set] = None) -> str:
    """Recall the user's own past statements across all their sessions, minus what's
    already in the recent context window."""
    if not vectors_available():
        return ""
    try:
        # Restrict to speaker='user': assistant replies are verbose and crowd out real facts.
        results = memory_collection.query(
            query_embeddings=_embed_query(user_text),
            n_results=RAG_MAX_RESULTS,
            include=["documents", "metadatas", "distances"],
            where={"$and": [{"user_id": int(user_id)}, {"speaker": "user"}]},
        )
        if not results["documents"] or not results["documents"][0]:
            return ""

        docs = results["documents"][0]
        metas = results["metadatas"][0]
        dists = results["distances"][0]
        ids = results["ids"][0] if results.get("ids") else [None] * len(docs)

        memory_blocks = []
        seen_content = set()
        for i, (doc, meta, dist) in enumerate(zip(docs, metas, dists)):
            if dist > RAG_DISTANCE_THRESHOLD:
                continue
            # Index by position (ids[i]) — NOT docs.index(doc), which mis-maps duplicates.
            msg_id = ids[i]
            if recent_context_ids and msg_id and msg_id in recent_context_ids:
                continue
            content_key = doc[:100].strip().lower()
            if content_key in seen_content:
                continue
            seen_content.add(content_key)
            session_label = "(current)" if meta.get("session_id") == current_session_id else "(past)"
            memory_blocks.append(f"User {session_label}: {doc}")

        if memory_blocks:
            logger.info("RAG: Retrieved %d relevant memories (of %d candidates)",
                        len(memory_blocks), len(docs))
        return "\n".join(memory_blocks)
    except Exception as e:
        logger.error("Vector DB Search Error: %s", e)
        return ""


# --- Idle-time fact extraction ----------------------------------------------
def get_unprocessed_messages(batch_size: int = 20) -> List[Dict]:
    """User messages not yet processed for fact extraction (only from real, owned sessions)."""
    conn = get_db()
    try:
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
        return []  # column might not exist yet
    finally:
        conn.close()


def _mark_messages_processed(msg_ids: List[int]):
    """Mark exactly the given user messages as fact-extracted (not whole sessions)."""
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


# How many times each message has been through a failed extraction. In memory rather than a schema
# column: the bound only has to stop a poison message looping within one process lifetime, and a
# restart retrying it a few more times is harmless.
_extract_attempts: Dict[int, int] = defaultdict(int)


def _too_many_attempts(msg_id: int) -> bool:
    """Count a failed pass and report whether this message should be written off."""
    _extract_attempts[msg_id] += 1
    return _extract_attempts[msg_id] >= FACT_EXTRACTION_MAX_ATTEMPTS


# Statements ABOUT THE CONVERSATION rather than about the user. The model produced three of these
# ("The user has not stated any rules about downloading models or binaries") in a batch where it had
# already, correctly, extracted the opposite — so they are not merely noise, they contradict real
# facts sitting beside them in the same profile.
#
# Deliberately narrow: it matches meta-statements about what went unsaid, NOT negative preferences.
# "The user refuses to use third-party mirrors" and "The user avoids Codespaces" are real facts and
# must survive.
_ABSENCE_FACT = re.compile(
    r"\b(?:"
    r"(?:has|have|had|did|was|were|is|are)\s+not\s+(?:yet\s+)?"
    r"(?:state[ds]?|said|specif(?:y|ied)|mention(?:ed)?|provide[ds]?|share[ds]?|given|told)"
    r"|no(?:t)?\s+(?:information|details?|specifics?)\s+(?:was|were|is|are)?\s*(?:given|provided|stated)"
    r"|(?:remains?|is|was)\s+(?:unclear|unspecified|unknown|unstated)"
    r"|was\s+not\s+stated"
    r")\b", re.I)


def _is_absence(content: str) -> bool:
    """True for 'the user did not say X' — an absence, which is not a fact about anyone."""
    return bool(_ABSENCE_FACT.search(content or ""))


def _parse_facts(response_text: str) -> tuple:
    """(facts, complete) from the extractor's reply.

    `complete` is False when the reply was cut short — the model was still writing when it hit the
    token limit. That case used to raise JSONDecodeError and throw away everything, including the
    facts it had already finished writing, and the messages were then marked processed so they
    could never be retried. Two real facts about the operator were lost that way before this was
    noticed.

    Complete objects are salvaged by scanning for balanced braces (respecting strings and escapes,
    so a "}" inside a fact's text does not end it early). Anything half-written is dropped.
    """
    text = response_text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if "```" in text:
            text = text.rsplit("```", 1)[0]
        text = text.strip()
    try:
        parsed = json.loads(text)
        return (parsed if isinstance(parsed, list) else []), True
    except json.JSONDecodeError:
        pass

    facts, depth, start, in_str, esc = [], 0, None, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    facts.append(json.loads(text[start:i + 1]))
                except json.JSONDecodeError:
                    pass
                start = None
    return facts, False


def _fit_batch(user_id: int, msgs: List[Dict]) -> tuple:
    """(messages that fit, their joined text) — as many as leave room for a reply.

    The window is the hard constraint: llama.cpp does not error when a prompt plus its requested
    completion exceed it, it silently drops tokens. So the batch is trimmed here instead, and
    whatever does not fit stays unmarked for the next pass rather than being sent and lost.
    """
    overhead = estimate_message_tokens({"role": "system", "content": FACT_EXTRACTION_PROMPT})
    room = MAX_CONTEXT_TOKENS - overhead - PROMPT_SAFETY_MARGIN - FACT_EXTRACTION_MIN_TOKENS
    kept, lines, used = [], [], 0
    for m in msgs:
        line = f"User said: {m['content']}"
        cost = estimate_tokens(line)
        if kept and used + cost > room:
            break                    # room for at least one, then stop before overflowing
        if not kept and cost > room:
            # A single message too large to reason about at all. Truncate it rather than loop on
            # it forever; the head of a statement carries the facts far more often than the tail.
            line = truncate_to_tokens(line, max(64, room))
            cost = estimate_tokens(line)
            logger.warning("Memory Core: message %s too long for one extraction pass; truncated",
                           m.get("id"))
        kept.append(m); lines.append(line); used += cost
    if len(kept) < len(msgs):
        logger.info("Memory Core: extracting %d of %d queued messages for user %d (window limit)",
                    len(kept), len(msgs), user_id)
    return kept, "\n".join(lines)


def extract_facts_batch(messages: List[Dict]):
    """Process a batch of unprocessed user messages through the LLM for fact extraction."""
    if not messages:
        return
    # Group by real owning user; skip any without a user_id rather than misattributing to user 1.
    by_user: Dict[int, List[Dict]] = {}
    for m in messages:
        uid = m.get("user_id")
        if not uid:
            continue
        by_user.setdefault(uid, []).append(m)

    truncated_users: set = set()
    failed_users: set = set()
    for user_id, all_msgs in by_user.items():
        # Take only as many messages as leave room for a usable reply. A batch is capped by count,
        # but not by SIZE — and an admin may type 10,000 characters, so six of those would exceed
        # the window on their own. Asking for output that cannot fit is how the truncation happened
        # in the first place; anything left over simply stays queued for the next pass.
        user_msgs, exchange_text = _fit_batch(user_id, all_msgs)
        if not user_msgs:
            continue
        try:
            llm_messages = [
                {"role": "system", "content": FACT_EXTRACTION_PROMPT},
                {"role": "user", "content": exchange_text},
            ]
            # Derive the output budget from what is actually left in the window rather than
            # guessing a number. A fixed 512 was the whole cause of the silent fact loss: it was
            # ample for one message and far too small for a dozen, and nothing in the code could
            # tell the difference. This asks the same question the chat path asks — how much room
            # is there after the prompt — so the answer scales with the batch instead of being
            # right only for the batch size someone had in mind.
            prompt_tokens = sum(estimate_message_tokens(m) for m in llm_messages)
            budget = MAX_CONTEXT_TOKENS - prompt_tokens - PROMPT_SAFETY_MARGIN
            n_predict = min(FACT_EXTRACTION_TOKENS, budget)      # _fit_batch guarantees the floor
            result = request_llm(llm_messages, temperature=0.1, n_predict=n_predict)
            facts, complete = _parse_facts(llm_content(result))
            if not complete:
                # Salvaged what it finished; the rest of this batch has not been seen, so it must
                # stay eligible for another pass rather than being silently written off.
                truncated_users.add(int(user_id))
                logger.warning("Memory Core: extractor output was truncated for user %d; "
                               "kept %d complete fact(s) and will retry the batch", user_id, len(facts))
            for fact in facts:
                # The model is not consistent about shape: sometimes [{"category","content"}],
                # sometimes a plain array of sentences. Dropping the latter discarded whole
                # batches of correct facts without a word in the log — the same silent loss as
                # the truncation, from a different direction. A sentence with no category is
                # still a fact; file it under "other" rather than throw it away.
                if isinstance(fact, str):
                    fact = {"category": "other", "content": fact}
                if not isinstance(fact, dict):
                    logger.debug("Memory Core: ignoring unusable fact entry %r", fact)
                    continue
                category = (fact.get("category") or "other").lower().strip()
                content = (fact.get("content") or "").strip()
                if not content or len(content) < 5:
                    continue
                if _is_absence(content):
                    logger.debug("Memory Core: dropping absence-shaped fact %r", content)
                    continue
                if category not in VALID_FACT_CATEGORIES:
                    category = "other"
                store_fact(int(user_id), category, content, source="auto")
            if facts:
                logger.info("Memory Core: Extracted %d facts from %d messages for user %d",
                            len(facts), len(user_msgs), user_id)
        except Exception as e:
            logger.error("Memory Core: Extraction error: %s", e)
            failed_users.add(int(user_id))

    # Mark only what was actually processed. Marking unconditionally meant one bad reply discarded
    # those messages permanently — the failure mode that hid this bug for weeks, because the counter
    # of "unextracted" messages went to zero either way.
    retry_users = truncated_users | failed_users
    done = [m["id"] for m in messages
            if int(m.get("user_id") or 0) not in retry_users or _too_many_attempts(m["id"])]
    _mark_messages_processed(done)
    held = len(messages) - len(done)
    if held:
        logger.info("Memory Core: holding %d message(s) for a later extraction pass", held)

    # Put the conversation's prefix back in the KV cache. Extraction has just spent the single
    # llama-server slot on a prompt that shares nothing with any chat. llama.cpp will often restore
    # the chat's prefix from a context checkpoint by itself, making this a no-op; it is here for
    # when it cannot, because that case costs the user a full re-evaluation of the system message.
    # Safe to do here: this runs only after the system has been idle, so the CPU is free and the
    # work is exactly what the user would otherwise have waited for.
    if WARM_CACHE_AFTER_EXTRACTION:
        import chat as _chat        # local import: chat imports memory, so a module-level one cycles
        warm_prefix(_chat.last_system_prefix())


def _memory_worker():
    """Background thread: when idle and not busy, extract facts from new messages."""
    global _memory_worker_running
    _memory_worker_running = True
    logger.info("Memory Core: Background worker started (idle threshold=%ds, check interval=%ds)",
                IDLE_THRESHOLD_SECONDS, IDLE_CHECK_INTERVAL)
    while _memory_worker_running:
        try:
            time.sleep(IDLE_CHECK_INTERVAL)
            if is_busy():
                continue
            idle_duration = time.time() - _last_activity_time
            if idle_duration < IDLE_THRESHOLD_SECONDS:
                continue
            unprocessed = get_unprocessed_messages(batch_size=FACT_EXTRACTION_BATCH)
            if not unprocessed:
                continue
            logger.info("Memory Core: System idle for %.0fs, processing %d unextracted messages",
                        idle_duration, len(unprocessed))
            extract_facts_batch(unprocessed)
        except Exception as e:
            logger.error("Memory Core: Worker error: %s", e)
            time.sleep(60)  # back off on errors


def start_memory_worker():
    t = threading.Thread(target=_memory_worker, daemon=True, name="memory-core")
    t.start()
    return t
