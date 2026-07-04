"""Export the embedding model (the FULL sentence-transformers pipeline) to ONNX, then verify it.

Why the full pipeline matters: embeddinggemma is transformer -> 1_Pooling -> 2_Dense -> 3_Dense.
Exporting only the transformer (what generic exporters do) silently produces WRONG vectors —
RAG would degrade with no error. This script traces the whole module chain into one graph and
then proves equivalence: cosine(torch, onnx) must be ≥ 0.999 across batch sizes and lengths.

One-time dev tool (torch is needed only to export — the runtime uses onnxruntime alone):

(torch/sentence-transformers are NOT project deps — pull them ephemerally, CPU wheels only):

    uv run --index https://download.pytorch.org/whl/cpu \
           --with sentence-transformers --with onnx --with onnxscript \
           python src/scripts/export_embed_onnx.py                # fp32 export + verify (--int8 for quant)

Output (gitignored): models/embed_onnx/{model.onnx[, model.int8.onnx], tokenizer.json, meta.json}
The verify step re-tokenizes with the standalone `tokenizers` library (no torch) — the exact
tokenization path the production OnnxEmbedder uses, so this validates that too.
"""
import argparse
import gc
import json
import os
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src" / "orchestrator"))
os.environ.setdefault("JARVIS_HOME", str(REPO))

from config import EMBED_DOC_PREFIX, EMBED_MODEL_NAME, EMBED_QUERY_PREFIX  # noqa: E402

OUT_DIR = REPO / "models" / "embed_onnx"
OPSET = 17

# Verification set: both prefixes, short/long, so shape-baking or masking bugs can't hide.
SAMPLES = (
    [EMBED_DOC_PREFIX + t for t in [
        "The user's name is Ravi.",
        "The user prefers Rust for systems programming and is learning inference engines.",
        "Jarvis runs on a 2011 Sandy Bridge laptop inside a Proxmox LXC with 8 GB of RAM, "
        "serving a llama.cpp Qwen model on port 8081 and a FastAPI orchestrator on port 5000, "
        "with Piper for speech synthesis and whisper.cpp for speech recognition on the voice path.",
    ]]
    + [EMBED_QUERY_PREFIX + t for t in [
        "what is my name",
        "which programming language do I like",
        "tell me about the server hardware and what services run on it and on which ports",
    ]]
)


def log(msg: str) -> None:
    print(f"[export] {msg}", flush=True)


def l2norm(a: np.ndarray) -> np.ndarray:
    return a / np.linalg.norm(a, axis=-1, keepdims=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--int8", action="store_true", help="also produce a dynamically-quantized int8 model")
    ap.add_argument("--seq", type=int, default=64, help="dummy sequence length used for tracing")
    ap.add_argument("--skip-export", action="store_true",
                    help="reuse an existing models/embed_onnx/model.onnx; only (re)verify / quantize")
    args = ap.parse_args()

    os.environ.setdefault("HF_HUB_OFFLINE", "1")   # model must come from the local cache
    import torch
    from sentence_transformers import SentenceTransformer

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    log(f"loading {EMBED_MODEL_NAME} (torch, from local HF cache)…")
    st = SentenceTransformer(EMBED_MODEL_NAME, trust_remote_code=False, device="cpu")
    st.eval()
    dim = st.get_sentence_embedding_dimension()
    max_seq = int(st.max_seq_length)
    log(f"pipeline: {[type(m).__name__ for m in st]}  dim={dim}  max_seq={max_seq}")

    # 1) Reference vectors from torch (UN-normalized — we normalize in numpy on both sides).
    log(f"reference vectors for {len(SAMPLES)} samples…")
    ref = st.encode(SAMPLES, normalize_embeddings=False, convert_to_numpy=True, batch_size=3)

    # 2) Save the tokenizer for standalone (torch-free) runtime use + the sidecar metadata.
    st.tokenizer.save_pretrained(OUT_DIR)
    pad_id = st.tokenizer.pad_token_id or 0
    (OUT_DIR / "meta.json").write_text(json.dumps({
        "model": EMBED_MODEL_NAME, "dim": dim, "max_seq_length": max_seq,
        "pad_token_id": pad_id, "opset": OPSET,
        "note": "pipeline ends with a Normalize module, so 'sentence_embedding' is ALREADY "
                "L2-normalized; normalizing again at runtime is harmless (idempotent)",
    }, indent=2))

    # 3) Trace the FULL module chain (dict features flow through; tensors in/out at the wrapper).
    class FullPipeline(torch.nn.Module):
        def __init__(self, st_model):
            super().__init__()
            self.mods = torch.nn.ModuleList(list(st_model.children()))

        def forward(self, input_ids, attention_mask):
            feats = {"input_ids": input_ids, "attention_mask": attention_mask}
            for m in self.mods:
                feats = m(feats)
            return feats["sentence_embedding"]

    onnx_path = OUT_DIR / "model.onnx"
    if args.skip_export and onnx_path.exists():
        log("skip-export: reusing existing model.onnx")
        del st
    else:
        wrapper = FullPipeline(st)
        enc = st.tokenizer(SAMPLES[:2], padding=True, truncation=True, max_length=args.seq, return_tensors="pt")
        log(f"exporting to {onnx_path} (opset {OPSET})…")
        with torch.no_grad():
            torch.onnx.export(
                wrapper, (enc["input_ids"], enc["attention_mask"]), str(onnx_path),
                input_names=["input_ids", "attention_mask"], output_names=["sentence_embedding"],
                dynamic_axes={"input_ids": {0: "batch", 1: "seq"},
                              "attention_mask": {0: "batch", 1: "seq"},
                              "sentence_embedding": {0: "batch"}},
                opset_version=OPSET,
            )
        size_mb = onnx_path.stat().st_size / 1e6
        # External-data files appear when the graph exceeds 2 GB protobuf limits; count them in.
        extra = sum(p.stat().st_size for p in OUT_DIR.glob("model.onnx.*")) / 1e6
        log(f"exported: {size_mb + extra:.0f} MB")
        del st, wrapper, enc

    # Free torch before verification so peak RAM stays sane on the 8 GB box.
    gc.collect()

    # 4) Verify with the PRODUCTION runtime path: tokenizers + onnxruntime, no torch objects.
    import onnxruntime as ort
    from tokenizers import Tokenizer

    def verify(path: Path, label: str) -> float:
        tok = Tokenizer.from_file(str(OUT_DIR / "tokenizer.json"))
        tok.enable_truncation(max_length=max_seq)
        tok.enable_padding(pad_id=pad_id, pad_token="<pad>")
        sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        worst = 1.0
        # batch of all 6, then singles → exercises dynamic batch AND dynamic seq axes
        for chunk in [SAMPLES] + [[s] for s in SAMPLES]:
            encs = tok.encode_batch(chunk)
            ids = np.array([e.ids for e in encs], dtype=np.int64)
            mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
            out = sess.run(["sentence_embedding"], {"input_ids": ids, "attention_mask": mask})[0]
            for text, vec in zip(chunk, out):
                r = ref[SAMPLES.index(text)]
                cos = float(np.dot(l2norm(vec), l2norm(r)))
                worst = min(worst, cos)
        log(f"{label}: worst-case cosine vs torch = {worst:.6f}")
        return worst

    worst_fp32 = verify(onnx_path, "fp32")
    ok = worst_fp32 >= 0.999

    if args.int8:
        # Never let a quantization failure mask the fp32 verdict — int8 is a bonus, fp32 is the ship gate.
        try:
            from onnxruntime.quantization import QuantType, quantize_dynamic
            from onnxruntime.quantization.shape_inference import quant_pre_process
            int8_path = OUT_DIR / "model.int8.onnx"
            prep_path = OUT_DIR / "model.prep.onnx"
            # onnx's strict shape inference chokes on the exported graph (768 vs 3072 dim annotation
            # from the two Dense heads); the symbolic-shape-inference preprocessor handles it.
            log("quantizing: preprocessing (symbolic shape inference)…")
            quant_pre_process(str(onnx_path), str(prep_path),
                              skip_optimization=True, skip_onnx_shape=True, skip_symbolic_shape=False,
                              use_external_data_format=True)
            log("quantizing (dynamic int8)…")
            quantize_dynamic(str(prep_path), str(int8_path), weight_type=QuantType.QInt8,
                             use_external_data_format=False)
            for p in OUT_DIR.glob("model.prep.onnx*"):
                p.unlink()
            log(f"int8 size: {int8_path.stat().st_size / 1e6:.0f} MB")
            worst_int8 = verify(int8_path, "int8")
            log(f"int8 verdict: {'usable' if worst_int8 >= 0.99 else 'DEGRADED — prefer fp32'} "
                f"(re-index required if adopted: vectors shift)")
        except Exception as e:
            log(f"int8 quantization FAILED ({type(e).__name__}: {e}) — fp32 verdict below still stands")

    log("PASS — fp32 ONNX is numerically equivalent; safe to serve."
        if ok else "FAIL — fp32 cosine < 0.999; do NOT use this export.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
