/**
 * wake-detect.js — the openWakeWord "hey jarvis" pipeline, as a pure module.
 *
 * Three chained ONNX models, streaming:
 *     16 kHz audio -> melspectrogram -> speech embedding -> "hey jarvis" classifier -> 0..1
 *
 * Kept separate from wake-worker.js (the same split as face-detect/face-worker) so the arithmetic
 * can be exercised outside a browser. That matters here more than usual: a first attempt at this
 * scored 0.000 on everything INCLUDING "hey jarvis", and a wake word that silently never fires is
 * indistinguishable from one that is merely switched off.
 *
 * The frame arithmetic is the part that is easy to get wrong. The melspectrogram model returns
 * `samples/160 - 3` frames, so to obtain exactly 8 new frames per 1280-sample chunk it must be fed
 * that chunk PLUS 480 samples of the preceding audio. Verified against openWakeWord's own
 * reference implementation on identical clips: 0.9987 / 0.9988 on "hey jarvis", 0.0002 on
 * unrelated speech — matching to four decimal places.
 */
export const RATE = 16000
export const CHUNK = 1280      // 80 ms — the step openWakeWord is built around
export const LOOKBACK = 480    // 160*3, the context that makes the mel return exactly 8 frames
export const MEL_WIN = 76      // mel frames per embedding
export const FEAT_WIN = 16     // embeddings per classification
const MEL_KEEP = 96            // ring sizes: a little more than each window needs
const FEAT_KEEP = 32
const INT16 = 32767            // the models were trained on int16-scale audio, not ±1 floats

/**
 * @param sessions {{mel, emb, ww}} onnxruntime InferenceSessions (web or node — same API)
 * @param Tensor   the runtime's Tensor constructor
 */
export function createWakeDetector(sessions, Tensor) {
  const { mel, emb, ww } = sessions
  const raw = new Float32Array(CHUNK + LOOKBACK)
  let primed = 0
  const melBuf = []
  const featBuf = []

  return {
    /** Feed exactly CHUNK samples (±1 float scale). Returns a 0..1 score, or null while warming. */
    async push(chunk) {
      raw.copyWithin(0, CHUNK)          // drop the oldest CHUNK, keep LOOKBACK of context
      raw.set(chunk, LOOKBACK)
      if (primed < 1) { primed++; return null }   // first call has no real lookback yet

      const scaled = new Float32Array(raw.length)
      for (let i = 0; i < raw.length; i++) scaled[i] = raw[i] * INT16

      const melOut = await mel.run({ input: new Tensor("float32", scaled, [1, raw.length]) })
      const m = melOut[mel.outputNames[0]]
      const frames = m.dims[2]
      for (let f = 0; f < frames; f++) {
        const row = new Float32Array(32)
        for (let b = 0; b < 32; b++) row[b] = m.data[f * 32 + b] / 10 + 2   // openWakeWord's transform
        melBuf.push(row)
      }
      while (melBuf.length > MEL_KEEP) melBuf.shift()
      if (melBuf.length < MEL_WIN) return null

      const win = new Float32Array(MEL_WIN * 32)
      const start = melBuf.length - MEL_WIN
      for (let i = 0; i < MEL_WIN; i++) win.set(melBuf[start + i], i * 32)
      const embOut = await emb.run({ input_1: new Tensor("float32", win, [1, MEL_WIN, 32, 1]) })
      featBuf.push(Float32Array.from(embOut[emb.outputNames[0]].data))
      while (featBuf.length > FEAT_KEEP) featBuf.shift()
      if (featBuf.length < FEAT_WIN) return null

      const feats = new Float32Array(FEAT_WIN * 96)
      const fstart = featBuf.length - FEAT_WIN
      for (let i = 0; i < FEAT_WIN; i++) feats.set(featBuf[fstart + i], i * 96)
      const out = await ww.run({ "x.1": new Tensor("float32", feats, [1, FEAT_WIN, 96]) })
      return Number(out[ww.outputNames[0]].data[0])
    },

    /** Forget everything heard so far — used after a detection so the same phrase can't re-fire. */
    reset() {
      raw.fill(0)
      primed = 0
      melBuf.length = 0
      featBuf.length = 0
    },
  }
}

/**
 * Slice an arbitrary stream of audio blocks into exact CHUNK-sized pieces.
 * The mic delivers whatever block size the audio graph uses; the detector needs 1280 exactly.
 */
export function createChunker(onChunk) {
  let pending = new Float32Array(0)
  return (block) => {
    const merged = new Float32Array(pending.length + block.length)
    merged.set(pending, 0)
    merged.set(block, pending.length)
    let off = 0
    const out = []
    while (merged.length - off >= CHUNK) {
      out.push(onChunk(merged.slice(off, off + CHUNK)))
      off += CHUNK
    }
    pending = merged.slice(off)
    return out
  }
}
