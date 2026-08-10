import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { subscribe, getSnapshot, dismissToast, settleDialog } from './notify.js'

// Renderer for the in-app notification store (notify.js). Mounted once, at the app root.

const TOAST_ICON = { info: "▸", success: "✓", warn: "!", error: "✕" }

function DialogBox({ dialog }) {
  const [value, setValue] = useState(dialog.defaultValue ?? "")
  const inputRef = useRef(null)
  const confirmRef = useRef(null)
  const isPrompt = dialog.type === "prompt"

  const accept = () => settleDialog(isPrompt ? value : true)
  const cancel = () => settleDialog(isPrompt ? null : false)

  useEffect(() => {
    if (isPrompt) { inputRef.current?.focus(); inputRef.current?.select() }
    else confirmRef.current?.focus()
  }, [isPrompt])

  // Escape cancels from anywhere, including while focus sits on the backdrop. Captured so it
  // resolves the dialog before the command palette's own Escape handling sees the key.
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); cancel() } }
    window.addEventListener("keydown", onKey, true)
    return () => window.removeEventListener("keydown", onKey, true)
  })   // no dep array: the handler closes over the dialog currently on screen

  return (
    <div className="app-dialog-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) cancel() }}>
      <div className="app-dialog" role="dialog" aria-modal="true" aria-label={dialog.title || "Confirm"}>
        <div className="app-dialog-title">{dialog.title || (isPrompt ? "Input required" : "Confirm")}</div>
        <div className="app-dialog-msg">{dialog.message}</div>
        {isPrompt && (
          <input
            ref={inputRef}
            className="hud-input app-dialog-input"
            value={value}
            placeholder={dialog.placeholder || ""}
            onChange={e => setValue(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); accept() } }}
          />
        )}
        <div className="app-dialog-actions">
          <button type="button" className="hud-btn" onClick={cancel}>{dialog.cancelLabel || "Cancel"}</button>
          <button type="button" ref={confirmRef} className={`hud-btn ${dialog.danger ? "danger" : "primary"}`} onClick={accept}>
            {dialog.confirmLabel || (isPrompt ? "Save" : "Confirm")}
          </button>
        </div>
      </div>
    </div>
  )
}

/** Renders the toast stack and the active dialog. */
export default function NotifyHost() {
  const snap = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  return (
    <>
      <div className="toast-stack" role="status" aria-live="polite">
        {snap.toasts.map(t => (
          <div key={t.id} className={`app-toast toast-${t.kind}`}>
            <span className="toast-icon" aria-hidden="true">{TOAST_ICON[t.kind] || TOAST_ICON.info}</span>
            <div className="toast-text">
              {t.title && <strong className="toast-title">{t.title}</strong>}
              <span>{t.text}</span>
            </div>
            <button type="button" className="toast-close" onClick={() => dismissToast(t.id)} aria-label="Dismiss">×</button>
          </div>
        ))}
      </div>
      {snap.dialog && <DialogBox key={snap.dialog.id} dialog={snap.dialog} />}
    </>
  )
}
