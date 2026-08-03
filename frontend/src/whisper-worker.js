/**
 * whisper-worker.js — Dedicated Web Worker for client-side speech recognition.
 *
 * Runs the OpenAI Whisper model (ONNX) via @huggingface/transformers entirely
 * in-browser.  All heavy work (model download, inference) happens here so the
 * main thread stays 100% responsive, and — the whole point — so speech-to-text
 * never touches the 2011 server CPU.
 *
 * Model sourcing is two-stage:
 *   1. OFFICIAL — huggingface.co, the upstream first-party source. Preferred:
 *      it is the authoritative copy and costs our server no bandwidth.
 *   2. FAILSAFE — a SHA-256-pinned copy served by our own orchestrator at
 *      /stt-models (fetched by src/scripts/download_models.sh). Used only when
 *      the official fetch fails: offline LAN, blocked egress, HF outage.
 * The stage that succeeded is reported to the UI so the operator can tell which
 * one is in use rather than guessing.
 *
 * Protocol (postMessage):
 *   Main → Worker:
 *     { type: "load" }                    — download & initialise the model
 *     { type: "transcribe", audio: Float32Array }  — run inference on PCM audio
 *
 *   Worker → Main:
 *     { type: "progress", progress, file, loaded, total }  — download progress
 *     { type: "ready", source }           — model warm; source = official|failsafe
 *     { type: "result", text }            — transcription result
 *     { type: "error", error }            — something went wrong
 */

import { pipeline, env } from "@huggingface/transformers";

const MODEL_ID = "onnx-community/whisper-base";

// Absolute, origin-relative: the failsafe copy is served by OUR orchestrator, so it
// must not resolve against the worker's own /assets/ URL.
const FAILSAFE_PATH = "/stt-models/";

let transcriber = null;
let modelSource = null;

/**
 * Point transformers.js at exactly one source. Both flags are set on every call:
 * leaving the other one enabled would let the library silently fall back on its
 * own, which would hide which source actually served the model.
 */
function selectSource(source) {
  if (source === "official") {
    env.allowRemoteModels = true;
    env.allowLocalModels = false;
  } else {
    env.allowRemoteModels = false;
    env.allowLocalModels = true;
    env.localModelPath = FAILSAFE_PATH;
  }
}

function buildPipeline() {
  return pipeline("automatic-speech-recognition", MODEL_ID, {
    dtype: "q8",              // 8-bit quantised — smaller download, fast inference
    device: "wasm",           // WASM CPU — widest browser compatibility
    progress_callback: (p) => {
      // p = { status, file, progress, loaded, total }
      if (p.status === "progress") {
        self.postMessage({
          type: "progress",
          progress: p.progress ?? 0,
          file: p.file ?? "",
          loaded: p.loaded ?? 0,
          total: p.total ?? 0,
        });
      }
    },
  });
}

/**
 * Initialise the ASR pipeline.  Downloads the model on first run (~76 MB for
 * whisper-base q8) and caches it in the browser's Cache Storage for instant
 * subsequent loads.
 */
async function loadModel() {
  if (transcriber) {
    self.postMessage({ type: "ready", source: modelSource });
    return;
  }

  let officialError = null;
  for (const source of ["official", "failsafe"]) {
    try {
      selectSource(source);
      transcriber = await buildPipeline();
      modelSource = source;
      self.postMessage({ type: "ready", source });
      return;
    } catch (err) {
      if (source === "official") {
        officialError = err;
        // Not fatal on its own — this is exactly what the failsafe copy exists for.
        console.warn("[STT] official source failed, trying failsafe:", err?.message || err);
      } else {
        // Both sources are gone. Report the official failure too: it is almost always
        // the real cause (blocked egress / CSP), while the failsafe error is just a 404
        // from a server that was never given the bundle.
        self.postMessage({
          type: "error",
          error: `Model download failed. Official: ${officialError?.message || officialError}. ` +
                 `Failsafe: ${err?.message || err}`,
        });
      }
    }
  }
}

/**
 * Transcribe a Float32Array of 16 kHz mono PCM audio.
 */
async function transcribe(audio) {
  if (!transcriber) {
    self.postMessage({ type: "error", error: "Model not loaded" });
    return;
  }

  try {
    const result = await transcriber(audio, {
      language: "en",            // English-only for speed; change for multilingual
      task: "transcribe",
      chunk_length_s: 30,        // process in 30-second windows
      stride_length_s: 5,        // 5-second overlap between chunks
      return_timestamps: false,
    });

    const text = (result?.text ?? "").trim();
    self.postMessage({ type: "result", text });
  } catch (err) {
    self.postMessage({ type: "error", error: err?.message || String(err) });
  }
}

// ─── Message router ──────────────────────────────────────────────────────────
self.addEventListener("message", (e) => {
  const { type, audio } = e.data || {};

  switch (type) {
    case "load":
      loadModel();
      break;
    case "transcribe":
      transcribe(audio);
      break;
    default:
      self.postMessage({ type: "error", error: `Unknown message type: ${type}` });
  }
});
