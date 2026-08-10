/**
 * FaceEnroll.jsx — enroll and recognise a face from the browser webcam.
 *
 * Everything visual happens on-device: frames go from the <video> into a canvas, then to a Web
 * Worker that detects, aligns and embeds them with the same YuNet + SFace models the Raspberry Pi
 * agent uses. Only the final 128-D vector is POSTed. No image is ever uploaded, written to disk, or
 * sent to the LLM — which is what makes it defensible to offer this to the public at all.
 *
 * Recognition asks the SERVER who a vector belongs to (/faces/identify) rather than downloading the
 * household's enrolled set and comparing here. Both would work; only one of them keeps other
 * people's face templates out of this page. A template is enough to replay someone's identity, so
 * the browser gets an answer, never the material to compute one.
 *
 * Consent is a hard gate, not a notice: the camera does not start until the visitor has read what
 * is stored and pressed the button. A face embedding is biometric data, and a member of the public
 * trying a demo has not agreed to anything by merely arriving.
 */
import { useCallback, useEffect, useRef, useState } from "react"
import { overlayRect } from "./face-detect.js"

// How many good frames make an enrollment. The agent captures several too: one frame is one pose
// under one lighting condition, and matching gets markedly more robust with a handful.
const CAPTURE_TARGET = 5
const FRAME_INTERVAL_MS = 400        // ~2.5 fps: plenty for enrollment, gentle on a laptop CPU
// Recognition runs slower than detection: the box should track your face at 2.5 fps, but the NAME
// on it costs a round trip and rarely changes, so it is asked for at a fraction of that rate — and
// once the answer is known, at a fraction of THAT. The server rate-limits each user to 120 requests
// a minute; asking every 1.2s while someone simply stands in frame would spend 50 of them restating
// a name that has not changed, leaving little headroom for the rest of the app.
const IDENTIFY_INTERVAL_MS = 1200        // still working out who this is
const IDENTIFY_SETTLED_MS = 5000         // a confident match — just confirm it occasionally

export default function FaceEnroll({ token, apiBase, onClose }) {
  const [stage, setStage] = useState("consent")   // consent | loading | live
  const [error, setError] = useState("")
  const [progress, setProgress] = useState(null)  // model download
  const [captured, setCaptured] = useState([])
  const [detection, setDetection] = useState(null)
  const [match, setMatch] = useState(null)
  const [name, setName] = useState("")
  const [people, setPeople] = useState([])        // who is enrolled here (names only)
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState("")

  const videoRef = useRef(null)
  const canvasRef = useRef(null)
  const workerRef = useRef(null)
  const streamRef = useRef(null)
  const inflightRef = useRef(false)
  const identifyBusyRef = useRef(false)
  const lastIdentifyRef = useRef(0)
  const settledRef = useRef(false)          // a confident match is on screen → poll lazily
  const frameSizeRef = useRef({ w: 1, h: 1 })

  const authed = useCallback((path, opts = {}) => fetch(apiBase + path, {
    ...opts,
    headers: { "Content-Type": "application/json", Authorization: "Bearer " + token, ...(opts.headers || {}) },
  }), [apiBase, token])

  /** Everything that must stop when this panel goes away: the camera light must go out the moment
   *  the visitor closes the panel, not whenever React happens to collect it. */
  const teardown = useCallback(() => {
    streamRef.current?.getTracks().forEach(t => t.stop())
    streamRef.current = null
    workerRef.current?.terminate()
    workerRef.current = null
    if (videoRef.current) videoRef.current.srcObject = null
  }, [])

  useEffect(() => teardown, [teardown])

  /** The names enrolled in this household — shown so "unrecognised" is distinguishable from
   *  "nobody is enrolled yet". Names only; no vectors are fetched. */
  const loadPeople = useCallback(async () => {
    try {
      const res = await authed("/admin/faces")
      if (res.ok) setPeople(((await res.json()).faces || []).map(f => f.name))
    } catch { /* the list is context, not function — recognition works without it */ }
  }, [authed])

  useEffect(() => { if (token) loadPeople() }, [token, loadPeople])

  /** Ask the server who this embedding belongs to. One vector out, {name, score} back.
   *  One request in flight at a time: on a slow link the answers would otherwise queue up and the
   *  label would lag further behind the face it belongs to with every frame. */
  const identify = useCallback(async (embedding) => {
    if (identifyBusyRef.current) return
    identifyBusyRef.current = true
    try {
      const res = await authed("/faces/identify", {
        method: "POST", body: JSON.stringify({ embedding }),
      })
      if (res.ok) {
        const found = await res.json()
        setMatch(found)
        settledRef.current = Boolean(found.name) && found.name !== "unknown"
      }
    } catch { /* hold the last label rather than flickering on one dropped request */ }
    finally { identifyBusyRef.current = false }
  }, [authed])

  const start = async () => {
    setError("")
    setStage("loading")
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 640 }, height: { ideal: 480 }, facingMode: "user" }, audio: false,
      })
      streamRef.current = stream
      if (videoRef.current) {
        videoRef.current.srcObject = stream
        await videoRef.current.play().catch(() => {})
      }
    } catch (e) {
      setError(e?.name === "NotAllowedError"
        ? "Camera permission was declined — nothing was captured."
        : `Could not open the camera: ${e?.message || e}`)
      setStage("consent")
      return
    }

    const worker = new Worker(new URL("./face-worker.js", import.meta.url), { type: "module" })
    workerRef.current = worker
    worker.onmessage = (e) => {
      const m = e.data || {}
      if (m.type === "progress") {
        setProgress(m.total ? { stage: m.stage, pct: Math.round(m.loaded / m.total * 100) } : null)
      } else if (m.type === "ready") {
        setProgress(null)
        setStage("live")
      } else if (m.type === "faces") {
        inflightRef.current = false
        const face = (m.faces || [])[0] || null
        // Carry the dimensions of the frame this detection came FROM. The overlay is positioned in
        // percentages of those, and reading the canvas ref at render time would both violate the
        // rules of hooks and silently use a stale size if the camera resolution changed.
        setDetection(face ? { ...face, frameW: frameSizeRef.current.w, frameH: frameSizeRef.current.h } : null)
        if (!face) {
          setMatch(null)
          settledRef.current = false        // they left frame; the next face is not assumed to be them
        } else {
          const due = settledRef.current ? IDENTIFY_SETTLED_MS : IDENTIFY_INTERVAL_MS
          if (Date.now() - lastIdentifyRef.current >= due) {
            lastIdentifyRef.current = Date.now()
            identify(face.embedding)        // async; the label updates when the answer lands
          }
        }
      } else if (m.type === "error") {
        inflightRef.current = false
        setError(m.error)
      }
    }
    worker.postMessage({ type: "load" })
  }

  // Frame pump. Deliberately one-in-flight-at-a-time: queueing frames on a 2011-era CPU would
  // build an unbounded backlog and make the preview lag further and further behind reality.
  useEffect(() => {
    if (stage !== "live") return
    const id = setInterval(() => {
      const video = videoRef.current, canvas = canvasRef.current, worker = workerRef.current
      if (!video || !canvas || !worker || inflightRef.current) return
      if (!video.videoWidth) return
      canvas.width = video.videoWidth
      canvas.height = video.videoHeight
      const ctx = canvas.getContext("2d", { willReadFrequently: true })
      ctx.drawImage(video, 0, 0)
      const frame = ctx.getImageData(0, 0, canvas.width, canvas.height)
      frameSizeRef.current = { w: canvas.width, h: canvas.height }
      inflightRef.current = true
      // Transfer the buffer rather than copying it — a 640x480 RGBA frame is 1.2 MB, several times
      // a second.
      worker.postMessage({ type: "detect", rgba: frame.data, width: canvas.width, height: canvas.height },
        [frame.data.buffer])
    }, FRAME_INTERVAL_MS)
    return () => clearInterval(id)
  }, [stage])

  const capture = () => {
    if (!detection) return
    setSaved("")
    setCaptured(prev => prev.length >= CAPTURE_TARGET ? prev : [...prev, detection.embedding])
  }

  const save = async () => {
    const person = name.trim()
    if (!person || captured.length === 0) return
    setBusy(true)
    setError("")
    try {
      for (let i = 0; i < captured.length; i++) {
        const res = await authed("/faces/enroll", {
          method: "POST",
          // replace=true only on the FIRST vector: it clears any previous set for this person, and
          // the rest then append. Sending it every time would leave exactly one embedding.
          body: JSON.stringify({ name: person, embedding: captured[i], source: "browser", replace: i === 0 }),
        })
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`)
      }
      await loadPeople()
      setCaptured([])
      lastIdentifyRef.current = 0        // re-identify on the next frame, not up to a second later
      // Deliberately NOT switching to a "done" screen: the payoff of this feature is enrolling and
      // then immediately seeing your own name on the live box, so detection keeps running.
      setSaved(person)
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  /** Delete every person this household has enrolled. The visible counterpart to the consent gate:
   *  having agreed to store a vector, the visitor must be able to withdraw it immediately rather
   *  than wait for the session to expire. */
  const deleteAll = async () => {
    setBusy(true)
    try {
      const res = await authed("/admin/faces")
      const { faces } = res.ok ? await res.json() : { faces: [] }
      for (const f of faces || []) await authed(`/admin/faces/${f.id}`, { method: "DELETE" })
      await loadPeople()
      setCaptured([])
      setMatch(null)
      settledRef.current = false         // the match that settled it no longer exists
      lastIdentifyRef.current = 0
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  const close = () => { teardown(); onClose?.() }

  return (
    <div className="jarvis-modal-overlay" onClick={close}>
      <div className="jarvis-modal face-modal" onClick={e => e.stopPropagation()}>
        <div className="jm-header">
          <h3>Face Recognition</h3>
          <button className="jm-close" onClick={close}>✕</button>
        </div>

        {stage === "consent" && (
          <div className="face-consent">
            <p className="jm-desc">
              This runs entirely in your browser. Your camera image is analysed on this device and
              <strong> never uploaded</strong> — no photo or video is sent to the server or stored anywhere.
            </p>
            <ul className="face-facts">
              <li><strong>What is sent:</strong> a list of 128 numbers describing the geometry of your face. It cannot be turned back into a picture of you.</li>
              <li><strong>How long it is kept:</strong> until you delete it, log out, or the session expires — whichever comes first.</li>
              <li><strong>Who can see it:</strong> only this sandbox. It is not shared with any other session.</li>
              <li><strong>Download:</strong> starting requires a one-off ~38 MB model download.</li>
            </ul>
            <p className="face-consent-ask">Only continue if you're happy with the above.</p>
            <button className="login-btn" onClick={start}>Enable camera</button>
          </div>
        )}

        {stage === "loading" && (
          <div className="face-loading">
            <p>{progress ? `Downloading the ${progress.stage} model… ${progress.pct}%` : "Starting the camera…"}</p>
            {progress && <div className="face-progress"><div style={{ width: `${progress.pct}%` }} /></div>}
          </div>
        )}

        <div className="face-stage" style={{ display: stage === "live" ? "block" : "none" }}>
          {/* The viewport takes its aspect ratio from the ACTUAL frame, not a hardcoded 4:3.
              Cameras often hand back 16:9 even when 640x480 is requested as "ideal", and a
              mismatched box would crop the video — at which point the percentage overlay below
              would no longer line up with what is on screen. */}
          <div className="face-viewport"
               style={{ aspectRatio: `${detection?.frameW || 4} / ${detection?.frameH || 3}` }}>
            <video ref={videoRef} playsInline muted className="face-video" />
            {detection && (
              // Percentages so the box tracks the video at any size, with no second canvas to keep
              // in sync with CSS layout. The LEFT edge is mirrored because the video is displayed
              // flipped (a selfie view is what people expect) while the detection coordinates come
              // from the unflipped frame. The box is a sibling of the <video>, so the video's own
              // transform does not move it — the mirroring has to happen here.
              <div className="face-box"
                   style={overlayRect(detection.box, detection.frameW, detection.frameH)}>
                <span className="face-box-label">
                  {match && match.name && match.name !== "unknown"
                    ? `${match.name} · ${match.score.toFixed(2)}`
                    : people.length ? "unrecognised" : "face detected"}
                </span>
              </div>
            )}
          </div>
          <canvas ref={canvasRef} style={{ display: "none" }} />

          <div className="face-controls">
            <input className="hud-input" value={name} onChange={e => setName(e.target.value)}
                   placeholder="Your name (or a nickname)" maxLength={64} />
            <button className="hud-btn" onClick={capture} disabled={!detection || captured.length >= CAPTURE_TARGET}>
              Capture {captured.length}/{CAPTURE_TARGET}
            </button>
            <button className="login-btn face-save" onClick={save}
                    disabled={busy || !name.trim() || captured.length === 0}>
              {busy ? "Saving…" : "Save face"}
            </button>
          </div>
          <p className="face-hint">
            {saved && <span className="face-saved">Saved “{saved}”. Look at the camera — you should see your name on the box.</span>}
            {!saved && !detection && "Looking for a face — make sure you're lit from the front."}
            {!saved && detection && captured.length < CAPTURE_TARGET &&
              "Capture a few frames, turning your head slightly between each — varied angles recognise far better."}
            {!saved && detection && captured.length >= CAPTURE_TARGET && "That's plenty. Add a name and save."}
          </p>
        </div>

        {people.length > 0 && (
          <div className="face-enrolled">
            <div className="face-enrolled-head">
              <span>Enrolled here: {people.join(", ")}</span>
              <button className="hud-btn danger" onClick={deleteAll} disabled={busy}>
                Delete my face data
              </button>
            </div>
          </div>
        )}

        {error && <div className="face-error">{error}</div>}
      </div>
    </div>
  )
}
