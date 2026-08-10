/**
 * face-detect.test.mjs — pin the YuNet post-processing port to OpenCV's output.
 *
 * The decoding in face-detect.js was derived empirically from the real graph, not from YuNet
 * documentation (older releases were anchor-BASED with variance scaling; the 2023mar export is
 * anchor-free with per-stride outputs). These fixtures come from cv2.FaceDetectorYN on the same
 * frame, so if anyone "corrects" the decoding back toward the older scheme the tests catch it.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import {
  DETECTOR_SIZE, STRIDES, decodeStride, decodeAll, nms, iou, letterbox, unletterbox,
  toDetectorTensor, overlayRect,
} from "../src/face-detect.js";

const here = dirname(fileURLToPath(import.meta.url));
const fx = JSON.parse(readFileSync(join(here, "fixtures", "detect-fixture.json"), "utf8"));

/** Rebuild sparse per-stride arrays from the fixture's firing cells (everything else is zero,
 *  which scores 0 and is filtered out — exactly as in a real frame). */
function buildOutputs() {
  const sizes = {};
  for (const s of STRIDES) {
    const cols = DETECTOR_SIZE / s;
    sizes[s] = cols * cols;
  }
  const arrays = {};
  for (const s of STRIDES) {
    arrays[`cls_${s}`] = new Float32Array(sizes[s]);
    arrays[`obj_${s}`] = new Float32Array(sizes[s]);
    arrays[`bbox_${s}`] = new Float32Array(sizes[s] * 4);
    arrays[`kps_${s}`] = new Float32Array(sizes[s] * 10);
  }
  for (const c of fx.cells) {
    arrays[`cls_${c.stride}`][c.index] = c.cls;
    arrays[`obj_${c.stride}`][c.index] = c.obj;
    arrays[`bbox_${c.stride}`].set(c.bbox, c.index * 4);
    arrays[`kps_${c.stride}`].set(c.kps, c.index * 10);
  }
  return name => arrays[name];
}

test("the model's grid sizes are what the decoder assumes", () => {
  // 6400 + 1600 + 400 = 8400 predictions, one per cell — anchor-free. If a future re-export went
  // back to multiple anchors per cell this arithmetic would be wrong and everything downstream
  // would silently shift.
  assert.equal(DETECTOR_SIZE / 8 * (DETECTOR_SIZE / 8), 6400);
  assert.equal(DETECTOR_SIZE / 16 * (DETECTOR_SIZE / 16), 1600);
  assert.equal(DETECTOR_SIZE / 32 * (DETECTOR_SIZE / 32), 400);
});

test("decodeAll reproduces OpenCV's box and landmarks", () => {
  const faces = decodeAll(buildOutputs(), fx.score_threshold);
  assert.equal(faces.length, 1, "expected exactly one face after NMS");
  const got = faces[0];

  got.box.forEach((v, i) => {
    assert.ok(Math.abs(v - fx.expected.box[i]) < 0.01,
      `box[${i}] was ${v}, OpenCV says ${fx.expected.box[i]}`);
  });
  got.landmarks.forEach(([x, y], i) => {
    const [ex, ey] = fx.expected.landmarks[i];
    assert.ok(Math.hypot(x - ex, y - ey) < 0.01,
      `landmark ${i} was ${x},${y}, OpenCV says ${ex},${ey}`);
  });
});

test("score combines class and objectness as a geometric mean", () => {
  // So the threshold means the same thing here as in the agent's config.
  const c = fx.cells[0];
  // Math.fround mirrors the Float32Array the real outputs arrive in — comparing against the
  // fixture's full-precision float64 would fail on storage rounding alone, not on the formula.
  const expected = Math.sqrt(Math.min(1, Math.max(0, Math.fround(c.cls)))
                             * Math.min(1, Math.max(0, Math.fround(c.obj))));
  const cols = DETECTOR_SIZE / c.stride;
  const cls = new Float32Array(cols * cols), obj = new Float32Array(cols * cols);
  const bbox = new Float32Array(cols * cols * 4), kps = new Float32Array(cols * cols * 10);
  cls[c.index] = c.cls; obj[c.index] = c.obj;
  bbox.set(c.bbox, c.index * 4); kps.set(c.kps, c.index * 10);
  // Threshold 0 so every cell decodes; the one under test is at the firing index, not position 0.
  const d = decodeStride(c.stride, cls, obj, bbox, kps, 0)[c.index];
  assert.ok(Math.abs(d.score - expected) < 1e-6, `score ${d.score} != ${expected}`);
});

test("the score threshold actually filters", () => {
  const get = buildOutputs();
  assert.equal(decodeAll(get, 0.99999).length, 0);
  assert.ok(decodeAll(get, 0.5).length >= 1);
});

test("nms drops overlapping duplicates and keeps the best", () => {
  const a = { box: [0, 0, 10, 10], score: 0.9, landmarks: [] };
  const b = { box: [1, 1, 10, 10], score: 0.8, landmarks: [] };   // heavy overlap with a
  const c = { box: [100, 100, 10, 10], score: 0.7, landmarks: [] };
  const kept = nms([b, a, c], 0.3);
  assert.deepEqual(kept.map(k => k.score), [0.9, 0.7]);
});

test("iou is 1 for identical boxes and 0 for disjoint ones", () => {
  assert.equal(iou([0, 0, 10, 10], [0, 0, 10, 10]), 1);
  assert.equal(iou([0, 0, 10, 10], [50, 50, 10, 10]), 0);
});

test("letterbox preserves aspect ratio and centres the frame", () => {
  const b = letterbox(fx.letterbox.source_width, fx.letterbox.source_height);
  assert.ok(Math.abs(b.scale - fx.letterbox.scale) < 1e-9);
  assert.equal(b.ox, fx.letterbox.ox);
  assert.equal(b.oy, fx.letterbox.oy);
  assert.equal(b.w, fx.letterbox.w);
  assert.equal(b.h, fx.letterbox.h);
});

test("unletterbox inverts letterbox", () => {
  const b = letterbox(1280, 720);
  const [x, y] = unletterbox([b.ox + 100 * b.scale, b.oy + 50 * b.scale], b);
  assert.ok(Math.abs(x - 100) < 1e-6 && Math.abs(y - 50) < 1e-6);
});

test("a wide frame is padded, not squashed", () => {
  // A distorted face moves the landmarks and shifts the embedding, so this matters.
  const w = 1280, h = 720;
  const { data, box } = toDetectorTensor(new Uint8ClampedArray(w * h * 4).fill(255), w, h);
  assert.equal(data.length, 3 * DETECTOR_SIZE * DETECTOR_SIZE);
  assert.ok(Math.abs(box.w / box.h - w / h) < 0.01, "aspect ratio must survive");
  assert.ok(box.oy > 0 && box.ox === 0, "a landscape frame is letterboxed top/bottom");
  // Padding stays black; the image area keeps its value.
  const px = (x, y) => data[y * DETECTOR_SIZE + x];
  assert.equal(px(320, 1), 0);
  assert.equal(px(320, DETECTOR_SIZE / 2), 255);
});

test("detector tensor is BGR at 0-255, matching OpenCV's blobFromImage", () => {
  const w = 4, h = 4;
  const rgba = new Uint8ClampedArray(w * h * 4);
  for (let i = 0; i < w * h; i++) { rgba[i * 4] = 10; rgba[i * 4 + 1] = 20; rgba[i * 4 + 2] = 30; }
  const { data, box } = toDetectorTensor(rgba, w, h);
  const area = DETECTOR_SIZE * DETECTOR_SIZE;
  const at = (x, y) => y * DETECTOR_SIZE + x;
  const p = at(box.ox + 2, box.oy + 2);
  assert.equal(data[p], 30, "channel 0 must be BLUE");
  assert.equal(data[area + p], 20, "channel 1 must be GREEN");
  assert.equal(data[2 * area + p], 10, "channel 2 must be RED");
});

test("overlayRect mirrors the box to match the flipped preview", () => {
  // A face on the LEFT of the raw frame must be drawn on the RIGHT of the mirrored preview.
  // Getting this wrong puts the box on the opposite side of the face, which reads as broken
  // detection rather than as a display bug.
  const r = overlayRect([0, 0, 100, 100], 1000, 500);
  assert.equal(r.left, "90%");        // 1 - (0+100)/1000
  assert.equal(r.top, "0%");
  assert.equal(r.width, "10%");
  assert.equal(r.height, "20%");
});

test("overlayRect leaves coordinates alone when not mirrored", () => {
  const r = overlayRect([0, 0, 100, 100], 1000, 500, false);
  assert.equal(r.left, "0%");
});

test("a centred box stays centred under mirroring", () => {
  // The invariant that catches an off-by-one in the mirror: a box centred horizontally must map
  // to itself.
  const r = overlayRect([450, 0, 100, 10], 1000, 100);
  assert.equal(r.left, "45%");
});
