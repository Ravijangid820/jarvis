/**
 * server-mic.js — the server's microphone, disguised as an ordinary MediaStream.
 *
 * The point of this file is that nothing else has to know. `/voice/server-mic/stream` sends raw
 * 16 kHz mono PCM; this pushes it through a Web Audio graph into a MediaStreamAudioDestinationNode
 * and hands back `dest.stream`. Callers then do exactly what they already do with getUserMedia's
 * stream — createMediaStreamSource, VAD, wake word, Whisper — with no branch for "where did this
 * audio come from".
 *
 * That is also why the server sends audio rather than text: transcription stays in the tab, on the
 * user's own CPU, which is the arrangement v3.2.0 deliberately moved to. The server does no STT for
 * this feature; it only opens a microphone.
 */

export const SERVER_PCM_RATE = 16000

/**
 * Open the server microphone as a MediaStream.
 *
 * @param {string} apiBase  orchestrator base URL
 * @param {string} token    bearer token
 * @param {string} device   ALSA device id from /voice/inputs (e.g. "plughw:1,0")
 * @param {{onEnd?: (reason: string) => void}} [opts]
 * @returns {Promise<{stream: MediaStream, stop: () => void}>}
 */
export async function openServerMicStream(apiBase, token, device, opts = {}) {
  const ctrl = new AbortController()
  const res = await fetch(`${apiBase}/voice/server-mic/stream?device=${encodeURIComponent(device)}`,
    { headers: { Authorization: "Bearer " + token }, signal: ctrl.signal })
  if (!res.ok) {
    // The server waits for real audio before answering 200, so a failure to open the device is a
    // proper error here with ffmpeg's own reason in it — not a stream that starts and says nothing.
    let detail = `HTTP ${res.status}`
    try { detail = (await res.json()).detail || detail } catch { /* keep the status */ }
    throw new Error(res.status === 409
      ? "The server microphone is already in use by another session."
      : detail)
  }
  if (!res.body) throw new Error("This browser cannot stream the server microphone")

  // Ask for a context at the PCM's own rate so the common case needs no resampling at all. A
  // browser that refuses the rate gets linear interpolation below rather than a pitch shift.
  let actx
  try { actx = new AudioContext({ sampleRate: SERVER_PCM_RATE }) } catch { actx = new AudioContext() }
  if (actx.state === "suspended") await actx.resume()

  const dest = actx.createMediaStreamDestination()
  const BLOCK = 2048
  const node = actx.createScriptProcessor(BLOCK, 1, 1)

  // A ring of decoded samples the audio callback drains. The network delivers in bursts and the
  // callback wants a steady trickle, so some buffering is unavoidable; the cap keeps a slow
  // consumer from growing it without bound, dropping the oldest audio rather than the newest
  // (stale audio is worthless here — this is a live conversation, not a recording).
  const MAX_QUEUED = SERVER_PCM_RATE * 5
  let queue = new Float32Array(0)
  const ratio = SERVER_PCM_RATE / actx.sampleRate    // 1 when we got the rate we asked for

  const push = (samples) => {
    const merged = new Float32Array(queue.length + samples.length)
    merged.set(queue); merged.set(samples, queue.length)
    queue = merged.length > MAX_QUEUED ? merged.subarray(merged.length - MAX_QUEUED) : merged
  }

  node.onaudioprocess = (ev) => {
    const out = ev.outputBuffer.getChannelData(0)
    if (ratio === 1) {
      const n = Math.min(out.length, queue.length)
      out.set(queue.subarray(0, n))
      // Underrun → silence for the remainder. Silence is the right filler: the VAD reads it as a
      // pause, whereas repeating the last block would sound like speech and could split or extend
      // an utterance.
      if (n < out.length) out.fill(0, n)
      queue = queue.subarray(n)
    } else {
      const need = Math.ceil(out.length * ratio)
      for (let i = 0; i < out.length; i++) {
        const pos = i * ratio
        const a = Math.floor(pos)
        out[i] = a + 1 < queue.length ? queue[a] + (queue[a + 1] - queue[a]) * (pos - a)
          : (queue[a] ?? 0)
      }
      queue = queue.subarray(Math.min(need, queue.length))
    }
  }
  node.connect(dest)

  let stopped = false
  const stop = () => {
    if (stopped) return
    stopped = true
    ctrl.abort()
    try { node.disconnect() } catch { /* already torn down */ }
    try { actx.close() } catch { /* already closed */ }
  }

  // Pump the body in the background. s16le arrives as bytes on arbitrary boundaries, so a trailing
  // odd byte is carried into the next chunk — splitting a sample across reads would otherwise
  // inject a loud click on every chunk boundary.
  ;(async () => {
    const reader = res.body.getReader()
    let odd = null
    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done || stopped) break
        let bytes = value
        if (odd) {
          const j = new Uint8Array(odd.length + value.length)
          j.set(odd); j.set(value, odd.length)
          bytes = j; odd = null
        }
        const usable = bytes.length - (bytes.length % 2)
        if (usable < bytes.length) odd = bytes.subarray(usable)
        const view = new DataView(bytes.buffer, bytes.byteOffset, usable)
        const f = new Float32Array(usable / 2)
        for (let i = 0; i < f.length; i++) f[i] = view.getInt16(i * 2, true) / 32768
        push(f)
      }
      if (!stopped) opts.onEnd?.("The server microphone stopped sending audio.")
    } catch (e) {
      if (!stopped) opts.onEnd?.(String(e?.message || e))
    }
  })()

  return { stream: dest.stream, stop }
}
