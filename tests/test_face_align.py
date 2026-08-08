"""The face-alignment correctness chain, end to end.

Browser-side enrollment only produces embeddings that match the ones a Pi camera produces if the
browser aligns the face exactly the way OpenCV's FaceRecognizerSF::alignCrop does. There is no
OpenCV in a browser, so frontend/src/face-align.js re-implements the transform — and a *nearly*
correct re-implementation is the dangerous outcome, because the embeddings still look plausible.

The chain is pinned in two halves:

    face-align.js  ==  similarity_transform()   <- frontend/test/face-align.test.mjs (fixtures)
    similarity_transform()  ==  cv2 alignCrop   <- THIS FILE, on real pixels through the real model

Skipped when opencv/the models aren't present, so the normal suite doesn't need a 38 MB model or a
CV dependency. Run it deliberately with:

    uv run --with opencv-python-headless --with numpy pytest tests/test_face_align.py -v
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "scripts"))

cv2 = pytest.importorskip("cv2", reason="opencv not installed")
np = pytest.importorskip("numpy")

from gen_align_fixture import DST, similarity_transform  # noqa: E402

YUNET = REPO / "camera" / "models" / "face_detection_yunet_2023mar.onnx"
SFACE = REPO / "camera" / "models" / "face_recognition_sface_2021dec.onnx"
SAMPLE = REPO / "frontend" / "test" / "fixtures" / "face_sample.jpg"

# SAMPLE is intentionally not committed — any photo with one clear face works, so there is no need
# to check a person's likeness into the repo. Supply one to run this cross-check locally.
pytestmark = pytest.mark.skipif(
    not (YUNET.exists() and SFACE.exists() and SAMPLE.exists()),
    reason="face models (run camera setup) or frontend/test/fixtures/face_sample.jpg not present")


@pytest.fixture(scope="module")
def detection():
    """(image, face row, landmarks) for the sample photo."""
    img = cv2.imread(str(SAMPLE))
    h, w = img.shape[:2]
    det = cv2.FaceDetectorYN.create(str(YUNET), "", (320, 320), 0.9, 0.3, 5000)
    det.setInputSize((w, h))
    _, faces = det.detect(img)
    assert faces is not None and len(faces), "no face detected in the fixture image"
    row = faces[0]
    return img, row, row[4:14].reshape(5, 2).astype(np.float64)


@pytest.fixture(scope="module")
def recognizer():
    return cv2.FaceRecognizerSF.create(str(SFACE), "")


def _embed(rec, aligned):
    f = rec.feature(aligned)[0].astype(np.float64)
    return f / (np.linalg.norm(f) or 1.0)


def test_reference_transform_reproduces_opencv_aligncrop(detection, recognizer):
    """The load-bearing assertion: our similarity fit and OpenCV's alignment agree on pixels."""
    img, row, lm = detection
    theirs = recognizer.alignCrop(img, row)
    ours = cv2.warpAffine(img, similarity_transform(lm, DST), (112, 112), flags=cv2.INTER_LINEAR)

    diff = np.abs(theirs.astype(int) - ours.astype(int))
    assert diff.max() <= 2, f"aligned crops differ by up to {diff.max()}/255 per pixel"
    assert diff.mean() < 0.05


def test_embeddings_from_both_alignments_are_interchangeable(detection, recognizer):
    """What actually matters: the same face, aligned either way, lands in the same place.

    SFace's recognition threshold is 0.363, so anything near 1.0 means a browser-enrolled face and
    a camera-enrolled one are directly comparable.
    """
    img, row, lm = detection
    theirs = _embed(recognizer, recognizer.alignCrop(img, row))
    ours = _embed(recognizer, cv2.warpAffine(img, similarity_transform(lm, DST), (112, 112),
                                             flags=cv2.INTER_LINEAR))
    assert float(theirs @ ours) > 0.9999


def test_unaligned_crop_is_measurably_worse(detection, recognizer):
    """Guards against the alignment silently becoming a no-op: a plain bbox resize must NOT score
    as well as the aligned crop, otherwise these tests would pass with the geometry removed."""
    img, row, lm = detection
    aligned = _embed(recognizer, recognizer.alignCrop(img, row))
    x, y, w, h = (int(v) for v in row[:4])
    x, y = max(0, x), max(0, y)
    naive = cv2.resize(img[y:y + h, x:x + w], (112, 112), interpolation=cv2.INTER_LINEAR)
    assert float(aligned @ _embed(recognizer, naive)) < 0.99


def test_transform_is_a_similarity_not_a_general_affine(detection):
    """SFace was trained on similarity-aligned faces. A 6-DOF affine fit would shear them, so the
    matrix must keep equal scale on both axes and stay orthogonal."""
    _, _, lm = detection
    m = similarity_transform(lm, DST)
    a, b, c, d = m[0, 0], m[0, 1], m[1, 0], m[1, 1]
    assert abs(np.hypot(a, c) - np.hypot(b, d)) < 1e-9      # equal scale on both axes
    assert abs(a * b + c * d) < 1e-9                        # axes stay perpendicular
