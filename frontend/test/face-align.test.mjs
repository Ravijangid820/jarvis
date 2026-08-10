/**
 * face-align.test.mjs — pin the browser alignment port to OpenCV's behaviour.
 *
 * The browser cannot call OpenCV, so face-align.js re-implements the transform SFace's alignCrop
 * applies. A *nearly* correct alignment is the dangerous failure: it produces embeddings that look
 * fine but sit slightly off in the vector space, so recognition degrades quietly instead of
 * breaking. These fixtures come from OpenCV itself (src/scripts/gen_align_fixture.py) and make any
 * such drift a test failure.
 *
 * Run: npm test   (node's built-in runner — no new dependency)
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import {
  REFERENCE_POINTS, ALIGNED_SIZE, similarityTransform, invertAffine,
  warpToAligned, alignedToTensor, l2Normalize, cosine,
} from "../src/face-align.js";

const here = dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(readFileSync(join(here, "fixtures", "align-fixture.json"), "utf8"));

test("reference points match the ones OpenCV aligns onto", () => {
  assert.deepEqual(REFERENCE_POINTS, fixture.reference_points);
});

test("similarityTransform reproduces OpenCV's estimateAffinePartial2D", () => {
  for (const c of fixture.cases) {
    const got = similarityTransform(c.landmarks, REFERENCE_POINTS);
    const want = [c.expected[0][0], c.expected[0][1], c.expected[0][2],
                  c.expected[1][0], c.expected[1][1], c.expected[1][2]];
    for (let i = 0; i < 6; i++) {
      // Translation terms are large (hundreds of px), so compare relatively; rotation/scale terms
      // are O(1) and compared absolutely. 1e-4 is far tighter than anything that would move an
      // embedding measurably.
      const scale = Math.max(1, Math.abs(want[i]));
      assert.ok(Math.abs(got[i] - want[i]) / scale < 1e-4,
        `${c.name}: term ${i} was ${got[i]}, OpenCV says ${want[i]}`);
    }
  }
});

test("the transform actually lands the landmarks on the reference points", () => {
  // The property the matrix exists for — independent of how it was computed.
  const lm = fixture.cases[0].landmarks;
  const m = similarityTransform(lm, REFERENCE_POINTS);
  lm.forEach((p, i) => {
    const x = m[0] * p[0] + m[1] * p[1] + m[2];
    const y = m[3] * p[0] + m[4] * p[1] + m[5];
    // A real face isn't a perfect similarity of the canonical one, so a few px of residual is
    // expected and correct — this checks the fit is sane, not exact.
    assert.ok(Math.hypot(x - REFERENCE_POINTS[i][0], y - REFERENCE_POINTS[i][1]) < 8,
      `landmark ${i} landed at ${x},${y}`);
  });
});

test("invertAffine round-trips", () => {
  const m = similarityTransform(fixture.cases[0].landmarks, REFERENCE_POINTS);
  const inv = invertAffine(m);
  const x = 40, y = 70;
  const fx = m[0] * x + m[1] * y + m[2], fy = m[3] * x + m[4] * y + m[5];
  const bx = inv[0] * fx + inv[1] * fy + inv[2], by = inv[3] * fx + inv[4] * fy + inv[5];
  assert.ok(Math.abs(bx - x) < 1e-6 && Math.abs(by - y) < 1e-6);
});

test("invertAffine refuses a degenerate matrix instead of returning NaNs", () => {
  assert.equal(invertAffine([0, 0, 0, 0, 0, 0]), null);
});

test("warpToAligned produces a full 112x112 RGBA buffer", () => {
  const w = 64, h = 64;
  const src = new Uint8ClampedArray(w * h * 4).fill(200);
  const m = similarityTransform(REFERENCE_POINTS, REFERENCE_POINTS);   // identity
  const out = warpToAligned(src, w, h, m);
  assert.equal(out.length, ALIGNED_SIZE * ALIGNED_SIZE * 4);
  // Inside the source region the value survives the resampling; outside is the black border.
  const at = (x, y) => out[(y * ALIGNED_SIZE + x) * 4];
  assert.equal(at(10, 10), 200);
  assert.equal(at(111, 111), 0);
});

test("warp is bilinear, not nearest-neighbour", () => {
  // A hard black/white edge sampled at a half-pixel offset must yield an intermediate value;
  // nearest-neighbour would snap to 0 or 255. Matching OpenCV's default interpolation is part of
  // matching the embedding space.
  const w = 8, h = 8;
  const src = new Uint8ClampedArray(w * h * 4);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const v = x < 4 ? 0 : 255;
      const o = (y * w + x) * 4;
      src[o] = src[o + 1] = src[o + 2] = v; src[o + 3] = 255;
    }
  }
  // Scale the 8px source across the 112px output: the edge is then spread over many output pixels.
  const m = [ALIGNED_SIZE / w, 0, 0, 0, ALIGNED_SIZE / h, 0];
  const out = warpToAligned(src, w, h, m);
  const row = 50;
  const values = [];
  for (let x = 0; x < ALIGNED_SIZE; x++) values.push(out[(row * ALIGNED_SIZE + x) * 4]);
  assert.ok(values.some(v => v > 5 && v < 250), "expected interpolated values across the edge");
});

test("alignedToTensor emits CHW in BGR at 0-255", () => {
  const px = new Uint8ClampedArray(ALIGNED_SIZE * ALIGNED_SIZE * 4);
  px[0] = 10; px[1] = 20; px[2] = 30; px[3] = 255;      // first pixel R=10 G=20 B=30
  const t = alignedToTensor(px);
  const area = ALIGNED_SIZE * ALIGNED_SIZE;
  assert.equal(t.length, 3 * area);
  assert.equal(t[0], 30, "channel 0 must be BLUE");
  assert.equal(t[area], 20, "channel 1 must be GREEN");
  assert.equal(t[2 * area], 10, "channel 2 must be RED");
  // SFace takes raw 0-255, not 0-1 — normalising here would silently break every match.
  assert.ok(t[0] > 1);
});

test("l2Normalize and cosine agree with the server's convention", () => {
  const v = l2Normalize([3, 4]);
  assert.ok(Math.abs(Math.hypot(v[0], v[1]) - 1) < 1e-12);
  assert.ok(Math.abs(cosine(v, v) - 1) < 1e-12);
  assert.ok(Math.abs(cosine(l2Normalize([1, 0]), l2Normalize([0, 1]))) < 1e-12);
});

test("l2Normalize survives an all-zero vector", () => {
  assert.deepEqual(l2Normalize([0, 0, 0]), [0, 0, 0]);
});
