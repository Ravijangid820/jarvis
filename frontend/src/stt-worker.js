/**
 * stt-worker.js — one warm Whisper worker for the whole document.
 *
 * The model was being loaded far more often than it needed to be. Three separate causes stacked up,
 * and all three produced the same "Preparing model…" the user sees every single time:
 *
 *   1. The chat mic and the live voice page each built their own worker, so the same ~76 MB model
 *      was compiled twice in one session.
 *   2. Leaving the live page tore its worker down, so coming back paid for it again.
 *   3. Stopping and restarting live mode terminated the worker outright.
 *
 * So the worker lives here, at module scope, owned by nobody and outliving every component that
 * uses it. Loading happens **once per page**; afterwards `ensureStt()` resolves on the spot and the
 * caller never shows a loading state at all.
 *
 * What this does NOT fix, deliberately: a genuine page reload still has to rebuild the ONNX session
 * from cached bytes, which costs seconds of CPU no cache can avoid. The download is gone (the model
 * bytes sit in the Cache Storage transformers.js manages), but compiling the graph is real work.
 * `isSttWarm()` exists so the UI can tell the two apart and say "Restoring…" rather than implying
 * a download that isn't happening.
 */

let worker = null
let loadPromise = null
let warm = false
let modelSource = ""
let nextId = 1
const pending = new Map()          // id -> {resolve, reject}
const progressSubs = new Set()     // load-progress listeners, while a load is in flight

/** True once the model is compiled and resident — callers can skip their loading UI entirely. */
export function isSttWarm() { return warm }

/** Which source the resident model came from ("official" | "failsafe"), for the status line. */
export function sttSource() { return modelSource }

function spawn() {
  const w = new Worker(new URL("./whisper-worker.js", import.meta.url), { type: "module" })
  w.addEventListener("message", (e) => {
    const m = e.data || {}
    if (m.type === "progress" || m.type === "status") {
      for (const fn of progressSubs) { try { fn(m) } catch { /* a bad listener must not stall load */ } }
      return
    }
    if (m.type === "ready") {
      warm = true
      modelSource = m.source || ""
      return
    }
    // Results and errors are routed by id. An error with no id belongs to the load, not to a
    // transcription — failing every queued request on it would be wrong.
    if (m.id != null && pending.has(m.id)) {
      const { resolve, reject } = pending.get(m.id)
      pending.delete(m.id)
      if (m.type === "result") resolve(m.text || "")
      else if (m.type === "error") reject(new Error(m.error || "Transcription failed"))
    }
  })
  return w
}

/**
 * Ensure the model is loaded, reusing the resident one if there is one.
 *
 * @param {(m: {type: string, progress?: number, phase?: string}) => void} [onProgress]
 * @returns {Promise<{source: string, cached: boolean}>} `cached` is true when nothing had to load.
 */
export function ensureStt(onProgress) {
  if (warm) return Promise.resolve({ source: modelSource, cached: true })
  if (onProgress) progressSubs.add(onProgress)
  if (!loadPromise) {
    worker ??= spawn()
    loadPromise = new Promise((resolve, reject) => {
      const onMsg = (e) => {
        const m = e.data || {}
        if (m.type === "ready") {
          worker.removeEventListener("message", onMsg)
          resolve({ source: m.source || "", cached: false })
        } else if (m.type === "error" && m.id == null) {
          worker.removeEventListener("message", onMsg)
          // Drop the failed attempt so a retry genuinely retries instead of resolving the same
          // rejected promise forever.
          loadPromise = null
          reject(new Error(m.error || "Model failed to load"))
        }
      }
      worker.addEventListener("message", onMsg)
      worker.postMessage({ type: "load" })
    })
  }
  return loadPromise.finally(() => { if (onProgress) progressSubs.delete(onProgress) })
}

/**
 * Transcribe 16 kHz mono PCM. Loads the model first if it isn't resident yet.
 * The audio buffer is transferred, not copied — a 30 s clip is ~2 MB.
 */
export async function transcribeAudio(pcm) {
  await ensureStt()
  const id = nextId++
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject })
    worker.postMessage({ type: "transcribe", audio: pcm, id }, [pcm.buffer])
  })
}

/**
 * Tear the worker down. Intended for logout, where leaving a model resident for the next person at
 * the same browser is the wrong default — not for ordinary navigation, which is the whole point of
 * this module.
 */
export function releaseStt() {
  worker?.terminate()
  worker = null
  loadPromise = null
  warm = false
  modelSource = ""
  for (const { reject } of pending.values()) reject(new Error("Speech recognition stopped"))
  pending.clear()
  progressSubs.clear()
}
