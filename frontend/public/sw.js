// Minimal service worker — makes Jarvis installable (PWA) and gives an offline shell, WITHOUT
// stale-cache risk: it's network-first (always fresh when online) and only falls back to the last
// cached page when offline.
const CACHE = "jarvis-shell-v1"

self.addEventListener("install", () => self.skipWaiting())
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()))

self.addEventListener("fetch", (e) => {
  const req = e.request
  if (req.method !== "GET") return
  e.respondWith(
    fetch(req)
      .then((res) => {
        if (req.mode === "navigate") {
          const copy = res.clone()
          caches.open(CACHE).then((c) => c.put("/", copy)).catch(() => {})
        }
        return res
      })
      .catch(() => (req.mode === "navigate" ? caches.match("/") : Promise.reject(new Error("offline"))))
  )
})
