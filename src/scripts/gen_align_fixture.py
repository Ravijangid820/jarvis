#!/usr/bin/env python3
"""Generate the face-alignment fixtures the browser port is tested against.

    uv run --with opencv-python-headless --with numpy python src/scripts/gen_align_fixture.py

Why this exists
---------------
The browser enrolls faces with onnxruntime-web, and SFace only produces comparable embeddings if
the face is ALIGNED onto its canonical 112x112 pose first. On the edge agent OpenCV does that
(FaceRecognizerSF::alignCrop); in the browser there is no OpenCV, so frontend/src/face-align.js
re-implements it. A nearly-right alignment is the dangerous case — embeddings that look fine but
sit slightly off, so recognition degrades quietly.

The correctness chain is therefore pinned in two places:

  JS  ==  this reference implementation   (frontend/test/face-align.test.mjs, fixtures below)
      ==  OpenCV's alignCrop              (tests/test_face_align.py, end-to-end on real pixels)

NOTE: alignCrop does NOT use estimateAffinePartial2D. That is a robust estimator (RANSAC/LMEDS)
which can discard input points as outliers, and on noisy landmarks it disagrees with the plain
least-squares fit by ~0.3%. SFace uses the least-squares similarity — Umeyama — over all five
points, which is what `similarity_transform` below implements and what the JS mirrors.

Supplying the sample image
--------------------------
`frontend/test/fixtures/face_sample.jpg` is NOT in the repository — any photo containing one clearly
visible face works, so there is no reason to commit someone's likeness (and no reason to reach for
the historical test images that ship with CV libraries; several are published without the subject's
consent). Drop one in yourself to regenerate the fixtures or run the OpenCV cross-check. Without it
the synthetic cases still generate and the cross-check skips.
"""
import json
import pathlib
import sys

import numpy as np

REPO = pathlib.Path(__file__).resolve().parents[2]

# SFace's reference landmarks inside the 112x112 aligned crop (the ArcFace canonical five).
DST = np.array([[38.2946, 51.6963],
                [73.5318, 51.5014],
                [56.0252, 71.7366],
                [41.5493, 92.3655],
                [70.7299, 92.2041]], dtype=np.float64)


def similarity_transform(src, dst):
    """Least-squares similarity (uniform scale + rotation + translation) — Umeyama 1991.

    The reference implementation. frontend/src/face-align.js is a direct port; keep them in step.
    """
    src, dst = np.asarray(src, float), np.asarray(dst, float)
    m_s, m_d = src.mean(0), dst.mean(0)
    sc, dc = src - m_s, dst - m_d
    cov = dc.T @ sc / len(src)
    U, D, Vt = np.linalg.svd(cov)
    S = np.eye(2)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0:
        S[1, 1] = -1
    R = U @ S @ Vt
    var = (sc ** 2).sum() / len(src)
    scale = (D * np.diag(S)).sum() / var if var > 1e-12 else 1.0
    t = m_d - scale * R @ m_s
    return np.hstack([scale * R, t.reshape(2, 1)])


def main():
    out_path = REPO / "frontend" / "test" / "fixtures" / "align-fixture.json"
    rng = np.random.default_rng(7)

    cases = []
    # A real detection, if the models + a sample image are available; otherwise synthetic only.
    try:
        import cv2
        sample = REPO / "camera" / "models" / "face_detection_yunet_2023mar.onnx"
        img_path = REPO / "frontend" / "test" / "fixtures" / "face_sample.jpg"
        if sample.exists() and img_path.exists():
            det = cv2.FaceDetectorYN.create(str(sample), "", (320, 320), 0.9, 0.3, 5000)
            img = cv2.imread(str(img_path))
            h, w = img.shape[:2]
            det.setInputSize((w, h))
            _, faces = det.detect(img)
            if faces is not None and len(faces):
                cases.append(("real-detection", faces[0][4:14].reshape(5, 2).astype(np.float64)))
    except ImportError:
        print("opencv not installed — synthetic cases only", file=sys.stderr)

    # Rotation / scale / translation across the whole similarity family, with a little landmark
    # noise so the fit is exercised rather than an exact inverse being recovered.
    for i, (ang, sc, tx, ty) in enumerate([(15, 1.0, 0, 0), (-25, 0.6, 40, -20),
                                           (60, 1.8, -15, 35), (170, 0.35, 200, 120)]):
        th = np.deg2rad(ang)
        R = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]]) * sc
        cases.append((f"synthetic-{i}", (DST @ R.T) + np.array([tx, ty]) + rng.normal(0, 0.4, (5, 2))))

    payload = {
        "_comment": ("Ground truth for frontend/src/face-align.js. The expected matrices are the "
                     "least-squares similarity transform onto SFace's reference points — the same "
                     "fit OpenCV's alignCrop applies (verified end-to-end by tests/test_face_align.py). "
                     "Regenerate with src/scripts/gen_align_fixture.py."),
        "reference_points": DST.tolist(),
        "cases": [{"name": n, "landmarks": lm.tolist(),
                   "expected": similarity_transform(lm, DST).tolist()} for n, lm in cases],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {out_path} ({len(payload['cases'])} cases)")


if __name__ == "__main__":
    main()
