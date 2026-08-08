/**
 * face-worker.js — browser-side face detection, alignment and embedding.
 *
 * Runs the SAME two OpenCV Zoo models the Raspberry Pi camera agent runs — YuNet to find faces and
 * five landmarks, SFace to turn an aligned face into a 128-D vector — via onnxruntime-web. Weights,
 * alignment and normalisation all match the edge pipeline (pinned by frontend/test/*.test.mjs and
 * tests/test_face_align.py), so a face enrolled from a laptop webcam is directly comparable with
 * one enrolled by a camera.
 *
 * The privacy property that makes this acceptable for a public demo: **pixels never leave the
 * browser**. Frames are captured, detected, aligned and embedded here; only the resulting
 * L2-normalised vector is sent to the server. That is the same posture as the edge agent, not a
 * weaker one invented for the web.
 *
 * Runs in a Worker so the ~38 MB model load and per-frame inference never jank the UI.
 *
 * Protocol (postMessage):
 *   Main → Worker
 *     { type: "load" }                                  — fetch + initialise both models
 *     { type: "detect", rgba, width, height }           — one frame → faces with embeddings
 *   Worker → Main
 *     { type: "progress", stage, loaded, total }
 *     { type: "ready" }
 *     { type: "faces", faces: [{ box, score, landmarks, embedding }] }
 *     { type: "error", error }
 */

import * as ort from "onnxruntime-web";
import { alignFace, alignedToTensor, l2Normalize } from "./face-align.js";
import { DETECTOR_SIZE, decodeAll, toDetectorTensor, unletterbox } from "./face-detect.js";

// Base-aware like whisper-worker.js: BASE_URL is "/" when the orchestrator serves the SPA but
// "/jarvis/" for the Pages build, so an absolute "/face-models/…" would 404 there.
const BASE = import.meta.env.BASE_URL;
const YUNET_URL = `${BASE}face-models/face_detection_yunet_2023mar.onnx`;
const SFACE_URL = `${BASE}face-models/face_recognition_sface_2021dec.onnx`;

// Same ORT vendoring as the STT worker: served by us, never a CDN — our CSP blocks remote script,
// and this is executable code rather than model data.
if (ort?.env?.wasm) {
  ort.env.wasm.wasmPaths = `${BASE}ort/${__ORT_VERSION__}/`;
  // Threads need SharedArrayBuffer, which requires cross-origin isolation. GitHub Pages cannot
  // send COOP/COEP, so requesting workers there is the difference between "slower" and "hangs".
  ort.env.wasm.numThreads = self.crossOriginIsolated
    ? Math.min(4, navigator.hardwareConcurrency || 2) : 1;
  ort.env.wasm.simd = true;
}

let detSession = null;
let recSession = null;

async function fetchWithProgress(url, stage) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${stage} model unavailable (HTTP ${res.status})`);
  const total = Number(res.headers.get("content-length") || 0);
  const reader = res.body?.getReader();
  if (!reader) return new Uint8Array(await res.arrayBuffer());
  const chunks = [];
  let loaded = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    loaded += value.length;
    self.postMessage({ type: "progress", stage, loaded, total });
  }
  const out = new Uint8Array(loaded);
  let off = 0;
  for (const c of chunks) { out.set(c, off); off += c.length; }
  return out;
}

async function load() {
  if (detSession && recSession) { self.postMessage({ type: "ready" }); return; }
  const opts = { executionProviders: ["wasm"], graphOptimizationLevel: "all" };
  // Detector first: it is 232 KB against SFace's 38 MB, so the UI can show a live box while the
  // recognizer is still downloading.
  detSession ??= await ort.InferenceSession.create(await fetchWithProgress(YUNET_URL, "detector"), opts);
  recSession ??= await ort.InferenceSession.create(await fetchWithProgress(SFACE_URL, "recognizer"), opts);
  self.postMessage({ type: "ready" });
}

async function detect(rgba, width, height) {
  if (!detSession || !recSession) throw new Error("models not loaded");

  const { data, box } = toDetectorTensor(rgba, width, height);
  const outputs = await detSession.run({
    [detSession.inputNames[0]]: new ort.Tensor("float32", data, [1, 3, DETECTOR_SIZE, DETECTOR_SIZE]),
  });
  const found = decodeAll(name => outputs[name].data);

  const faces = [];
  for (const f of found) {
    // Back out of the letterbox BEFORE aligning: the crop must come from the full-resolution
    // frame, not from the 640px detector view, or the face is upscaled from a thumbnail and the
    // embedding degrades for no reason.
    const landmarks = f.landmarks.map(p => unletterbox(p, box));
    const [bx, by] = unletterbox([f.box[0], f.box[1]], box);
    const aligned = alignFace(rgba, width, height, landmarks);
    const recOut = await recSession.run({
      [recSession.inputNames[0]]: new ort.Tensor("float32", alignedToTensor(aligned), [1, 3, 112, 112]),
    });
    faces.push({
      box: [bx, by, f.box[2] / box.scale, f.box[3] / box.scale],
      score: f.score,
      landmarks,
      embedding: l2Normalize(Array.from(recOut[recSession.outputNames[0]].data)),
    });
  }
  return faces;
}

self.onmessage = async (e) => {
  const msg = e.data || {};
  try {
    if (msg.type === "load") {
      await load();
    } else if (msg.type === "detect") {
      self.postMessage({ type: "faces", faces: await detect(msg.rgba, msg.width, msg.height) });
    }
  } catch (err) {
    self.postMessage({ type: "error", error: String(err?.message || err) });
  }
};
