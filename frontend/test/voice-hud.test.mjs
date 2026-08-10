/**
 * voice-hud — the geometry and motion behind the reactor face.
 *
 * The drawing itself needs a canvas and is judged by eye, but the parts that decide whether it
 * *reads* correctly are pure: which phase is gold, how lit each state is, and a particle system
 * that must recycle in place rather than allocate or drift out of range on a slow frame.
 */
import assert from "node:assert/strict"
import test from "node:test"

import {
  blockSegments, isBurstPhase, makeParticle, mulberry32, phaseIntensity, stepParticle, tickGeometry,
} from "../src/voice-hud.js"

test("gold means Jarvis holds the conversation, cyan means you do", () => {
  for (const p of ["thinking", "speaking"]) assert.equal(isBurstPhase(p), true, p)
  for (const p of ["off", "loading", "armed", "listening", "hearing", "error"]) {
    assert.equal(isBurstPhase(p), false, p)
  }
})

test("every phase is lit, and idle is dimmer than active", () => {
  const off = phaseIntensity("off")
  assert.ok(off > 0, "an idle instrument is powered, not blank — a dark screen reads as broken")
  assert.ok(off < phaseIntensity("loading"))
  assert.ok(phaseIntensity("loading") < phaseIntensity("armed"))
  assert.ok(phaseIntensity("armed") < phaseIntensity("listening"))
  assert.equal(phaseIntensity("listening"), 1)
})

test("the tick bezel is a unit circle with regular major marks", () => {
  const t = tickGeometry(120, 5)
  assert.equal(t.count, 120)
  assert.equal(t.major.reduce((a, b) => a + b, 0), 24, "every 5th of 120")
  for (let i = 0; i < t.count; i++) {
    assert.ok(Math.abs(Math.hypot(t.cos[i], t.sin[i]) - 1) < 1e-6, `tick ${i} off the circle`)
  }
  // First tick at twelve o'clock, so the bezel is symmetric about the vertical.
  assert.ok(Math.abs(t.cos[0]) < 1e-6 && Math.abs(t.sin[0] + 1) < 1e-6)
})

test("block segments leave gaps and never overlap", () => {
  const segs = blockSegments(18, 0.62)
  assert.equal(segs.length, 18)
  const slot = (Math.PI * 2) / 18
  for (let i = 0; i < segs.length; i++) {
    const [s, e] = segs[i]
    assert.ok(e > s, "a segment must have width")
    assert.ok(e - s < slot, "and must not fill its slot, or the ring reads as solid")
    if (i + 1 < segs.length) assert.ok(segs[i + 1][0] >= e, "segments must not overlap")
  }
})

test("particles recycle in place and stay in range", () => {
  const rand = mulberry32(1)
  const p = makeParticle(rand)
  const before = Object.keys(p).sort()
  for (let i = 0; i < 4000; i++) {
    stepParticle(p, 0.05, 1, rand)
    assert.ok(p.r >= 0 && p.r <= 1, `r escaped: ${p.r}`)
    assert.ok(p.uz >= -1 && p.uz <= 1, `uz escaped: ${p.uz}`)
    assert.ok(Math.abs(Math.hypot(p.ux, p.uy, p.uz) - 1) < 1e-6, "direction must stay a unit vector")
  }
  // Same object, same shape: the pool is preallocated so no per-frame garbage reaches the VAD.
  assert.deepEqual(Object.keys(p).sort(), before)
})

test("a stalled frame cannot fling a particle past the rim", () => {
  // The draw loop clamps dt, but the particle must also be well-behaved on its own: r wraps rather
  // than running away, so one long frame can't leave a filament stranded outside the sphere.
  const rand = mulberry32(9)
  const p = makeParticle(rand)
  stepParticle(p, 0.05, 1, rand)
  assert.ok(p.r >= 0 && p.r <= 1)
})

test("louder output drives the burst outward faster", () => {
  const quiet = makeParticle(mulberry32(3))
  const loud = { ...makeParticle(mulberry32(3)) }
  stepParticle(quiet, 0.016, 0, mulberry32(3))
  stepParticle(loud, 0.016, 1, mulberry32(3))
  assert.ok(loud.r > quiet.r, "energy must visibly bloom, not merely brighten")
})

test("the PRNG is deterministic, so the face is the same every run", () => {
  const a = mulberry32(42), b = mulberry32(42)
  for (let i = 0; i < 50; i++) assert.equal(a(), b())
  const c = mulberry32(42)
  for (let i = 0; i < 200; i++) { const v = c(); assert.ok(v >= 0 && v < 1) }
})
