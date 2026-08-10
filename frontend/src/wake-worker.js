/**
 * wake-worker.js — runs the "hey jarvis" detector off the main thread.
 *
 * Thin wrapper: all the arithmetic lives in wake-detect.js so it can be verified outside a
 * browser (it was — against openWakeWord's own reference implementation, matching to four decimal
 * places). This file only owns model loading, chunking and the fire/cooldown policy.
 *
 * Protocol:
 *   in : {type:"load"} | {type:"audio", pcm:Float32Array} | {type:"reset"} | {type:"threshold", value}
 *   out: {type:"ready"} | {type:"wake", score} | {type:"level", score} | {type:"error", error}
 */
import * as ort from "onnxruntime-web"
import { createWakeDetector, createChunker, CHUNK } from "./wake-detect.js"
import { fetchModel } from "./model-cache.js"

const BASE = import.meta.env.BASE_URL

// Same reasoning as whisper-worker: serve the ORT backend ourselves, version-scoped, because the
// CSP (rightly) blocks the CDN it would otherwise reach for — and a blocked fetch here surfaces as
// "no available backend found" with no hint that a CDN was involved.
if (ort?.env?.wasm) {
  ort.env.wasm.wasmPaths = `${BASE}ort/${__ORT_VERSION__}/`
  // One thread is plenty: this is ~3 small inferences per 80 ms, and it must coexist with Whisper
  // and the rest of the page rather than compete with them.
  ort.env.wasm.numThreads = 1
}

let detector = null
let feed = null
let threshold = 0.5          // openWakeWord's own default
let cooldownUntil = 0
const COOLDOWN_MS = 2000     // after firing, ignore detections so one phrase can't wake it twice

async function load() {
  try {
    const opts = { executionProviders: ["wasm"] }
    // Bytes come from Cache Storage rather than a URL so a reload does no network work at all —
    // ORT fetching the URL itself would revalidate over HTTP every time, and a hard refresh would
    // re-download all three. Building a session from an ArrayBuffer is otherwise identical.
    const [melBuf, embBuf, wwBuf] = await Promise.all([
      fetchModel(`${BASE}wake-models/melspectrogram.onnx`),
      fetchModel(`${BASE}wake-models/embedding_model.onnx`),
      fetchModel(`${BASE}wake-models/hey_jarvis_v0.1.onnx`),
    ])
    const [mel, emb, ww] = await Promise.all([
      ort.InferenceSession.create(new Uint8Array(melBuf), opts),
      ort.InferenceSession.create(new Uint8Array(embBuf), opts),
      ort.InferenceSession.create(new Uint8Array(wwBuf), opts),
    ])
    detector = createWakeDetector({ mel, emb, ww }, ort.Tensor)
    // The detector is stateful, so chunks must be processed strictly in order — hence one promise
    // chain rather than firing them off concurrently, which would interleave the ring buffers and
    // quietly produce nonsense scores.
    let chain = Promise.resolve()
    feed = createChunker((chunk) => {
      chain = chain
        .then(async () => {
          if (!detector) return
          const score = await detector.push(chunk)
          if (score === null) return
          self.postMessage({ type: "level", score })
          const now = Date.now()
          if (score >= threshold && now >= cooldownUntil) {
            cooldownUntil = now + COOLDOWN_MS
            detector.reset()          // so the same phrase cannot immediately re-fire
            self.postMessage({ type: "wake", score })
          }
        })
        .catch(err => self.postMessage({ type: "error", error: String(err?.message || err) }))
      return null
    })
    self.postMessage({ type: "ready" })
  } catch (err) {
    self.postMessage({
      type: "error",
      error: `Wake-word models failed to load: ${err?.message || err}. They are served from ` +
             `${BASE}wake-models/ — run src/scripts/download_models.sh if they are missing.`,
    })
  }
}

self.addEventListener("message", (e) => {
  const { type, pcm, value } = e.data || {}
  if (type === "load") load()
  else if (type === "audio") { if (feed && pcm) feed(pcm) }
  else if (type === "reset") { detector?.reset(); cooldownUntil = 0 }
  else if (type === "threshold") { if (typeof value === "number") threshold = value }
})

export { CHUNK }
