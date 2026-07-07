#!/usr/bin/env python3
"""One-time migration: rebuild the ChromaDB vector store for the memory rework.

Why: the old "jarvis_memory" collection used L2 space and embedded raw text without
embeddinggemma's required prompt prefixes. The orchestrator now uses a cosine collection
("jarvis_memory_cos") and document/query prefixes. This script re-embeds every stored
message into the new collection so existing history is searchable again.

Run (after deploying the new code, with the orchestrator stopped to avoid races):
    uv run python src/scripts/reembed_memory.py

Torch-free: embeds via the ONNX bundle (models/embed_onnx — download_models.sh fetches it,
or export your own with export_embed_onnx.py for a custom model).
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

import chromadb

# Portable paths (any checkout, any user): JARVIS_CONFIG > JARVIS_HOME > repo-relative.
_REPO = Path(os.environ.get("JARVIS_HOME") or Path(__file__).resolve().parents[2])
CONFIG = json.loads(Path(os.environ.get("JARVIS_CONFIG") or _REPO / "config" / "jarvis.json").read_text())
DB_PATH = CONFIG["memory"]["db_path"]
CHROMA_PATH = CONFIG["memory"].get("chroma_db_path", "memory/chroma_db")
DB_PATH = DB_PATH if Path(DB_PATH).is_absolute() else str(_REPO / DB_PATH)
CHROMA_PATH = CHROMA_PATH if Path(CHROMA_PATH).is_absolute() else str(_REPO / CHROMA_PATH)

EMBED_DOC_PREFIX = "title: none | text: "
COLLECTION = "jarvis_memory_cos"
EMBED_ONNX_DIR = Path(os.environ.get("EMBED_ONNX_DIR") or _REPO / "models" / "embed_onnx")

sys.path.insert(0, str(_REPO / "src" / "orchestrator"))
from onnx_embed import OnnxEmbedder  # noqa: E402  (torch-free runtime)


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT ch.id, ch.session_id, ch.speaker, ch.content, cs.user_id
        FROM conversation_history ch
        JOIN chat_sessions cs ON ch.session_id = cs.id
        ORDER BY ch.id ASC
        """
    ).fetchall()
    conn.close()
    print(f"Found {len(rows)} messages to re-embed.")

    client = chromadb.PersistentClient(path=CHROMA_PATH)
    # Start clean so re-runs are idempotent.
    try:
        client.delete_collection(COLLECTION)
        print(f"Dropped existing '{COLLECTION}'.")
    except Exception:
        pass
    col = client.get_or_create_collection(name=COLLECTION, metadata={"hnsw:space": "cosine"})

    if not rows:
        print("Nothing to embed. Done.")
        return

    model = OnnxEmbedder(EMBED_ONNX_DIR)  # onnxruntime + tokenizers — no torch, no remote code
    BATCH = 32
    for start in range(0, len(rows), BATCH):
        batch = rows[start:start + BATCH]
        docs = [r["content"] for r in batch]
        vecs = model.encode([EMBED_DOC_PREFIX + d for d in docs], normalize_embeddings=True)
        col.add(
            ids=[str(r["id"]) for r in batch],
            documents=docs,
            embeddings=[v.tolist() for v in vecs],
            metadatas=[
                {"session_id": r["session_id"], "speaker": r["speaker"], "user_id": int(r["user_id"])}
                for r in batch
            ],
        )
        print(f"  embedded {min(start + BATCH, len(rows))}/{len(rows)}")

    print(f"Done. '{COLLECTION}' now holds {col.count()} vectors.")


if __name__ == "__main__":
    main()
