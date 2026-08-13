// Continuous segmentation in /voice live mode: how long a pause must last before Jarvis decides
// you have stopped, and that every threshold means the same wall-clock time on a 44.1 kHz device
// as on a 48 kHz one.
//
// This used to be a second implementation of the state machine, replayed against synthetic level
// traces — 160 lines that imported nothing from ../src and so could not have failed for any change
// to the app. It now drives the real gate from vad.js, with the real thresholds; only the audio
// buffer is simulated, since counting blocks is all these assertions need.
import { test } from "node:test"
import assert from "node:assert"
import {
  BLOCK, blockTiming, createUtteranceGate, END_SILENCE_MS, KEEP_TAIL_MS, MAX_UTTERANCE_MS,
  MIN_UTTERANCE_MS, PREROLL_MS, START_MS, WARMUP_MS,
} from "../src/vad.js"

const conv = (rate) => blockTiming(rate, BLOCK)

/** Replay a level trace through the real gate; reports the utterance it would send, in ms. */
function run(levels, rate = 48000, pauseMs = END_SILENCE_MS) {
  const { msPerBlock, inBlocks } = conv(rate)
  const gate = createUtteranceGate({
    prerollBlocks: inBlocks(PREROLL_MS),
    startBlocks: inBlocks(START_MS),
    warmupBlocks: inBlocks(WARMUP_MS),
    minBlocks: inBlocks(MIN_UTTERANCE_MS),
    keepTailBlocks: inBlocks(KEEP_TAIL_MS),
  })
  const endBlocks = inBlocks(pauseMs)
  const maxBlocks = inBlocks(MAX_UTTERANCE_MS)
  let buf = 0                                  // stands in for VoiceLive's array of audio blocks
  let openedAtBlock = -1
  for (let i = 0; i < levels.length; i++) {
    buf++
    if (!gate.capturing && buf > gate.prerollBlocks) buf--          // the caller's shift()
    const r = gate.step(levels[i], buf, { endBlocks, maxBlocks })
    if (r.state === "opened" && openedAtBlock < 0) openedAtBlock = i
    if (r.state !== "closed") continue
    const kept = Math.max(0, buf - r.dropTrailing)
    return {
      closedAtBlock: i, openedAtBlock, sentMs: r.usable ? kept * msPerBlock : 0,
      msPerBlock, endBlocks, why: r.tooLong ? "cap" : "pause",
    }
  }
  return { closedAtBlock: -1, openedAtBlock, sentMs: 0, msPerBlock, endBlocks }
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

test("going live with music already playing does not open an utterance", () => {
  // The floor seeds from the room itself, so steady music simply becomes the floor.
  const r = run([...quiet(4000, 48000, 0.08)])
  assert.equal(r.openedAtBlock, -1, "steady background is the floor, never speech")
})

test("nothing may open an utterance until the floor has heard the room", () => {
  // The first block or two out of an AudioContext are often digital silence, before the mic is
  // really delivering. Seeding the floor from THAT means a trigger of 0.012 against a room at
  // 0.05, so the room reads as speech and capture opens on block three — which is what the
  // warm-up window exists to prevent. It cannot make a bad seed good; it holds the gate shut
  // while the floor catches up.
  const r = run([...quiet(150, 48000, 0), ...loud(3000, 48000, 0.05), ...quiet(9000, 48000, 0)])
  assert.notEqual(r.openedAtBlock, -1, "it must still open eventually")
  const openedMs = r.openedAtBlock * r.msPerBlock
  assert.ok(openedMs >= WARMUP_MS,
    `opened at ${openedMs.toFixed(0)}ms, inside the ${WARMUP_MS}ms warm-up`)
})


// --- the pause threshold is the setting people actually feel ------------------------------------

test("a 3-second thinking pause no longer ends the sentence", () => {
  // The complaint that drove the default to 5s: pausing mid-thought was read as "finished".
  const r = run([...quiet(900), ...loud(800), ...quiet(3000), ...loud(900), ...quiet(7000)])
  const closedS = r.closedAtBlock * r.msPerBlock / 1000
  assert.ok(closedS > 5.5, `closed at ${closedS.toFixed(1)}s — cut off during the pause`)
  assert.ok(r.sentMs > 0)
})

test("a shorter pause setting closes the utterance sooner", () => {
  // The setting people actually reach for. Same trace, two thresholds: it must be the threshold
  // that decides, not something that only looks configurable.
  const trace = [...quiet(900), ...loud(800), ...quiet(9000)]
  const fast = run(trace, 48000, 1500)
  const slow = run(trace, 48000, 8000)
  assert.ok(fast.closedAtBlock < slow.closedAtBlock)
  const gapS = (slow.closedAtBlock - fast.closedAtBlock) * slow.msPerBlock / 1000
  assert.ok(Math.abs(gapS - 6.5) < 0.5, `${gapS.toFixed(1)}s apart, expected the 6.5s difference`)
})

test("while armed for a wake phrase the pause is much tighter", () => {
  // "jarvis, are you there" has to be heard and answered quickly. Waiting out the conversational
  // pause first — up to 8 s — would make the wake word feel broken.
  const trace = [...quiet(900), ...loud(700), ...quiet(2000)]
  assert.notEqual(run(trace, 48000, 420).closedAtBlock, -1, "armed: segments on its own short pause")
  assert.equal(run(trace, 48000, 5000).closedAtBlock, -1, "in conversation: still listening")
})

test("the trailing pause is trimmed off before transcription, but not to the last syllable", () => {
  // 5s of silence on every clip would dominate the audio Whisper sees, costing time on a slow
  // CPU and inviting it to hallucinate text to fill the gap. Trimming ALL of it is the opposite
  // failure: the clip then ends on the final word and clips it.
  const r = run([...quiet(900), ...loud(1200), ...quiet(7000)])
  assert.ok(r.sentMs > 0, "still sends")
  assert.ok(r.sentMs < 1200 + KEEP_TAIL_MS + PREROLL_MS + 400,
    `clip is ${r.sentMs.toFixed(0)}ms — the 5s pause was not trimmed`)
  assert.ok(r.sentMs > 1200 + PREROLL_MS + 100,
    `clip is ${r.sentMs.toFixed(0)}ms — barely more than the speech, so no tail was kept`)
})
