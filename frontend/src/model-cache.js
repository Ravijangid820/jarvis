/**
 * model-cache.js — keep model weights in Cache Storage so they are fetched once, ever.
 *
 * transformers.js already does this for the Whisper checkpoint. The wake-word and face models do
 * not go through transformers.js — they are loaded by URL through onnxruntime-web or plain fetch —
 * so they were left relying on the HTTP cache. That mostly works, and "mostly" is the problem: the
 * HTTP cache is evictable under pressure, revalidates on reload (a round trip before a byte is
 * read), and is bypassed entirely by a hard refresh. Cache Storage is explicit and durable, so a
 * second visit does no network work at all and the models keep working offline.
 *
 * Safe because these URLs are content-pinned: the orchestrator serves them from SHA-256-verified
 * files under a versioned cache name. Changing a model means bumping CACHE_NAME, which drops the
 * old entries wholesale rather than leaving a stale weight file to be matched forever.
 */

const CACHE_NAME = "jarvis-models-v1"

/** Cache Storage needs a secure context; plain HTTP on a LAN IP has no `caches`. */
function available() {
  return typeof caches !== "undefined" && caches?.open
}

/** Delete every cache this app owns except the current one — how a model bump takes effect. */
export async function purgeStaleModelCaches() {
  if (!available()) return
  try {
    const names = await caches.keys()
    await Promise.all(names.filter(n => n.startsWith("jarvis-models-") && n !== CACHE_NAME)
      .map(n => caches.delete(n)))
  } catch { /* eviction is best-effort; a stale cache costs space, not correctness */ }
}

/**
 * Fetch a model file, preferring the durable cache.
 *
 * @param {string} url
 * @param {(loaded: number, total: number) => void} [onProgress] reports only on a real download —
 *        a cache hit is instantaneous and firing progress for it would render a pointless 0→100%.
 * @returns {Promise<ArrayBuffer>}
 */
export async function fetchModel(url, onProgress) {
  if (available()) {
    try {
      const cache = await caches.open(CACHE_NAME)
      const hit = await cache.match(url)
      if (hit) return await hit.arrayBuffer()
    } catch { /* fall through to the network */ }
  }

  const res = await fetch(url)
  if (!res.ok) throw new Error(`${url} unavailable (HTTP ${res.status})`)

  // Read the body ourselves so download progress is reportable, then hand the cache a fresh
  // Response built from the bytes — res.body is already consumed by then, and cache.put on a
  // consumed Response stores nothing.
  const total = Number(res.headers.get("content-length") || 0)
  const reader = res.body?.getReader()
  let buf
  if (!reader) {
    buf = await res.arrayBuffer()
  } else {
    const chunks = []
    let loaded = 0
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      chunks.push(value)
      loaded += value.length
      onProgress?.(loaded, total)
    }
    const out = new Uint8Array(loaded)
    let off = 0
    for (const c of chunks) { out.set(c, off); off += c.length }
    buf = out.buffer
  }

  if (available()) {
    try {
      const cache = await caches.open(CACHE_NAME)
      await cache.put(url, new Response(buf.slice(0), {
        headers: { "Content-Type": "application/octet-stream", "Content-Length": String(buf.byteLength) },
      }))
    } catch { /* over quota / private mode — the model still works, it just reloads next time */ }
  }
  return buf
}
