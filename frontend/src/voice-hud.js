/**
 * voice-hud.js — the reactor face on the live voice page.
 *
 * Two looks, and which one is showing is *information*, not decoration:
 *
 *   CYAN HUD    the system is attending to YOU — idle, armed, listening, hearing. Concentric
 *               instrument rings, a tick bezel, segmented blocks, and an amber arc that is a real
 *               level meter: it tracks microphone amplitude, so a dead mic leaves it flat.
 *   GOLD BURST  the system is working or talking — thinking, speaking. A volumetric spray of fine
 *               radial filaments whose energy rides Piper's output level.
 *
 * Anyone glancing at the screen can tell who currently holds the conversation from colour alone,
 * before reading a word of the label underneath.
 *
 * ## Why it is built this way
 *
 * This canvas shares the main thread with a ScriptProcessorNode running the VAD ~43 times a second.
 * Starving that callback doesn't drop frames, it drops *audio* — so the render is written to be
 * cheap and, above all, allocation-free per frame:
 *
 *   - Ring geometry and the tick bezel are trigonometry that never changes, so they are rasterised
 *     once into an offscreen canvas and thereafter blitted. Only the parts that actually move are
 *     drawn per frame.
 *   - The particle pool is allocated once and recycled in place; no object churn, so no GC pauses
 *     landing in the middle of an utterance.
 *   - devicePixelRatio is capped at 2. A 3x phone would otherwise rasterise 2.25x the pixels for a
 *     difference nobody can see on a glow.
 *
 * `reduce` (the app's "Reduce effects" preference, or prefers-reduced-motion) thins the particle
 * count and drops the soft glows rather than freezing the display: the amplitude readout is the
 * point of the screen and must survive.
 */

export const CYAN = "103, 199, 235"
export const AMBER = "251, 202, 3"
export const GOLD = "255, 176, 46"

/** Phases where Jarvis is producing rather than receiving — the gold half of the design. */
const BURST_PHASES = new Set(["thinking", "speaking"])

export function isBurstPhase(phase) { return BURST_PHASES.has(phase) }

/**
 * How lit the face is, 0..1. `off` is dim-but-present (the instrument is powered, not running);
 * `loading` sits between so the warm-up reads as progress rather than a stall.
 */
export function phaseIntensity(phase) {
  if (phase === "off") return 0.28
  if (phase === "loading") return 0.6
  if (phase === "armed") return 0.72
  return 1
}

/**
 * The tick bezel: `count` marks around the circle, every `majorEvery`-th one long.
 * Returned as flat cos/sin pairs so the draw loop never calls Math.cos again.
 */
export function tickGeometry(count, majorEvery = 5) {
  const cos = new Float32Array(count)
  const sin = new Float32Array(count)
  const major = new Uint8Array(count)
  for (let i = 0; i < count; i++) {
    const a = (i / count) * Math.PI * 2 - Math.PI / 2
    cos[i] = Math.cos(a); sin[i] = Math.sin(a)
    major[i] = i % majorEvery === 0 ? 1 : 0
  }
  return { count, cos, sin, major }
}

/**
 * Where a segmented block ring's gaps fall: `n` blocks each spanning `fill` of its slot.
 * Returns [startAngle, endAngle] pairs in radians.
 */
export function blockSegments(n, fill = 0.72) {
  const slot = (Math.PI * 2) / n
  const out = []
  for (let i = 0; i < n; i++) out.push([i * slot, i * slot + slot * fill])
  return out
}

/**
 * One filament of the gold burst, as a direction on the unit sphere.
 *
 * Sampled properly: `uz` uniform in [-1,1] with a uniform azimuth gives points spread evenly over
 * the surface. (Picking a latitude angle uniformly instead — the obvious version — crowds the poles
 * and the sphere visibly clumps at top and bottom.) The vector is then projected orthographically,
 * so the silhouette is a true circle and `uz` is free to carry depth as brightness.
 */
export function makeParticle(rand) {
  return { ...respawn({}, rand), r: rand(), speed: 0.2 + rand() * 0.55, seed: rand() }
}

function respawn(p, rand) {
  const uz = rand() * 2 - 1
  const k = Math.sqrt(Math.max(0, 1 - uz * uz))
  const az = rand() * Math.PI * 2
  p.ux = Math.cos(az) * k
  p.uy = Math.sin(az) * k
  p.uz = uz
  p.len = 0.05 + rand() * 0.13      // short: the sphere is built from density, not long rays
  return p
}

/**
 * Advance one filament. Recycles at the far edge rather than reallocating, and returns the same
 * object so the caller's pool never changes shape.
 *
 * `energy` (0..1) both speeds the flight and biases respawn inward, so a loud reply visibly blooms
 * outward instead of merely brightening.
 */
export function stepParticle(p, dt, energy, rand) {
  p.r += p.speed * dt * (0.35 + energy * 1.5)
  if (p.r > 1) {
    p.r -= 1
    respawn(p, rand)
  }
  return p
}

/** A tiny deterministic PRNG — same face every run, and testable. */
export function mulberry32(seed) {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

/** Milliseconds spent since the timestamp the frame started with. */
function elapsed(t0) {
  return (typeof performance !== "undefined" ? performance.now() : Date.now()) - t0
}

const TICKS = tickGeometry(120, 5)
const BLOCKS = blockSegments(18, 0.62)
const INNER_BLOCKS = blockSegments(36, 0.5)

/**
 * Rasterise everything that never moves — bezel, fine rings, block rings — once per size.
 * Drawn at dpr scale so the blit is 1:1 and stays crisp.
 */
function buildStatic(size, dpr) {
  const c = document.createElement("canvas")
  c.width = Math.max(1, Math.round(size * dpr))
  c.height = Math.max(1, Math.round(size * dpr))
  const g = c.getContext("2d")
  g.setTransform(dpr, 0, 0, dpr, 0, 0)
  const cx = size / 2, cy = size / 2
  const R = size * 0.5

  // Tick bezel — the dense graduations that make it read as an instrument.
  for (let i = 0; i < TICKS.count; i++) {
    const long = TICKS.major[i]
    const r1 = R * 0.895
    const r2 = R * (long ? 0.945 : 0.925)
    g.strokeStyle = `rgba(${CYAN}, ${long ? 0.5 : 0.22})`
    g.lineWidth = long ? Math.max(1, size * 0.004) : Math.max(0.5, size * 0.002)
    g.beginPath()
    g.moveTo(cx + TICKS.cos[i] * r1, cy + TICKS.sin[i] * r1)
    g.lineTo(cx + TICKS.cos[i] * r2, cy + TICKS.sin[i] * r2)
    g.stroke()
  }

  // Hairline rings. Varying weights keep it from looking like a target.
  for (const [rr, alpha, w] of [[0.955, 0.30, 1], [0.88, 0.16, 1], [0.60, 0.14, 1], [0.35, 0.10, 1]]) {
    g.strokeStyle = `rgba(${CYAN}, ${alpha})`
    g.lineWidth = Math.max(0.5, size * 0.0016 * w)
    g.beginPath(); g.arc(cx, cy, R * rr, 0, Math.PI * 2); g.stroke()
  }

  // Fine inner graticule — the schematic texture behind the core in the reference.
  g.strokeStyle = `rgba(${CYAN}, 0.07)`
  g.lineWidth = Math.max(0.5, size * 0.0014)
  for (let i = 0; i < 12; i++) {
    const a = (i / 12) * Math.PI * 2
    g.beginPath()
    g.moveTo(cx + Math.cos(a) * R * 0.36, cy + Math.sin(a) * R * 0.36)
    g.lineTo(cx + Math.cos(a) * R * 0.585, cy + Math.sin(a) * R * 0.585)
    g.stroke()
  }
  return c
}

/**
 * Create the visualizer bound to a canvas.
 *
 * @returns {{draw: (state: {phase: string, amp: number, now: number}) => void, dispose: () => void}}
 */
export function createHud(canvas, { reduce = false, seed = 7 } = {}) {
  const ctx = canvas.getContext("2d")
  const rand = mulberry32(seed)
  const pool = []
  // Density is the whole effect: the reference sphere is hundreds of fine filaments, not dozens of
  // long ones. Each is a single 2-point stroke, so this stays cheap even at full count.
  const POOL = reduce ? 150 : 460
  for (let i = 0; i < POOL; i++) pool.push(makeParticle(rand))

  let statics = null, staticSize = 0, staticDpr = 0
  let smooth = 0, lit = 0, burst = 0, last = 0
  // Adaptive filament budget. The VAD callback shares this thread, and starving it drops AUDIO
  // rather than frames — so instead of picking a particle count that's safe on the slowest
  // plausible device, measure the real cost here and give ground when it gets expensive. Recovery
  // is slower than the cut, so a single hitch doesn't permanently thin the sphere.
  const FLOOR = Math.min(60, POOL)
  let active = POOL, cost = 0

  const draw = ({ phase, amp, now }) => {
    const t0 = now
    const dpr = Math.min(2, window.devicePixelRatio || 1)
    const size = Math.min(canvas.clientWidth, canvas.clientHeight)
    if (!size) return
    if (canvas.width !== Math.round(size * dpr)) {
      canvas.width = Math.round(size * dpr)
      canvas.height = Math.round(size * dpr)
      statics = null
    }
    if (!statics || staticSize !== size || staticDpr !== dpr) {
      statics = buildStatic(size, dpr); staticSize = size; staticDpr = dpr
    }
    const dt = last ? Math.min(0.05, (now - last) / 1000) : 0.016   // clamp: a backgrounded tab
    last = now                                                      // must not teleport the burst

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, size, size)

    const cx = size / 2, cy = size / 2, R = size * 0.5
    smooth += (amp - smooth) * 0.22
    const a = Math.max(0, Math.min(1, smooth))
    const targetLit = phaseIntensity(phase)
    lit += (targetLit - lit) * 0.08
    // Cross-fade the two identities rather than cutting, so "thinking" blooms out of the HUD
    // instead of replacing it — the transition is most of what sells the effect.
    burst += ((isBurstPhase(phase) ? 1 : 0) - burst) * 0.07
    const spin = now / 1000

    ctx.globalCompositeOperation = "lighter"   // additive: overlapping glows accumulate like light

    // ---- gold burst ---------------------------------------------------------------------------
    if (burst > 0.01) {
      const energy = phase === "speaking" ? a : 0.35 + Math.sin(spin * 2.2) * 0.12
      const shell = R * 0.62
      const spinC = Math.cos(spin * 0.22), spinS = Math.sin(spin * 0.22)
      ctx.lineCap = "round"
      for (let i = 0; i < active; i++) {
        const p = stepParticle(pool[i], dt, energy, rand)
        // Spin the direction about the Y axis, then project orthographically: screen position is
        // just (ux, uy) scaled. A true circular silhouette falls out, and uz is left carrying depth.
        const ux = p.ux * spinC + p.uz * spinS
        const uz = p.uz * spinC - p.ux * spinS
        // Concentrated near the shell rather than spread along the whole radius — density at one
        // radius is what makes a spray of lines read as a solid sphere instead of a starburst.
        const rr = shell * (0.72 + p.r * 0.34)
        const x1 = cx + ux * rr, y1 = cy + p.uy * rr
        const r2 = rr * (1 + p.len)
        const x2 = cx + ux * r2, y2 = cy + p.uy * r2
        // Depth cue: filaments on the near face are brighter than those going away from us, which
        // is most of what separates a sphere from a disc.
        const depth = 0.45 + 0.55 * (uz * 0.5 + 0.5)
        const fade = Math.sin(Math.min(1, p.r) * Math.PI)
        ctx.strokeStyle = `rgba(${GOLD}, ${0.62 * fade * depth * burst * (0.55 + energy * 0.8)})`
        ctx.lineWidth = Math.max(0.5, size * (0.0008 + p.seed * 0.0016))
        ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke()
      }
      if (!reduce) {
        // Hot core, falling away fast — the sphere should look lit from inside.
        const gl = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * 0.78)
        gl.addColorStop(0, `rgba(255, 236, 190, ${0.5 * burst})`)
        gl.addColorStop(0.22, `rgba(${GOLD}, ${0.22 * burst})`)
        gl.addColorStop(1, `rgba(${GOLD}, 0)`)
        ctx.fillStyle = gl
        ctx.beginPath(); ctx.arc(cx, cy, R * 0.78, 0, Math.PI * 2); ctx.fill()
      }
      // A slim equator, tilted with the spin — a hint of the orbital plane rather than a hoop
      // sitting in front of the sphere.
      ctx.strokeStyle = `rgba(${GOLD}, ${0.30 * burst})`
      ctx.lineWidth = Math.max(0.8, size * 0.0018)
      ctx.beginPath()
      ctx.ellipse(cx, cy, shell * 1.08, shell * 0.30, Math.sin(spin * 0.18) * 0.25, 0, Math.PI * 2)
      ctx.stroke()
    }

    // ---- cyan HUD -----------------------------------------------------------------------------
    // Dimmed under the burst, never removed. Cross-fading it away left the speaking state a bare
    // spray on black — the instrument housing is what the sphere needs to sit *inside*, and losing
    // it made the busiest moment of the conversation look like the emptiest.
    const hud = (1 - burst * 0.62) * lit
    if (hud > 0.01) {
      ctx.globalAlpha = hud
      ctx.drawImage(statics, 0, 0, size, size)
      ctx.globalAlpha = 1

      // Segmented block rings, counter-rotating — the chunky arcs of the reference bezel.
      for (const [ring, segs, rr, w, speed, dir] of [
        ["outer", BLOCKS, 0.815, 0.030, 0.06, 1],
        ["inner", INNER_BLOCKS, 0.655, 0.014, 0.11, -1],
      ]) {
        ctx.strokeStyle = `rgba(${CYAN}, ${(ring === "outer" ? 0.34 : 0.20) * hud})`
        ctx.lineWidth = size * w
        for (const [s, e] of segs) {
          const off = spin * speed * dir
          ctx.beginPath(); ctx.arc(cx, cy, R * rr, s + off, e + off); ctx.stroke()
        }
      }

      // Amber level meter — the bright accent arc, and a real readout: it is microphone amplitude.
      const meterA = -Math.PI * 0.75, sweep = Math.PI * 1.05
      ctx.strokeStyle = `rgba(${CYAN}, ${0.13 * hud})`
      ctx.lineWidth = size * 0.016
      ctx.beginPath(); ctx.arc(cx, cy, R * 0.885, meterA, meterA + sweep); ctx.stroke()
      if (a > 0.005) {
        ctx.strokeStyle = `rgba(${AMBER}, ${0.92 * hud})`
        ctx.lineWidth = size * 0.016
        ctx.beginPath(); ctx.arc(cx, cy, R * 0.885, meterA, meterA + sweep * a); ctx.stroke()
      }

      // Indicator dots, lighting up in sequence with level — ref 1's row of yellow pips.
      for (let i = 0; i < 6; i++) {
        const ang = -Math.PI * 0.30 + i * 0.075
        const on = a > (i + 0.6) / 7
        ctx.fillStyle = `rgba(${on ? AMBER : CYAN}, ${(on ? 0.95 : 0.20) * hud})`
        ctx.beginPath()
        ctx.arc(cx + Math.cos(ang) * R * 0.885, cy + Math.sin(ang) * R * 0.885,
                Math.max(1, size * 0.006), 0, Math.PI * 2)
        ctx.fill()
      }

      // Sweep hand — slow when idle, urgent while actually hearing speech.
      const sweepSpeed = phase === "hearing" ? 1.6 : 0.35
      const ha = spin * sweepSpeed
      const grad = ctx.createLinearGradient(cx, cy, cx + Math.cos(ha) * R * 0.8, cy + Math.sin(ha) * R * 0.8)
      grad.addColorStop(0, `rgba(${CYAN}, 0)`)
      grad.addColorStop(1, `rgba(${CYAN}, ${0.5 * hud})`)
      ctx.strokeStyle = grad
      ctx.lineWidth = Math.max(1, size * 0.004)
      ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(cx + Math.cos(ha) * R * 0.8, cy + Math.sin(ha) * R * 0.8); ctx.stroke()

      // Core — the part that breathes with your voice.
      const coreR = R * (0.10 + a * 0.075)
      if (!reduce) {
        const gl = ctx.createRadialGradient(cx, cy, 0, cx, cy, coreR * 4)
        gl.addColorStop(0, `rgba(${CYAN}, ${0.55 * hud})`)
        gl.addColorStop(1, `rgba(${CYAN}, 0)`)
        ctx.fillStyle = gl
        ctx.beginPath(); ctx.arc(cx, cy, coreR * 4, 0, Math.PI * 2); ctx.fill()
      }
      ctx.fillStyle = `rgba(${CYAN}, ${0.9 * hud})`
      ctx.beginPath(); ctx.arc(cx, cy, coreR, 0, Math.PI * 2); ctx.fill()

      // Expanding echo rings, emitted on loud moments — visible proof the mic is live.
      for (let i = 0; i < 3; i++) {
        const ph = (spin * 0.5 + i / 3) % 1
        const rr = R * (0.2 + ph * 0.62)
        ctx.strokeStyle = `rgba(${CYAN}, ${(1 - ph) * 0.3 * a * hud})`
        ctx.lineWidth = Math.max(1, size * 0.0035)
        ctx.beginPath(); ctx.arc(cx, cy, rr, 0, Math.PI * 2); ctx.stroke()
      }
    }

    ctx.globalCompositeOperation = "source-over"

    // Only meaningful while the burst is actually drawing — measuring an idle HUD frame would
    // "prove" there is headroom that vanishes the moment Jarvis speaks.
    if (burst > 0.5) {
      cost += (elapsed(t0) - cost) * 0.1
      if (cost > 9 && active > FLOOR) active = Math.max(FLOOR, Math.round(active * 0.85))
      else if (cost < 4.5 && active < POOL) active = Math.min(POOL, active + 4)
    }
  }

  return {
    draw,
    dispose: () => { statics = null; pool.length = 0 },
    /** Exposed for diagnostics — how far the budget has had to give ground on this device. */
    stats: () => ({ active, pool: POOL, cost }),
  }
}
