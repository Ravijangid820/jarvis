/**
 * stt-worker — the model is loaded once per page, not once per use.
 *
 * This is the module that makes "Preparing model…" a first-run event rather than a permanent tax,
 * so the things worth pinning are: a single worker, a single load no matter how many callers pile
 * on, an instant resolve once warm, and replies routed to the right caller (two components share
 * this worker, so a broadcast would deliver one user's transcript to the other's handler).
 */
import assert from "node:assert/strict"
import test from "node:test"

const workers = []
/** transcribeAudio awaits ensureStt() before posting, so the post lands a microtask later. */
const settle = () => new Promise(r => setTimeout(r, 0))

/** A Worker double standing in for whisper-worker.js. */
class FakeWorker {
  constructor() {
    this.posted = []
    this.listeners = new Set()
    this.terminated = false
    workers.push(this)
  }
  addEventListener(_t, fn) { this.listeners.add(fn) }
  removeEventListener(_t, fn) { this.listeners.delete(fn) }
  postMessage(msg) { this.posted.push(msg) }
  terminate() { this.terminated = true }
  /** Simulate the worker replying. */
  emit(data) { for (const fn of [...this.listeners]) fn({ data }) }
}
globalThis.Worker = FakeWorker
globalThis.URL = globalThis.URL

const { ensureStt, isSttWarm, transcribeAudio, releaseStt, sttSource } =
  await import("../src/stt-worker.js")

test.afterEach(() => { releaseStt(); workers.length = 0 })

test("concurrent callers share one worker and one load", async () => {
  const a = ensureStt()
  const b = ensureStt()
  assert.equal(workers.length, 1, "the chat mic and the voice page must not build two")
  assert.equal(workers[0].posted.filter(m => m.type === "load").length, 1,
    "and must not ask it to load twice")
  workers[0].emit({ type: "ready", source: "official" })
  assert.deepEqual((await a).source, "official")
  assert.equal((await b).cached, false)
})

test("once warm, ensureStt resolves instantly and reports it was cached", async () => {
  const p = ensureStt()
  workers[0].emit({ type: "ready", source: "failsafe" })
  await p
  assert.equal(isSttWarm(), true)
  assert.equal(sttSource(), "failsafe")

  const again = await ensureStt()
  // `cached` is what lets the UI skip its loading state entirely rather than flash it.
  assert.equal(again.cached, true)
  assert.equal(workers.length, 1, "no second worker")
})

test("progress reaches the caller that asked for it, and stops after ready", async () => {
  const seen = []
  const p = ensureStt(m => seen.push(m.type))
  workers[0].emit({ type: "progress", progress: 42, loaded: 42, total: 100 })
  workers[0].emit({ type: "status", phase: "preparing" })
  workers[0].emit({ type: "ready", source: "official" })
  await p
  assert.deepEqual(seen, ["progress", "status"])

  workers[0].emit({ type: "progress", progress: 99 })
  assert.deepEqual(seen, ["progress", "status"], "listener must be unsubscribed once loaded")
})

test("results are routed by id, not broadcast to every caller", async () => {
  const p = ensureStt()
  workers[0].emit({ type: "ready", source: "official" })
  await p

  const first = transcribeAudio(new Float32Array(8))
  const second = transcribeAudio(new Float32Array(8))
  await settle()
  const ids = workers[0].posted.filter(m => m.type === "transcribe").map(m => m.id)
  assert.equal(new Set(ids).size, 2, "each request needs its own id")

  // Answer out of order — exactly what happens when two components are both listening.
  workers[0].emit({ type: "result", text: "second one", id: ids[1] })
  workers[0].emit({ type: "result", text: "first one", id: ids[0] })
  assert.equal(await first, "first one")
  assert.equal(await second, "second one")
})

test("a transcription error rejects only its own request", async () => {
  const p = ensureStt()
  workers[0].emit({ type: "ready", source: "official" })
  await p

  const ok = transcribeAudio(new Float32Array(4))
  const bad = transcribeAudio(new Float32Array(4))
  await settle()
  const ids = workers[0].posted.filter(m => m.type === "transcribe").map(m => m.id)
  workers[0].emit({ type: "error", error: "decode failed", id: ids[1] })
  workers[0].emit({ type: "result", text: "fine", id: ids[0] })
  await assert.rejects(() => bad, /decode failed/)
  assert.equal(await ok, "fine")
})

test("a failed load can be retried rather than rejecting forever", async () => {
  const p = ensureStt()
  workers[0].emit({ type: "error", error: "network down" })   // no id => it's the load failing
  await assert.rejects(() => p, /network down/)
  assert.equal(isSttWarm(), false)

  const retry = ensureStt()
  const w = workers[workers.length - 1]
  w.emit({ type: "ready", source: "official" })
  assert.equal((await retry).source, "official", "a retry must actually retry")
})

test("releaseStt tears down and fails anything in flight", async () => {
  const p = ensureStt()
  workers[0].emit({ type: "ready", source: "official" })
  await p
  const inflight = transcribeAudio(new Float32Array(4))
  await settle()
  releaseStt()
  assert.equal(workers[0].terminated, true)
  assert.equal(isSttWarm(), false)
  await assert.rejects(() => inflight, /stopped/)
})
