/**
 * face-detect.js — YuNet post-processing, as pure functions.
 *
 * The ONNX graph emits raw per-cell predictions; turning those into boxes and landmarks is done in
 * OpenCV's C++ for the edge agent and has to be re-done here for the browser. Getting it subtly
 * wrong yields plausible-looking boxes with displaced landmarks, which then produce a bad alignment
 * and a quietly useless embedding — so the decoding below was derived empirically against
 * cv2.FaceDetectorYN and is pinned by tests (see face-detect.test.mjs / tests/test_face_detect.py).
 *
 * Facts about face_detection_yunet_2023mar.onnx, established by inspecting the graph rather than
 * assumed from older YuNet documentation:
 *
 *   - The input is FIXED at 1x3x640x640. It is not a dynamic axis, so frames must be letterboxed
 *     to 640x640; onnxruntime rejects any other shape outright.
 *   - Outputs are SEPARATE per stride — cls_8/obj_8/bbox_8/kps_8, then _16, then _32 — not one
 *     concatenated tensor.
 *   - It is ANCHOR-FREE. There are no prior boxes and no variance scaling (both of which older
 *     YuNet releases used): one prediction per grid cell, 6400 + 1600 + 400 = 8400 in total.
 *   - Pixel values are raw 0-255 BGR in NCHW, matching OpenCV's blobFromImage defaults.
 */

export const DETECTOR_SIZE = 640;
export const STRIDES = [8, 16, 32];
export const DEFAULT_SCORE_THRESHOLD = 0.9;   // same as the camera agent's detectors.faces
export const DEFAULT_NMS_THRESHOLD = 0.3;

/** Intersection-over-union of two [x, y, w, h] boxes. */
export function iou(a, b) {
  const x1 = Math.max(a[0], b[0]), y1 = Math.max(a[1], b[1]);
  const x2 = Math.min(a[0] + a[2], b[0] + b[2]), y2 = Math.min(a[1] + a[3], b[1] + b[3]);
  const inter = Math.max(0, x2 - x1) * Math.max(0, y2 - y1);
  const union = a[2] * a[3] + b[2] * b[3] - inter;
  return union > 0 ? inter / union : 0;
}

/**
 * Decode one stride's raw outputs into candidate detections in 640x640 pixel space.
 *
 * @param {number} stride 8 | 16 | 32
 * @param {ArrayLike<number>} cls per-cell class score
 * @param {ArrayLike<number>} obj per-cell objectness
 * @param {ArrayLike<number>} bbox 4 per cell: dx, dy, log-w, log-h
 * @param {ArrayLike<number>} kps 10 per cell: five dx/dy landmark pairs
 * @param {number} scoreThreshold
 * @param {number} size detector input edge (640)
 */
export function decodeStride(stride, cls, obj, bbox, kps,
                             scoreThreshold = DEFAULT_SCORE_THRESHOLD, size = DETECTOR_SIZE) {
  const cols = size / stride;
  const out = [];
  for (let i = 0; i < cls.length; i++) {
    // Geometric mean of class and objectness — the combination OpenCV uses, so the threshold
    // carries the same meaning here as in the agent's config.
    const score = Math.sqrt(clamp01(cls[i]) * clamp01(obj[i]));
    if (score < scoreThreshold) continue;

    const cellX = i % cols, cellY = Math.floor(i / cols);
    // Anchor-free: the offset is relative to the CELL, in cell units, and the size is a log-scale
    // multiple of the stride. No prior box and no 0.1/0.2 variance terms are involved.
    const cx = (cellX + bbox[i * 4]) * stride;
    const cy = (cellY + bbox[i * 4 + 1]) * stride;
    const w = Math.exp(bbox[i * 4 + 2]) * stride;
    const h = Math.exp(bbox[i * 4 + 3]) * stride;

    const landmarks = [];
    for (let k = 0; k < 5; k++) {
      landmarks.push([(cellX + kps[i * 10 + k * 2]) * stride,
                      (cellY + kps[i * 10 + k * 2 + 1]) * stride]);
    }
    out.push({ box: [cx - w / 2, cy - h / 2, w, h], score, landmarks });
  }
  return out;
}

function clamp01(v) { return v < 0 ? 0 : v > 1 ? 1 : v; }

/** Greedy non-maximum suppression, highest score first. */
export function nms(candidates, threshold = DEFAULT_NMS_THRESHOLD, limit = 8) {
  const sorted = [...candidates].sort((a, b) => b.score - a.score);
  const kept = [];
  for (const c of sorted) {
    if (kept.every(k => iou(k.box, c.box) < threshold)) kept.push(c);
    if (kept.length >= limit) break;
  }
  return kept;
}

/**
 * Decode every stride and suppress overlaps.
 * @param {(name: string) => ArrayLike<number>} get output tensor data by name (e.g. "bbox_8")
 */
export function decodeAll(get, scoreThreshold = DEFAULT_SCORE_THRESHOLD,
                          nmsThreshold = DEFAULT_NMS_THRESHOLD, size = DETECTOR_SIZE) {
  const all = [];
  for (const s of STRIDES) {
    all.push(...decodeStride(s, get(`cls_${s}`), get(`obj_${s}`), get(`bbox_${s}`), get(`kps_${s}`),
                             scoreThreshold, size));
  }
  return nms(all, nmsThreshold);
}

/**
 * Letterbox geometry for fitting a frame into the detector's fixed square input without distorting
 * it. Squashing a face to a non-square aspect moves the landmarks and therefore shifts the
 * embedding, so aspect ratio is preserved and the remainder is padded.
 */
export function letterbox(width, height, size = DETECTOR_SIZE) {
  const scale = Math.min(size / width, size / height);
  const w = Math.round(width * scale), h = Math.round(height * scale);
  return { scale, w, h, ox: Math.floor((size - w) / 2), oy: Math.floor((size - h) / 2) };
}

/** Map a detector-space point back to original frame coordinates. */
export function unletterbox([x, y], { scale, ox, oy }) {
  return [(x - ox) / scale, (y - oy) / scale];
}

/**
 * Build the detector's input tensor: RGBA frame → letterboxed 640x640, BGR, NCHW, 0-255 floats.
 * Nearest-neighbour sampling is adequate here — this feeds DETECTION only. The embedding path
 * re-crops from the full-resolution frame (see face-align.js), so detector downscaling never
 * limits recognition quality.
 */
export function toDetectorTensor(rgba, width, height, size = DETECTOR_SIZE) {
  const box = letterbox(width, height, size);
  const area = size * size;
  const data = new Float32Array(3 * area);
  for (let y = 0; y < box.h; y++) {
    const srcY = Math.min(height - 1, Math.floor(y / box.scale));
    for (let x = 0; x < box.w; x++) {
      const srcX = Math.min(width - 1, Math.floor(x / box.scale));
      const s = (srcY * width + srcX) * 4;
      const d = (y + box.oy) * size + (x + box.ox);
      data[d] = rgba[s + 2];              // B
      data[area + d] = rgba[s + 1];       // G
      data[2 * area + d] = rgba[s];       // R
    }
  }
  return { data, box };
}

/**
 * Convert a detection box into CSS percentages for an overlay drawn on top of the video.
 *
 * `mirrored` matters and is easy to get wrong: the preview is displayed flipped (a selfie view is
 * what people expect) but detections are in UNFLIPPED frame coordinates, and the overlay element
 * is a sibling of the <video> — so the video's CSS transform does not move it. The left edge has
 * to be mirrored here. Flipping the overlay with its own scaleX(-1) does NOT work: that mirrors the
 * box about its own centre and leaves it on the wrong side of the frame.
 */
export function overlayRect(box, frameW, frameH, mirrored = true) {
  const [x, y, w, h] = box;
  const left = mirrored ? (1 - (x + w) / frameW) : (x / frameW);
  // Rounded: 1 - 0.55 is 0.44999999999999996 in binary floating point, which would otherwise put
  // "44.99999999999999%" into the style attribute. Three decimals is far finer than a pixel.
  const pct = (v) => `${Math.round(v * 100000) / 1000}%`;
  return { left: pct(left), top: pct(y / frameH), width: pct(w / frameW), height: pct(h / frameH) };
}
