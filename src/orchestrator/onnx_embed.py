"""Torch-free embedding runtime: ONNX Runtime + tokenizers (no sentence-transformers, no torch).

Loads the full-pipeline graph produced by src/scripts/export_embed_onnx.py (transformer + pooling +
both Dense heads + Normalize, in ONE graph — exporting only the transformer yields wrong vectors)
and mimics the minimal SentenceTransformer surface that memory.py uses:

    encode(texts, normalize_embeddings=True, convert_to_numpy=True) -> np.ndarray
    get_embedding_dimension() -> int

Only stdlib + numpy at import time; onnxruntime/tokenizers import inside __init__ (both are already
project dependencies via chromadb/transformers). No app-module imports — safe anywhere in the graph.
"""
import json
import os
from pathlib import Path
from typing import Any, List

import numpy as np


class OnnxEmbedder:
    """Drop-in (minimal) replacement for the SentenceTransformer instance in memory.py."""

    def __init__(self, model_dir: Path):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model_dir = Path(model_dir)
        meta = json.loads((model_dir / "meta.json").read_text())
        self.model_name: str = meta.get("model", "unknown")
        self._dim: int = int(meta["dim"])

        # fp32 by default. int8 (model.int8.onnx) shifts vectors slightly — switching REQUIRES a
        # re-index (reembed_memory.py), so it's opt-in via env, never auto-picked.
        fname = os.environ.get("JARVIS_EMBED_ONNX_FILE", "model.onnx")
        model_path = model_dir / fname
        if not model_path.exists():
            raise FileNotFoundError(f"ONNX model not found: {model_path}")

        tok = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        tok.enable_truncation(max_length=int(meta["max_seq_length"]))
        tok.enable_padding(pad_id=int(meta.get("pad_token_id", 0)), pad_token="<pad>")
        self._tok = tok

        so = ort.SessionOptions()
        # Leave a core for llama-server / the event loop on the 2-4 core boxes this runs on.
        so.intra_op_num_threads = max(1, (os.cpu_count() or 2) - 1)
        self._sess = ort.InferenceSession(str(model_path), sess_options=so,
                                          providers=["CPUExecutionProvider"])
        self.runtime = f"onnx ({fname})"

    def get_embedding_dimension(self) -> int:
        return self._dim

    # older sentence-transformers name, kept for callers that probe either
    get_sentence_embedding_dimension = get_embedding_dimension

    def encode(self, texts: List[str], normalize_embeddings: bool = True,
               convert_to_numpy: bool = True, **_: Any) -> np.ndarray:
        encs = self._tok.encode_batch(list(texts))
        ids = np.asarray([e.ids for e in encs], dtype=np.int64)
        mask = np.asarray([e.attention_mask for e in encs], dtype=np.int64)
        out = self._sess.run(["sentence_embedding"], {"input_ids": ids, "attention_mask": mask})[0]
        if normalize_embeddings:
            # The exported graph ends in a Normalize module, so this is idempotent — kept for
            # exactness with the SentenceTransformer call signature and as a safety net.
            out = out / np.linalg.norm(out, axis=-1, keepdims=True)
        return out
