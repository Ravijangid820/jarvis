import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './fonts/fonts.css'
import './index.css'
import App from './App.jsx'
import NotifyHost from './NotifyHost.jsx'
import { purgeStaleModelCaches } from './model-cache.js'

/**
 * GitHub Pages SPA redirect handler (companion to public/404.html).
 *
 * Pages has no server-side rewrite, so 404.html encodes the requested path into the query
 * string and bounces to index.html; this decodes it back into a real URL. It lives here rather
 * than as an inline <script> in index.html because our CSP is script-src 'self' — an inline
 * script is blocked outright, so the shim never ran when served by the orchestrator and logged
 * a CSP violation on every page load. As the first statement of the entry module it still runs
 * before React reads the location, which is all the ordering this needs.
 */
function applyPagesRedirect(l) {
  if (l.search[1] !== '/') return
  const decoded = l.search.slice(1).split('&').map(s => s.replace(/~and~/g, '&')).join('?')
  window.history.replaceState(null, '', l.pathname.slice(0, -1) + decoded + l.hash)
}
applyPagesRedirect(window.location)

// Drop model caches from an older CACHE_NAME. Idempotent, off the critical path, and the only
// thing that makes bumping a model version actually take effect on a returning visitor.
purgeStaleModelCaches()

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
    <NotifyHost />
  </StrictMode>,
)
