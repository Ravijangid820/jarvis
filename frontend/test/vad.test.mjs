// Replays the exact decision logic from startSilenceWatch against synthetic level traces.
import { test } from "node:test"
import assert from "node:assert"

const VOICE_SILENCE_MS = 1400, VOICE_NO_SPEECH_MS = 7000, VOICE_MAX_MS = 30000
const FRAME = 16   // ~60fps

/** @returns {{stopped:boolean, autoSubmit:boolean, atMs:number}} */
function run(levels) {   // levels: array of RMS values, one per frame
  let noise = 0.01, quietSince = 0, spoke = false
  const started = 0
  for (let i = 0; i < levels.length; i++) {
    const now = i * FRAME
    const level = levels[i]
    const trigger = Math.max(0.012, noise * 3)
    if (level > trigger) { spoke = true; quietSince = 0 }
    else {
      if (!spoke) noise = noise * 0.95 + level * 0.05
      if (!quietSince) quietSince = now
    }
    const quietFor = quietSince ? now - quietSince : 0
    if (spoke && quietFor >= VOICE_SILENCE_MS) return { stopped: true, autoSubmit: true, atMs: now }
    if ((!spoke && now - started >= VOICE_NO_SPEECH_MS) || now - started >= VOICE_MAX_MS)
      return { stopped: true, autoSubmit: false, atMs: now }
  }
  return { stopped: false, autoSubmit: false, atMs: levels.length * FRAME }
}

const frames = (ms) => Math.ceil(ms / FRAME)
const quiet = (ms, v = 0.004) => Array(frames(ms)).fill(v)
const loud  = (ms, v = 0.20)  => Array(frames(ms)).fill(v)

test("speech then a pause auto-submits", () => {
  const r = run([...quiet(300), ...loud(1500), ...quiet(2000)])
  assert.equal(r.autoSubmit, true)
  assert.ok(r.atMs > 1500 && r.atMs < 3600, `stopped at ${r.atMs}ms`)
})

test("a pause shorter than the threshold does NOT end the take", () => {
  // someone thinking mid-sentence: 900ms gap, then more speech
  const r = run([...loud(800), ...quiet(900), ...loud(800), ...quiet(400)])
  assert.equal(r.stopped, false, "must not cut someone off mid-thought")
})

test("clicking but never speaking gives up without submitting", () => {
  const r = run(quiet(9000))
  assert.equal(r.stopped, true)
  assert.equal(r.autoSubmit, false, "silence must never fire an empty turn")
  assert.ok(r.atMs >= VOICE_NO_SPEECH_MS && r.atMs < VOICE_NO_SPEECH_MS + 200)
})

test("a noisy room still detects speech and still stops", () => {
  // constant fan at 0.02 — above the 0.012 absolute floor, so only the adaptive part saves us
  const r = run([...quiet(1500, 0.02), ...loud(1200, 0.35), ...quiet(2000, 0.02)])
  assert.equal(r.autoSubmit, true, "adaptive floor must absorb steady background noise")
})

test("an endless take is capped", () => {
  const r = run(loud(40000))
  assert.equal(r.stopped, true)
  assert.equal(r.autoSubmit, false)
  assert.ok(r.atMs >= VOICE_MAX_MS)
})
