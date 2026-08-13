// The push-to-talk take: when does one click's recording end, and does it auto-send?
//
// This file used to contain its own copy of the decision logic, replayed against synthetic level
// traces. It passed while testing an algorithm the app no longer used: it kept a hardcoded noise
// seed and froze the floor once speech began, both of which were REMOVED from App.jsx after music
// in the room made the take run to the cap every time. It also pinned VOICE_MAX_MS at 30 s when
// the app had long since moved to 300 s. Nothing about it could have caught either change,
// because it imported nothing.
//
// It now drives the real state machine, with the real thresholds.
import { test } from "node:test"
import assert from "node:assert"
import {
  createSilenceWatch, VOICE_MAX_MS, VOICE_NO_SPEECH_MS, VOICE_SILENCE_MS,
} from "../src/vad.js"

const FRAME = 16   // ~60fps, the rate requestAnimationFrame drives the watcher at

/** Replay a level trace through the real watcher. @returns {{stopped, autoSubmit, atMs, reason}} */
function run(levels) {
  const watch = createSilenceWatch({
    silenceMs: VOICE_SILENCE_MS, noSpeechMs: VOICE_NO_SPEECH_MS, maxMs: VOICE_MAX_MS, startedAt: 0,
  })
  for (let i = 0; i < levels.length; i++) {
    const end = watch.step(levels[i], i * FRAME)
    if (end) return { stopped: true, autoSubmit: end.autoSubmit, atMs: i * FRAME, reason: end.reason }
  }
  return { stopped: false, autoSubmit: false, atMs: levels.length * FRAME, reason: null }
}

const frames = (ms) => Math.ceil(ms / FRAME)
const quiet = (ms, v = 0.004) => Array(frames(ms)).fill(v)
const loud = (ms, v = 0.20) => Array(frames(ms)).fill(v)

/**
 * Someone actually talking, for `ms`: bursts with the short gaps between words.
 *
 * A flat run of one level is NOT speech to this detector and must not be — the floor seeds from
 * the room, so a constant tone becomes the floor and nothing ever clears it. That is the whole
 * defence against a fan or music opening a take, and it means "loud" has to vary to count.
 */
const speech = (ms, v = 0.20) => {
  const out = []
  while (out.length < frames(ms)) out.push(...loud(400, v), ...quiet(180))
  return out.slice(0, frames(ms))
}

test("speech then a pause auto-submits", () => {
  const r = run([...quiet(300), ...loud(1500), ...quiet(2000)])
  assert.equal(r.autoSubmit, true)
  assert.equal(r.reason, "pause")
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
  assert.equal(r.reason, "silence")
  assert.ok(r.atMs >= VOICE_NO_SPEECH_MS && r.atMs < VOICE_NO_SPEECH_MS + 200)
})

test("a noisy room still detects speech and still stops", () => {
  // constant fan at 0.02 — above the 0.012 absolute floor, so only the adaptive part saves us
  const r = run([...quiet(1500, 0.02), ...loud(1200, 0.35), ...quiet(2000, 0.02)])
  assert.equal(r.autoSubmit, true, "adaptive floor must absorb steady background noise")
})

test("the floor is seeded from the room, not from a guess", () => {
  // Music playing from the first frame. A hardcoded seed read this as speech immediately; seeding
  // from the first observation means the room IS the floor and only something louder counts.
  const r = run(Array(frames(4000)).fill(0.18))
  assert.equal(r.stopped, false, "steady background must not register as someone talking")
})

test("the floor keeps tracking during speech, so the pause after it is still detectable", () => {
  // Freezing the floor when capture began was a real bug: with a background above the frozen
  // trigger, every level after it read as loud, no pause was ever seen, and the take ran to the
  // cap. Speech over a loud-ish room, then the room alone.
  const r = run([...quiet(1200, 0.03), ...loud(1500, 0.40), ...quiet(3000, 0.03)])
  assert.equal(r.autoSubmit, true)
  assert.equal(r.reason, "pause")
})

test("a take that never falls quiet is capped, and the cap does not auto-send", () => {
  const r = run([...quiet(500), ...speech(VOICE_MAX_MS + 2000)])
  assert.equal(r.stopped, true)
  assert.equal(r.reason, "cap")
  assert.equal(r.autoSubmit, false,
    "a runaway recording lands in the box for review; it never fires a turn by itself")
})

test("the no-speech deadline only applies while nothing has been said", () => {
  // Talking past VOICE_NO_SPEECH_MS is normal — the deadline is for someone who clicked the mic
  // and then said nothing. Only the silence threshold may end a take that has heard speech.
  const r = run([...quiet(500), ...speech(9000), ...quiet(2000)])
  assert.equal(r.autoSubmit, true)
  assert.equal(r.reason, "pause")
  assert.ok(r.atMs > VOICE_NO_SPEECH_MS, `ended at ${r.atMs}ms, before anyone stopped talking`)
})
