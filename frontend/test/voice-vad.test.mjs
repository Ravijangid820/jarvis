// Mirrors the VAD state machine in VoiceLive.jsx (/voice live mode). Pins the two things that
// have bitten already: how long a pause must last before Jarvis decides you've stopped, and that
// the thresholds mean the same wall-clock time on a 44.1 kHz device as on a 48 kHz one.
import { test } from "node:test"
import assert from "node:assert"

const BLOCK = 2048
const PREROLL_MS = 430, START_MS = 130, END_SILENCE_MS = 5000, KEEP_TAIL_MS = 300
const MIN_UTTERANCE_MS = 350, MAX_UTTERANCE_MS = 300000
const TRIGGER_OVER_NOISE = 3.0, MIN_RMS = 0.012
const NOISE_FALL = 0.30, NOISE_RISE = 0.002, WARMUP_MS = 600

const conv = (rate) => {
  const msPerBlock = (BLOCK / rate) * 1000
  const inBlocks = (ms) => Math.max(1, Math.round(ms / msPerBlock))
  return { msPerBlock, inBlocks }
}

/** Replay a level trace; returns the utterance that would be sent, in ms of buffered audio. */
function run(levels, rate = 48000) {
  const { msPerBlock, inBlocks } = conv(rate)
  const prerollBlocks = inBlocks(PREROLL_MS), startBlocks = inBlocks(START_MS)
  const endBlocks = inBlocks(END_SILENCE_MS), minBlocks = inBlocks(MIN_UTTERANCE_MS)
  const maxBlocks = inBlocks(MAX_UTTERANCE_MS), warmupBlocks = inBlocks(WARMUP_MS)

  let noise = 0, warmed = 0, voiced = 0, quiet = 0, capturing = false, buf = 0, spoken = 0
  for (let i = 0; i < levels.length; i++) {
    const level = levels[i]
    if (warmed === 0) noise = level
    warmed++
    const floor = noise
    noise = level < floor ? floor * (1 - NOISE_FALL) + level * NOISE_FALL
                          : floor * (1 - NOISE_RISE) + level * NOISE_RISE
    const trigger = Math.max(MIN_RMS, floor * TRIGGER_OVER_NOISE)
    const loud = level > trigger
    buf++
    if (!capturing) {
      if (buf > prerollBlocks) buf = prerollBlocks
      if (warmed < warmupBlocks) { voiced = 0; continue }
      voiced = loud ? voiced + 1 : 0
      if (voiced >= startBlocks) { capturing = true; quiet = 0; spoken = voiced }
      continue
    }
    quiet = loud ? 0 : quiet + 1
    if (loud) spoken++
    const tooLong = buf >= maxBlocks
    if (quiet < endBlocks && !tooLong) continue
    // Voiced blocks, not buffered ones — the trailing silence is not part of the utterance.
    const keepTail = inBlocks(KEEP_TAIL_MS)
    const trimmed = Math.max(0, quiet - keepTail)
    const kept = Math.max(0, buf - trimmed)
    const sent = spoken >= minBlocks ? kept * msPerBlock : 0
    return { closedAtBlock: i, sentMs: sent, quietBlocks: quiet, msPerBlock, endBlocks, why: tooLong ? "cap" : "pause" }
  }
  return { closedAtBlock: -1, sentMs: 0, msPerBlock, endBlocks }
}

const blocksFor = (ms, rate = 48000) => Math.ceil(ms / ((BLOCK / rate) * 1000))
const quiet = (ms, rate = 48000, v = 0.004) => Array(blocksFor(ms, rate)).fill(v)
const loud = (ms, rate = 48000, v = 0.25) => Array(blocksFor(ms, rate)).fill(v)

test("a pause under the threshold does not end the utterance", () => {
  // 1.2s of thinking mid-sentence — used to cut people off at ~0.78s
  const r = run([...quiet(900), ...loud(900), ...quiet(3000), ...loud(700), ...quiet(300)])
  assert.equal(r.closedAtBlock, -1, "must keep listening through a 1.2s pause")
})

test("a pause past the threshold does end it", () => {
  // A quiet lead-in, as in real use: you open the page, the floor settles, then you speak.
  const r = run([...quiet(900), ...loud(900), ...quiet(7000)])
  assert.notEqual(r.closedAtBlock, -1)
  assert.ok(r.sentMs > 0, "and the utterance is actually sent")
})

test("the silence threshold is ~1.5s of real time, not of assumed-48kHz blocks", () => {
  for (const rate of [48000, 44100, 16000]) {
    const { msPerBlock, inBlocks } = conv(rate)
    const realMs = inBlocks(END_SILENCE_MS) * msPerBlock
    assert.ok(Math.abs(realMs - END_SILENCE_MS) < 50,
      `at ${rate}Hz the threshold is ${realMs.toFixed(0)}ms, expected ~${END_SILENCE_MS}ms`)
  }
})

test("44.1kHz and 48kHz close the utterance at the same wall-clock moment", () => {
  const a = run([...loud(800, 48000), ...quiet(2500, 48000)], 48000)
  const b = run([...loud(800, 44100), ...quiet(2500, 44100)], 44100)
  const aMs = a.closedAtBlock * a.msPerBlock
  const bMs = b.closedAtBlock * b.msPerBlock
  assert.ok(Math.abs(aMs - bMs) < 100, `48k closed at ${aMs.toFixed(0)}ms, 44.1k at ${bMs.toFixed(0)}ms`)
})

test("a click is still discarded rather than transcribed", () => {
  const r = run([...quiet(900), ...loud(120), ...quiet(7000)])
  assert.equal(r.sentMs, 0, "too short to be speech")
})

test("a long answer is NOT cut off part way through", () => {
  // The cap used to fire at 25 s and chop a long reply mid-sentence. It is now a memory backstop
  // far beyond any real utterance, so ~90 s of continuous speech must survive intact.
  const words = []
  for (let i = 0; i < 300; i++) words.push(...loud(220), ...quiet(90))   // ~93 s of talking
  const r = run([...quiet(1500), ...words, ...quiet(7000)])
  assert.notEqual(r.closedAtBlock, -1, "must still close once you stop")
  assert.equal(r.why, "pause", "it must end because you stopped talking, not because of a cap")
  assert.ok(r.sentMs > 80000, `only ${(r.sentMs / 1000).toFixed(1)}s survived`)
})

test("the memory backstop still exists for a capture that never ends", () => {
  // Guards the tab, not the user: buffered Float32 is ~190 KB/s, so an utterance that never
  // closes has to stop somewhere.
  const words = []
  for (let i = 0; i < 1400; i++) words.push(...loud(220), ...quiet(90))  // way past the backstop
  const r = run([...quiet(1500), ...words])
  assert.notEqual(r.closedAtBlock, -1, "must not buffer forever")
  assert.equal(r.why, "cap")
})


// --- background music -------------------------------------------------------------------------
// The floor used to start at a hardcoded 0.01 and freeze once capture began. With music playing,
// the music itself opened an utterance within ~85 ms and then held the level above the frozen
// trigger, so the pause never registered and every "utterance" ran to the 15 s cap — feeding
// Whisper 15 s of music, on repeat.

test("music alone never becomes a turn", () => {
  const r = run([...quiet(3000, 48000, 0.05), ...quiet(20000, 48000, 0.05)])
  assert.equal(r.sentMs, 0, "steady background must not be transcribed as speech")
})

test("speech over music still closes on the pause, not the 15s cap", () => {
  const music = 0.05
  const r = run([...quiet(2500, 48000, music), ...loud(1500, 48000, 0.25),
                 ...quiet(12000, 48000, music)])
  assert.notEqual(r.closedAtBlock, -1, "must close")
  const closedS = r.closedAtBlock * r.msPerBlock / 1000
  assert.ok(closedS < 12, `closed at ${closedS.toFixed(1)}s — the 15s cap means it never heard the pause`)
  assert.ok(r.sentMs > 0, "and the utterance is sent")
})

test("the warm-up stops a loud start from triggering instantly", () => {
  // going live with music already playing: the first blocks must not open an utterance
  const r = run([...quiet(400, 48000, 0.08)])
  assert.equal(r.closedAtBlock, -1)
})


// --- the pause threshold is the setting people actually feel ------------------------------------

test("a 3-second thinking pause no longer ends the sentence", () => {
  // The complaint that drove the default to 5s: pausing mid-thought was read as "finished".
  const r = run([...quiet(900), ...loud(800), ...quiet(3000), ...loud(900), ...quiet(7000)])
  const closedS = r.closedAtBlock * r.msPerBlock / 1000
  assert.ok(closedS > 5.5, `closed at ${closedS.toFixed(1)}s — cut off during the pause`)
  assert.ok(r.sentMs > 0)
})

test("the trailing pause is trimmed off before transcription", () => {
  // 5s of silence on every clip would dominate the audio Whisper sees, costing time on a slow
  // CPU and inviting it to hallucinate text to fill the gap.
  const r = run([...quiet(900), ...loud(1200), ...quiet(7000)])
  assert.ok(r.sentMs > 0, "still sends")
  assert.ok(r.sentMs < 1200 + KEEP_TAIL_MS + PREROLL_MS + 400,
    `clip is ${r.sentMs.toFixed(0)}ms — the 5s pause was not trimmed`)
})
