/**
 * App-owned toasts and dialogs — the replacement for window.alert/confirm/prompt.
 *
 * The native dialogs are browser chrome: unstyled, positioned by the browser, and blocking on
 * the main thread, which stalls a streaming reply and its per-sentence TTS for as long as the
 * box is up. These render inside the HUD instead, so they inherit the active theme, and the
 * confirm/prompt pair returns a promise rather than freezing the tab.
 *
 * The API is imperative rather than a React context because the callers are plain async
 * handlers spread across App and Admin — `await confirmDialog(...)` is a one-line swap for
 * `confirm(...)`, with no provider to thread through. This module is the store; NotifyHost.jsx
 * renders it and is mounted once, at the root.
 */

let state = { toasts: [], dialog: null }
let dialogQueue = []           // dialogs are modal: extras wait their turn instead of stacking
let nextId = 1
const listeners = new Set()

export const subscribe = (fn) => { listeners.add(fn); return () => listeners.delete(fn) }
export const getSnapshot = () => state

const commit = (next) => { state = next; for (const fn of listeners) fn() }

/** Remove a toast by id (no-op if it already timed out or was dismissed). */
export function dismissToast(id) {
  if (!state.toasts.some(t => t.id === id)) return
  commit({ ...state, toasts: state.toasts.filter(t => t.id !== id) })
}

/**
 * Show a transient toast. `opts` may be a kind string ("info" | "success" | "warn" | "error")
 * or an object { kind, title, duration }. duration 0 keeps it up until dismissed.
 * Returns the toast id.
 */
export function notify(text, opts = {}) {
  const o = typeof opts === "string" ? { kind: opts } : opts
  const kind = o.kind || "info"
  const id = nextId++
  const ms = o.duration ?? (kind === "error" ? 8000 : 4500)
  commit({ ...state, toasts: [...state.toasts, { id, kind, title: o.title || "", text: String(text ?? "") }] })
  if (ms > 0) setTimeout(() => dismissToast(id), ms)
  return id
}

export const notifyError = (text, opts = {}) => notify(text, { ...opts, kind: "error" })
export const notifyOk = (text, opts = {}) => notify(text, { ...opts, kind: "success" })

function pushDialog(spec) {
  return new Promise(resolve => {
    dialogQueue.push({ ...spec, id: nextId++, resolve })
    if (!state.dialog) commit({ ...state, dialog: dialogQueue[0] })
  })
}

/** Resolve the open dialog and promote the next one in the queue. */
export function settleDialog(value) {
  const current = dialogQueue.shift()
  commit({ ...state, dialog: dialogQueue[0] || null })
  current?.resolve(value)
}

/**
 * Ask for confirmation. Resolves true on confirm, false on cancel/Escape/backdrop.
 * opts: { title, confirmLabel, cancelLabel, danger }.
 */
export const confirmDialog = (message, opts = {}) => pushDialog({ ...opts, type: "confirm", message })

/**
 * Ask for a line of text. Resolves the string on submit, or null on cancel — same contract as
 * window.prompt, so `if (!value) return` guards carry over unchanged.
 * opts: { title, confirmLabel, cancelLabel, placeholder }.
 */
export const promptDialog = (message, defaultValue = "", opts = {}) =>
  pushDialog({ ...opts, type: "prompt", message, defaultValue })
