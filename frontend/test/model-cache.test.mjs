/**
 * model-cache — fetch a model once, ever.
 *
 * The behaviour worth pinning is what happens on the *second* visit and on the unhappy paths: a hit
 * must do no network work at all, a cache that refuses to store must not break loading, and a
 * version bump must actually evict the old weights rather than leave them matched forever.
 */
import assert from "node:assert/strict"
import test from "node:test"

import { fetchModel, purgeStaleModelCaches } from "../src/model-cache.js"

/** A Cache Storage double. `puts` records what was stored; `broken` makes put() throw. */
function fakeCaches({ broken = false } = {}) {
  const stores = new Map()
  const api = {
    _deleted: [],
    async open(name) {
      if (!stores.has(name)) stores.set(name, new Map())
      const m = stores.get(name)
      return {
        async match(url) {
          const buf = m.get(url)
          return buf ? { arrayBuffer: async () => buf } : undefined
        },
        async put(url, res) {
          if (broken) throw new Error("QuotaExceededError")
          m.set(url, await res.arrayBuffer())
        },
      }
    },
    async keys() { return [...stores.keys()] },
    async delete(name) { api._deleted.push(name); return stores.delete(name) },
    _stores: stores,
  }
  return api
}

function bytes(n, fill = 7) { return new Uint8Array(new Array(n).fill(fill)) }

/** A fetch double that counts calls and streams the body in two chunks. */
function fakeFetch(payload, { ok = true, status = 200 } = {}) {
  const f = async () => {
    f.calls++
    const half = Math.ceil(payload.length / 2)
    let i = 0
    return {
      ok, status,
      headers: { get: (k) => (k === "content-length" ? String(payload.length) : null) },
      body: {
        getReader: () => ({
          read: async () => {
            if (i >= payload.length) return { done: true }
            const chunk = payload.subarray(i, i + half)
            i += half
            return { done: false, value: chunk }
          },
        }),
      },
      arrayBuffer: async () => payload.buffer,
    }
  }
  f.calls = 0
  return f
}

test("a second load is served from the cache with no network call", async () => {
  globalThis.caches = fakeCaches()
  const payload = bytes(64)
  globalThis.fetch = fakeFetch(payload)

  const first = await fetchModel("/wake-models/a.onnx")
  assert.equal(globalThis.fetch.calls, 1)
  assert.equal(new Uint8Array(first).length, 64)

  const second = await fetchModel("/wake-models/a.onnx")
  assert.equal(globalThis.fetch.calls, 1, "the whole point: no second request")
  assert.deepEqual(new Uint8Array(second), new Uint8Array(first))
})

test("progress is reported while downloading, and not on a cache hit", async () => {
  globalThis.caches = fakeCaches()
  globalThis.fetch = fakeFetch(bytes(100))

  const seen = []
  await fetchModel("/wake-models/b.onnx", (loaded, total) => seen.push([loaded, total]))
  assert.ok(seen.length > 0, "a real download must be visible")
  assert.equal(seen.at(-1)[0], 100)
  assert.equal(seen.at(-1)[1], 100)

  const cached = []
  await fetchModel("/wake-models/b.onnx", (l, t) => cached.push([l, t]))
  // A 0→100% flash for a load that took no time reads as a download that didn't happen.
  assert.deepEqual(cached, [], "a cache hit must not animate progress")
})

test("a cache that refuses to store still yields a working model", async () => {
  globalThis.caches = fakeCaches({ broken: true })
  const payload = bytes(32, 3)
  globalThis.fetch = fakeFetch(payload)
  const buf = await fetchModel("/wake-models/c.onnx")
  assert.deepEqual(new Uint8Array(buf), payload, "over quota must degrade to plain fetching")
})

test("no Cache Storage at all (insecure context) still loads", async () => {
  delete globalThis.caches
  const payload = bytes(16, 5)
  globalThis.fetch = fakeFetch(payload)
  const buf = await fetchModel("/wake-models/d.onnx")
  assert.deepEqual(new Uint8Array(buf), payload)
})

test("an HTTP error is raised, not cached as a model", async () => {
  globalThis.caches = fakeCaches()
  globalThis.fetch = fakeFetch(bytes(8), { ok: false, status: 404 })
  await assert.rejects(() => fetchModel("/wake-models/missing.onnx"), /HTTP 404/)
  const store = globalThis.caches._stores.get("jarvis-models-v1")
  assert.ok(!store || !store.has("/wake-models/missing.onnx"),
    "a 404 body must never be stored — it would be served as weights forever after")
})

test("a version bump evicts the previous cache and leaves the current one", async () => {
  const c = fakeCaches()
  globalThis.caches = c
  await c.open("jarvis-models-v0")
  await c.open("jarvis-models-v1")
  await c.open("some-other-app-cache")
  await purgeStaleModelCaches()
  assert.deepEqual(c._deleted, ["jarvis-models-v0"])
})
