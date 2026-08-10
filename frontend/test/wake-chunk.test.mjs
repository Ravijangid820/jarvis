// The wake detector is stateful and needs EXACTLY 1280-sample chunks; the mic delivers whatever
// block size the audio graph happens to use. If the chunker drops or duplicates a sample at a
// boundary, scores degrade quietly rather than failing — so pin it.
import { test } from "node:test"
import assert from "node:assert"
import { createChunker, CHUNK } from "../src/wake-detect.js"

/** Collect the chunks a sequence of blocks produces. */
function feed(blockSizes) {
  const seen = []
  const push = createChunker((c) => { seen.push(c); return null })
  let n = 0
  for (const size of blockSizes) {
    const b = new Float32Array(size)
    for (let i = 0; i < size; i++) b[i] = n++       // a strictly increasing ramp
    push(b)
  }
  return { seen, total: n }
}

test("chunks are always exactly CHUNK samples", () => {
  const { seen } = feed([2048, 2048, 2048, 2048])
  assert.ok(seen.length > 0)
  for (const c of seen) assert.equal(c.length, CHUNK)
})

test("no sample is dropped or repeated across block boundaries", () => {
  // 2048 does not divide 1280, so every chunk after the first straddles a block edge.
  const { seen } = feed([2048, 2048, 2048, 2048, 2048])
  const flat = []
  for (const c of seen) flat.push(...c)
  for (let i = 0; i < flat.length; i++) {
    assert.equal(flat[i], i, `sample ${i} is ${flat[i]} — the stream is misaligned`)
  }
})

test("ragged block sizes still produce a contiguous stream", () => {
  const { seen } = feed([100, 4096, 7, 1, 900, 3000, 333])
  const flat = []
  for (const c of seen) flat.push(...c)
  for (let i = 0; i < flat.length; i++) assert.equal(flat[i], i)
})

test("a partial tail is held back, not emitted short or padded", () => {
  const { seen, total } = feed([CHUNK + 5])
  assert.equal(seen.length, 1)
  assert.equal(seen[0].length, CHUNK)
  assert.ok(total > CHUNK, "the leftover 5 samples must wait for the next block")
})

test("blocks smaller than a chunk accumulate until one is complete", () => {
  const { seen } = feed(Array(13).fill(100))     // 1300 samples total
  assert.equal(seen.length, 1, "1300 samples is exactly one chunk plus a remainder")
  assert.equal(seen[0][0], 0)
  assert.equal(seen[0][CHUNK - 1], CHUNK - 1)
})
