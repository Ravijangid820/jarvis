"""Jarvis Memory Core: embeddings + vector store, the user knowledge base,
idle-time fact extraction, and request-activity tracking.

Depends on config, db, and llm — never on chat/main (keeps the import graph acyclic).
"""
import json
import queue
import threading
import time
from typing import Any, Dict, List, Optional

import chromadb
from sentence_transformers import SentenceTransformer

from config import (
    CHROMA_DB_PATH, EMBED_DOC_PREFIX, EMBED_MODEL_NAME, EMBED_QUERY_PREFIX,
    FACT_DEDUP_SIM, FACT_DEDUP_WORD, FACT_EXTRACTION_PROMPT, IDLE_CHECK_INTERVAL,
    IDLE_THRESHOLD_SECONDS, RAG_DISTANCE_THRESHOLD,
    RAG_MAX_RESULTS, VALID_FACT_CATEGORIES, logger,
)
from db import get_db
from llm import llm_content, request_llm

# --- Embeddings + vector store ----------------------------------------------
chroma_client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
try:
    _embed_model: Optional[SentenceTransformer] = SentenceTransformer(EMBED_MODEL_NAME)
    # Cosine space with normalized vectors (the "jarvis_memory_cos" collection).
    memory_collection = chroma_client.get_or_create_collection(
        name="jarvis_memory_cos", metadata={"hnsw:space": "cosine"}
    )
    # Surface the embedding dimension + collection size at startup. If EMBED_MODEL_NAME
    # ever changes to a different dimension, get_or_create returns the OLD collection and
    # the first add() would fail inside the worker — logging this makes that diagnosable.
    logger.info("Embeddings: %s (dim=%d), collection 'jarvis_memory_cos' has %d vectors",
                EMBED_MODEL_NAME, _embed_model.get_sentence_embedding_dimension(),
                memory_collection.count())
except Exception as e:
    logger.error("Failed to initialize ChromaDB / embedding model: %s", e)
    _embed_model = None
    memory_collection = None


def vectors_available() -> bool:
    return memory_collection is not None and _embed_model is not None


def _embed_documents(texts: List[str]) -> List[List[float]]:
    vecs = _embed_model.encode([EMBED_DOC_PREFIX + t for t in texts], normalize_embeddings=True)
    return [v.tolist() for v in vecs]


def _embed_query(text: str) -> List[List[float]]:
    vec = _embed_model.encode([EMBED_QUERY_PREFIX + text], normalize_embeddings=True)
    return [vec[0].tolist()]


# --- Background embedding ---------------------------------------------------
# Embedding a 300M model on a no-AVX2 CPU is hundreds of ms, so it must NOT run
# inline in the chat request path. enqueue_embedding() hands off; this worker drains.
embed_queue: "queue.Queue" = queue.Queue()


def enqueue_embedding(msg_id, content: str, metadata: dict):
    if vectors_available():
        embed_queue.put((msg_id, content, metadata))


def _embedding_worker():
    while True:
        item = embed_queue.get()
        try:
            if item is None:
                return
            msg_id, content, metadata = item
            if vectors_available():
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

    for user_id, user_msgs in by_user.items():
        exchange_text = "\n".join(f"User said: {m['content']}" for m in user_msgs)
        try:
            llm_messages = [
                {"role": "system", "content": FACT_EXTRACTION_PROMPT},
                {"role": "user", "content": exchange_text},
            ]
            result = request_llm(llm_messages, temperature=0.1, n_predict=512)
            response_text = llm_content(result).strip()
            if response_text.startswith("```"):
                response_text = response_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            facts = json.loads(response_text)
            if not isinstance(facts, list):
                facts = []
            for fact in facts:
                if not isinstance(fact, dict):
                    continue
                category = fact.get("category", "other").lower().strip()
                content = fact.get("content", "").strip()
                if not content or len(content) < 5:
                    continue
                if category not in VALID_FACT_CATEGORIES:
                    category = "other"
                store_fact(int(user_id), category, content, source="auto")
            if facts:
                logger.info("Memory Core: Extracted %d facts from %d messages for user %d",
                            len(facts), len(user_msgs), user_id)
        except json.JSONDecodeError:
            logger.warning("Memory Core: LLM returned non-JSON for fact extraction")
        except Exception as e:
            logger.error("Memory Core: Extraction error: %s", e)

    _mark_messages_processed([m["id"] for m in messages])


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
            unprocessed = get_unprocessed_messages(batch_size=20)
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
