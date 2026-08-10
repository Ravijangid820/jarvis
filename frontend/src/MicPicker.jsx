/**
 * MicPicker.jsx — one list of every microphone the user could speak into.
 *
 * Deliberately mixes two kinds of hardware in a single list, because the question a person is
 * actually asking is "which mic is nearest me?" and the answer doesn't care which machine the cable
 * goes into. Inputs on this device come from the browser; inputs on the Jarvis box come from
 * /voice/inputs and are streamed here as audio when chosen.
 *
 * Whatever is picked, transcription still happens in this tab — choosing the server's microphone
 * moves where the sound is captured, not where it is understood.
 */
import { useCallback, useEffect, useState } from "react"
import {
  getMicSource, labelsHidden, listMics, micLabel, setMicSource, unlockMicLabels,
} from "./mic-select.js"

export default function MicPicker({ token, apiBase, onClose, onChange }) {
  const [mics, setMics] = useState([])
  const [serverMics, setServerMics] = useState([])
  const [serverErr, setServerErr] = useState("")
  const [serverBusy, setServerBusy] = useState(false)
  const [chosen, setChosen] = useState(() => getMicSource())
  const [needsUnlock, setNeedsUnlock] = useState(false)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const local = await listMics()
      setMics(local)
      setNeedsUnlock(labelsHidden(local))
    } catch { setMics([]) }
    try {
      const res = await fetch(apiBase + "/voice/inputs", { headers: { Authorization: "Bearer " + token } })
      if (res.ok) {
        const d = await res.json()
        setServerMics(d.inputs || [])
        setServerErr(d.error || "")
        setServerBusy(Boolean(d.busy))
      } else {
        // 403 in a demo household is expected, not a fault worth shouting about.
        setServerMics([])
        setServerErr(res.status === 403 ? "" : `Could not reach the server's microphones (HTTP ${res.status})`)
      }
    } catch { setServerErr("Could not reach the server's microphones") }
    setLoading(false)
  }, [apiBase, token])

  useEffect(() => { load() }, [load])

  // Plugging a headset in while the panel is open should just work.
  useEffect(() => {
    const md = navigator.mediaDevices
    if (!md?.addEventListener) return
    const onChangeDevices = () => load()
    md.addEventListener("devicechange", onChangeDevices)
    return () => md.removeEventListener("devicechange", onChangeDevices)
  }, [load])

  const unlock = async () => { await unlockMicLabels(); await load() }

  const pick = (kind, deviceId) => {
    setMicSource(kind, deviceId)
    const next = getMicSource()
    setChosen(next)
    onChange?.(next)
  }

  const isChosen = (kind, id) => (kind === "server"
    ? chosen.kind === "server" && chosen.serverDevice === id
    : chosen.kind === "browser" && (chosen.deviceId || "") === (id || ""))

  return (
    <div className="jarvis-modal-backdrop" onClick={onClose}>
      <div className="jarvis-modal model-modal" onClick={e => e.stopPropagation()}>
        <div className="jm-header">
          <h3>🎙 Microphone</h3>
          <button type="button" className="jm-close" onClick={onClose}>×</button>
        </div>
        <div className="jm-body">
          <p className="jm-desc">
            Where your voice is picked up. Pick whichever microphone is closest to you — a good mic
            on the server beats this device's built-in one if you're sitting next to the server, and
            vice versa. Either way your speech is transcribed here in this tab, never uploaded.
          </p>

          {loading && <div className="model-empty">Looking for microphones…</div>}

          {!loading && (
            <>
              <div className="jm-subhead">This device</div>
              <div className="model-grid">
                <div className={`model-card ${isChosen("browser", "") ? "active" : ""}`}
                     onClick={() => pick("browser", "")}>
                  <div className="model-card-top">
                    <strong>Automatic</strong>
                    {isChosen("browser", "") && <span className="model-active-badge">Active</span>}
                  </div>
                  <div className="model-card-meta">Whatever this device normally uses</div>
                </div>
                {mics.filter(m => m.deviceId && m.deviceId !== "default").map((m, i) => (
                  <div key={m.deviceId}
                       className={`model-card ${isChosen("browser", m.deviceId) ? "active" : ""}`}
                       onClick={() => pick("browser", m.deviceId)}>
                    <div className="model-card-top">
                      <strong>{micLabel(m, i)}</strong>
                      {isChosen("browser", m.deviceId) && <span className="model-active-badge">Active</span>}
                    </div>
                    <div className="model-card-meta">On this device</div>
                  </div>
                ))}
              </div>
              {needsUnlock && (
                <p className="jm-desc">
                  Your browser hides microphone names until it has been given access once.{" "}
                  <button type="button" className="adm-link" onClick={unlock}>Show names</button>
                </p>
              )}

              <div className="jm-subhead">On the Jarvis server</div>
              {serverMics.length === 0 ? (
                <div className="model-empty">
                  {serverErr || "No microphone is attached to the server."}
                </div>
              ) : (
                <div className="model-grid">
                  {serverMics.map(s => (
                    <div key={s.device}
                         className={`model-card ${isChosen("server", s.device) ? "active" : ""}`}
                         onClick={() => pick("server", s.device)}>
                      <div className="model-card-top">
                        <strong>{s.name}</strong>
                        {isChosen("server", s.device) && <span className="model-active-badge">Active</span>}
                      </div>
                      <div className="model-card-meta">{s.card} · on the server</div>
                    </div>
                  ))}
                </div>
              )}
              {serverBusy && (
                <div className="model-switching-status">
                  ⚠ The server microphone is currently in use by another session.
                </div>
              )}
              {serverMics.length > 0 && (
                <p className="jm-desc">
                  Only one session can use the server's microphone at a time, and the always-on
                  wake-word listener holds it if that's running.
                </p>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
