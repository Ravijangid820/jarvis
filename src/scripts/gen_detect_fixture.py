#!/usr/bin/env python3
"""Generate YuNet decoding fixtures for the browser port.

    uv run --with opencv-python-headless --with numpy --with onnxruntime \
        python src/scripts/gen_detect_fixture.py

frontend/src/face-detect.js re-implements OpenCV's YuNet post-processing. Rather than ship the
full 8400-cell output (megabytes), the fixture stores the raw predictions for the cells that
actually fire, plus the box/landmarks cv2.FaceDetectorYN produced for the same frame. That pins
the decoding arithmetic — the part that is easy to get subtly wrong — against ground truth.
"""
import json
import pathlib

import cv2
import numpy as np
import onnxruntime as ort

REPO = pathlib.Path(__file__).resolve().parents[2]
MODEL = REPO / "camera" / "models" / "face_detection_yunet_2023mar.onnx"
SAMPLE = REPO / "frontend" / "test" / "fixtures" / "face_sample.jpg"
SIZE = 640
STRIDES = (8, 16, 32)


def main():
    if not SAMPLE.exists():
        raise SystemExit(f"{SAMPLE} not found — drop in any photo with one clear face (see the "
                         "module docstring; it is intentionally not committed)")
    img = cv2.imread(str(SAMPLE))
    h, w = img.shape[:2]
    scale = min(SIZE / w, SIZE / h)
    sw, sh = round(w * scale), round(h * scale)
    ox, oy = (SIZE - sw) // 2, (SIZE - sh) // 2
    canvas = np.zeros((SIZE, SIZE, 3), np.uint8)
    canvas[oy:oy + sh, ox:ox + sw] = cv2.resize(img, (sw, sh))

    det = cv2.FaceDetectorYN.create(str(MODEL), "", (SIZE, SIZE), 0.9, 0.3, 5000)
    _, truth = det.detect(canvas)
    assert truth is not None and len(truth), "no face detected in the fixture image"

    sess = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
    blob = canvas.astype(np.float32).transpose(2, 0, 1)[None]
    outs = {o.name: a for o, a in zip(sess.get_outputs(), sess.run(None, {"input": blob}))}

    cells = []
    for stride in STRIDES:
        cls = outs[f"cls_{stride}"][0, :, 0]
        obj = outs[f"obj_{stride}"][0, :, 0]
        score = np.sqrt(np.clip(cls, 0, 1) * np.clip(obj, 0, 1))
        for i in np.where(score >= 0.9)[0]:
            cells.append({
                "stride": stride, "index": int(i),
                "cls": float(cls[i]), "obj": float(obj[i]),
                "bbox": [float(v) for v in outs[f"bbox_{stride}"][0, i]],
                "kps": [float(v) for v in outs[f"kps_{stride}"][0, i]],
            })

    payload = {
        "_comment": ("Ground truth for frontend/src/face-detect.js. `cells` holds the raw YuNet "
                     "predictions that cleared the score threshold; `expected` is what "
                     "cv2.FaceDetectorYN reported for the same frame. Regenerate with "
                     "src/scripts/gen_detect_fixture.py."),
        "detector_size": SIZE,
        "score_threshold": 0.9,
        "cells": cells,
        "expected": {
            "box": [float(v) for v in truth[0][:4]],
            "landmarks": [[float(x), float(y)] for x, y in truth[0][4:14].reshape(5, 2)],
        },
        "letterbox": {"source_width": w, "source_height": h,
                      "scale": scale, "ox": ox, "oy": oy, "w": sw, "h": sh},
    }
    out = REPO / "frontend" / "test" / "fixtures" / "detect-fixture.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {out} ({len(cells)} firing cells)")


if __name__ == "__main__":
    main()
