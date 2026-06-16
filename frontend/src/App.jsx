import React, { useState, useEffect, useRef } from 'react'
import './index.css'

const API = ""

function App() {
  const [token, setToken] = useState(localStorage.getItem("jarvis_token"))
  const [role, setRole] = useState(localStorage.getItem("jarvis_role") || "user")
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [loginError, setLoginError] = useState("")
  const [loginStatus, setLoginStatus] = useState("Initialize")

  const [sessions, setSessions] = useState([])
  const [currentSessionId, setCurrentSessionId] = useState("default")
  const [currentTitle, setCurrentTitle] = useState("New Session")
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState("")
  const [processing, setProcessing] = useState(false)
  const [speed, setSpeed] = useState("")

  // Server Status
  const [isOnline, setIsOnline] = useState(false)
  const [modelName, setModelName] = useState("—")
  const [uplink, setUplink] = useState("N/A")

  // Sidebar: `open` drives the mobile slide-in drawer; `collapsed` hides it on desktop.
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  // The ☰ button means different things per layout: drawer toggle on mobile,
  // collapse/expand on desktop (where the sidebar is docked, not an overlay).
  const toggleSidebar = () => {
    if (window.innerWidth <= 768) setSidebarOpen(o => !o)
    else setSidebarCollapsed(c => !c)
  }

  // Parameters
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [themeOpen, setThemeOpen] = useState(false)   // theme picker collapsed by default
  const [temp, setTemp] = useState(0.7)
  const [topK, setTopK] = useState(40)
  const [topP, setTopP] = useState(0.9)
  const [minP, setMinP] = useState(0.05)
  const [repeatPenalty, setRepeatPenalty] = useState(1.1)
  const [presencePenalty, setPresencePenalty] = useState(0.0)
  const [freqPenalty, setFreqPenalty] = useState(0.0)
  const [nPredict, setNPredict] = useState(1024)
  const [seed, setSeed] = useState(-1)
  const [voice, setVoice] = useState(false)
  const [sysPrompt, setSysPrompt] = useState("")

  // Cinematic boot sequence — shown once per browser session, click to skip.
  const [booting, setBooting] = useState(() => !sessionStorage.getItem("jarvis_booted"))
  const skipBoot = () => {
    sessionStorage.setItem("jarvis_booted", "1")
    setBooting(false)
  }

  // Telemetry strip (real data): tok/s history (sparkline), live uptime, boot progress.
  const [tokHistory, setTokHistory] = useState([])
  const [uptime, setUptime] = useState(0)
  const [bootPct, setBootPct] = useState(0)

  // Command palette (⌘K / Ctrl+K).
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [paletteQuery, setPaletteQuery] = useState("")
  const [paletteIndex, setPaletteIndex] = useState(0)

  // Theme switcher + synthesized UI sound (both persisted, sound off by default).
  const [theme, setTheme] = useState(() => localStorage.getItem("jarvis_theme") || "stark")
  const [sound, setSound] = useState(() => localStorage.getItem("jarvis_sound") === "1")
  const audioCtxRef = useRef(null)

  const messagesEndRef = useRef(null)
  const inputRef = useRef(null)
  const abortRef = useRef(null)   // AbortController for the in-flight /chat/stream request
  const messagesContainerRef = useRef(null)
  const sessionStartRef = useRef(Date.now())
  const paletteInputRef = useRef(null)
  // Whether the chat is "pinned" to the bottom. Auto-scroll only happens while
  // pinned, so streaming never yanks the user back down when they scroll up to read.
  const stickToBottomRef = useRef(true)
  const prevMsgCountRef = useRef(0)

  const onMessagesScroll = () => {
    const el = messagesContainerRef.current
    if (!el) return
    stickToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120
  }

  // --- Initialization ---
  useEffect(() => {
    if (token) {
      checkHealth()
      loadSessions()
      loadHistory("default")
    }
  }, [token])

  useEffect(() => {
    if (!booting) return
    const t = setTimeout(() => {
      sessionStorage.setItem("jarvis_booted", "1")
      setBooting(false)
    }, 2400)
    return () => clearTimeout(t)
  }, [booting])

  useEffect(() => {
    const el = messagesContainerRef.current
    if (!el) return
    // A new message (you sent one, or a session loaded) re-pins to the bottom;
    // streaming-token updates keep the count steady and only scroll if still pinned.
    const newMessage = messages.length !== prevMsgCountRef.current
    prevMsgCountRef.current = messages.length
    if (newMessage) {
      // You sent a message or switched session — snap instantly to the latest.
      stickToBottomRef.current = true
      el.scrollTop = el.scrollHeight
      return
    }
    if (!stickToBottomRef.current) return
    // Streaming-token updates: instant so per-token scrolls don't fight a smooth
    // animation; smooth only for the small settle once the reply finishes.
    el.scrollTo({ top: el.scrollHeight, behavior: processing ? "auto" : "smooth" })
  }, [messages, processing])

  // Live uptime ticker (drives the telemetry readout).
  useEffect(() => {
    const id = setInterval(() => setUptime(Math.floor((Date.now() - sessionStartRef.current) / 1000)), 1000)
    return () => clearInterval(id)
  }, [])

  // Boot progress counter, synced to the ~2.1s boot bar.
  useEffect(() => {
    if (!booting) return
    const start = Date.now()
    const id = setInterval(() => {
      const pct = Math.min(100, Math.round((Date.now() - start) / 2100 * 100))
      setBootPct(pct)
      if (pct >= 100) clearInterval(id)
    }, 60)
    return () => clearInterval(id)
  }, [booting])

  // Pointer-reactive parallax/tilt (desktop only). Publishes cursor offset (-1..1)
  // as --mx/--my CSS vars; the stylesheet maps them to subtle depth transforms.
  useEffect(() => {
    if (window.matchMedia("(hover: none)").matches) return
    let raf = 0
    const onMove = (e) => {
      if (raf) return
      raf = requestAnimationFrame(() => {
        raf = 0
        const root = document.documentElement
        root.style.setProperty("--mx", ((e.clientX / window.innerWidth - 0.5) * 2).toFixed(3))
        root.style.setProperty("--my", ((e.clientY / window.innerHeight - 0.5) * 2).toFixed(3))
      })
    }
    window.addEventListener("pointermove", onMove, { passive: true })
    return () => { window.removeEventListener("pointermove", onMove); if (raf) cancelAnimationFrame(raf) }
  }, [])

  // Command palette: ⌘K / Ctrl+K toggles it; Escape closes.
  useEffect(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault()
        setPaletteQuery(""); setPaletteIndex(0); setPaletteOpen(o => !o)
      } else if (e.key === "Escape") {
        setPaletteOpen(false)
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [])

  useEffect(() => { if (paletteOpen) paletteInputRef.current?.focus() }, [paletteOpen])

  // Theme: a global hue/saturation tint applied via [data-theme] on <html>.
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme)
    localStorage.setItem("jarvis_theme", theme)
  }, [theme])
  useEffect(() => { localStorage.setItem("jarvis_sound", sound ? "1" : "0") }, [sound])

  const checkHealth = async () => {
    try {
      const res = await fetch(API + "/health")
      if (res.ok) {
        const data = await res.json()
        setIsOnline(true)
        setModelName(data.model || "active")
        setUplink("Stable")
      } else {
        setIsOnline(false)
        setUplink("N/A")
      }
    } catch (e) {
      setIsOnline(false)
      setUplink("N/A")
    }
  }

  // --- Auth ---
  const doLogin = async () => {
    if (!username || !password) return
    try {
      setLoginError("")
      setLoginStatus("Connecting...")
      const res = await fetch(API + "/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password })
      })
      if (!res.ok) throw new Error("Invalid credentials")
      const data = await res.json()
      localStorage.setItem("jarvis_token", data.token)
      localStorage.setItem("jarvis_role", data.role)
      setToken(data.token)
      setRole(data.role)
      setUsername("")
      setPassword("")
    } catch (e) {
      setLoginError(e.message)
    } finally {
      setLoginStatus("Initialize")
    }
  }

  const doLogout = () => {
    // Revoke the token server-side (best-effort) before clearing it locally.
    if (token) {
      fetch(API + "/auth/logout", {
        method: "POST",
        headers: { "Authorization": "Bearer " + token }
      }).catch(() => {})
    }
    localStorage.removeItem("jarvis_token")
    localStorage.removeItem("jarvis_role")
    setToken(null)
    setSessions([])
    setMessages([])
    setCurrentSessionId("default")
  }

  // --- Sessions & History ---
  const loadSessions = async () => {
    try {
      const res = await fetch(API + "/sessions", { headers: { "Authorization": "Bearer " + token } })
      if (res.ok) {
        const data = await res.json()
        setSessions(data.sessions || [])
      } else if (res.status === 401 || res.status === 403) doLogout()
    } catch (e) {}
  }

  const loadHistory = async (sid) => {
    try {
      const res = await fetch(API + "/history/" + sid, { headers: { "Authorization": "Bearer " + token } })
      if (res.ok) {
        const data = await res.json()
        const mappedMsgs = (data.messages || []).map(m => ({...m, role: m.role === 'assistant' ? 'jarvis' : m.role}))
        setMessages(mappedMsgs)
        setCurrentSessionId(sid)
        if (sid === "default") setCurrentTitle("New Session")
        else {
          const s = sessions.find(x => x.id === sid)
          if (s) setCurrentTitle(s.title)
        }
        setSidebarOpen(false)
      }
    } catch (e) {}
  }

  const createSession = async () => {
    try {
      const res = await fetch(API + "/sessions", { method: "POST", headers: { "Authorization": "Bearer " + token } })
      if (res.ok) {
        const data = await res.json()
        setCurrentSessionId(data.id)
        setCurrentTitle(data.title)
        await loadSessions()
        await loadHistory(data.id)
      }
    } catch (e) {}
  }

  const renameSession = async (e, sid) => {
    e.stopPropagation()
    const newName = prompt("Enter new name:")
    if (!newName) return
    try {
      await fetch(API + "/sessions/" + sid, {
        method: "PUT",
        headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token },
        body: JSON.stringify({ title: newName })
      })
      if (sid === currentSessionId) setCurrentTitle(newName)
      loadSessions()
    } catch (e) {}
  }

  const deleteSession = async (e, sid) => {
    e.stopPropagation()
    if (!confirm("Delete this session?")) return
    try {
      await fetch(API + "/sessions/" + sid, { method: "DELETE", headers: { "Authorization": "Bearer " + token } })
      if (sid === currentSessionId) loadHistory("default")
      loadSessions()
    } catch (e) {}
  }

  // Abort the in-flight stream. Closing the connection also lets the server stop
  // the upstream LLM (its streaming generator is closed), freeing the model slot.
  const stopGeneration = () => {
    abortRef.current?.abort()
  }

  // --- Send Message ---
  const send = async (queryOverride) => {
    const userText = (queryOverride || input).trim()
    if (!userText || processing) return

    let sid = currentSessionId
    if (sid === "default") {
      try {
        const sRes = await fetch(API + "/sessions", { method: "POST", headers: { "Authorization": "Bearer " + token } })
        if (sRes.ok) {
          const sData = await sRes.json()
          sid = sData.id
          setCurrentSessionId(sid)
          setCurrentTitle(sData.title)
        }
      } catch (e) {}
    }

    if (!queryOverride) setInput("")
    setProcessing(true)
    setSpeed("")
    blip("send")
    
    setMessages(prev => [...prev, { role: "user", content: userText }])
    setMessages(prev => [...prev, { role: "jarvis", content: "", isStreaming: true }])

    const payload = {
      text: userText,
      session_id: sid,
      temperature: temp,
      top_k: topK, top_p: topP, min_p: minP,
      repeat_penalty: repeatPenalty, presence_penalty: presencePenalty, frequency_penalty: freqPenalty,
      // Guard against NaN from a cleared number field (parseInt("") === NaN).
      n_predict: Number.isFinite(nPredict) ? nPredict : undefined,
      seed: Number.isFinite(seed) ? seed : undefined,
      voice_feedback: voice,
      system_prompt: sysPrompt || undefined
    }

    const controller = new AbortController()
    abortRef.current = controller

    try {
      const startTime = performance.now()
      const res = await fetch(API + "/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token },
        body: JSON.stringify(payload),
        signal: controller.signal
      })

      if (!res.ok) {
        if (res.status === 401 || res.status === 403) doLogout()
        throw new Error("API Error")
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let answer = ""

      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        
        const chunk = decoder.decode(value, { stream: true })
        const lines = chunk.split("\n")
        
        for (const line of lines) {
          if (line.startsWith("data: ")) {
            const dataStr = line.slice(6)
            if (dataStr === "[DONE]") break
            try {
              const data = JSON.parse(dataStr)
              if (data.error && !data.done) {
                // Mid-stream backend error event — surface it instead of swallowing.
                answer = answer || "⚠️ The AI backend hit an error. Please try again."
              }
              if (data.content) {
                answer += data.content
                setMessages(prev => {
                  const newMsgs = [...prev]
                  newMsgs[newMsgs.length - 1] = { role: "jarvis", content: answer, isStreaming: true }
                  return newMsgs
                })
              }
              if (data.done) {
                blip("done")
                const wallTimeSecs = (performance.now() - startTime) / 1000
                // Honest, clearly-approximate estimate (~4 chars/token), guarded for div-by-zero.
                if (answer && wallTimeSecs > 0) {
                  const tps = (answer.length / 4) / wallTimeSecs
                  setSpeed(`~${tps.toFixed(1)} tok/s`)
                  setTokHistory(h => [...h, tps].slice(-24))   // feed the telemetry sparkline
                }
                const finalText = answer || "⚠️ No response was generated."
                setMessages(prev => {
                  const newMsgs = [...prev]
                  newMsgs[newMsgs.length - 1] = { role: "jarvis", content: finalText, isStreaming: false }
                  return newMsgs
                })
                if (data.new_title) {
                  setCurrentTitle(data.new_title)
                  loadSessions()
                }
                if (data.audio) {
                  try {
                    const audio = new Audio("data:audio/wav;base64," + data.audio)
                    audio.play().catch(() => {})
                  } catch (e) {}
                }
              }
            } catch (e) {}
          }
        }
      }
    } catch (e) {
      if (e.name === "AbortError") {
        // User pressed Stop — keep whatever streamed so far, just end the stream state.
        setMessages(prev => {
          const newMsgs = [...prev]
          const last = newMsgs[newMsgs.length - 1]
          if (last && last.role === "jarvis") {
            newMsgs[newMsgs.length - 1] = { ...last, content: last.content || "⏹ Generation stopped.", isStreaming: false }
          }
          return newMsgs
        })
      } else {
        console.error(e)
        // Replace the empty streaming placeholder with a visible error so the UI isn't stuck.
        setMessages(prev => {
          const newMsgs = [...prev]
          const last = newMsgs[newMsgs.length - 1]
          if (last && last.role === "jarvis" && !last.content) {
            newMsgs[newMsgs.length - 1] = { role: "jarvis", content: "⚠️ Could not reach Jarvis. Check the connection and try again.", isStreaming: false }
          }
          return newMsgs
        })
      }
    } finally {
      abortRef.current = null
      setProcessing(false)
      loadSessions()
      if (inputRef.current) inputRef.current.focus()
    }
  }

  // Synthesized UI blips (Web Audio — no files). Lazily created on first use, which
  // also satisfies the browser autoplay gesture requirement. No-op when sound is off.
  const blip = (kind) => {
    if (!sound) return
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext
      if (!audioCtxRef.current) audioCtxRef.current = new Ctx()
      const ctx = audioCtxRef.current
      const tones = { send: [520, 880], done: [660, 990], open: [440, 660] }[kind] || [600]
      const now = ctx.currentTime
      tones.forEach((f, i) => {
        const osc = ctx.createOscillator(), g = ctx.createGain()
        osc.type = "sine"; osc.frequency.value = f
        const t = now + i * 0.06
        g.gain.setValueAtTime(0, t)
        g.gain.linearRampToValueAtTime(0.05, t + 0.01)
        g.gain.exponentialRampToValueAtTime(0.0001, t + 0.12)
        osc.connect(g); g.connect(ctx.destination)
        osc.start(t); osc.stop(t + 0.13)
      })
    } catch { /* audio unavailable — ignore */ }
  }

  // --- Command palette actions ---
  const paletteActions = () => {
    const acts = [
      { tag: "NEW", label: "New session", run: () => createSession() },
      { tag: "VOX", label: `Voice output: ${voice ? "on" : "off"} — toggle`, run: () => setVoice(v => !v) },
      { tag: "CFG", label: `${advancedOpen ? "Hide" : "Show"} advanced parameters`, run: () => setAdvancedOpen(o => !o) },
      { tag: "IN", label: "Focus message input", run: () => inputRef.current?.focus() },
      { tag: "SND", label: `Sound: ${sound ? "on" : "off"} — toggle`, run: () => setSound(s => !s) },
      ...(role === "admin" ? [{ tag: "ADM", label: "Open admin console", run: () => { window.location.href = "/admin" } }] : []),
      { tag: "OUT", label: "Disconnect", run: () => doLogout() },
      ...sessions.map(s => ({ tag: "GO", label: `Go to: ${s.title}`, run: () => loadHistory(s.id) })),
    ]
    const q = paletteQuery.trim().toLowerCase()
    return q ? acts.filter(a => a.label.toLowerCase().includes(q)) : acts
  }

  const runPaletteItem = (item) => {
    setPaletteOpen(false); setPaletteQuery("")
    item?.run()
  }

  const onPaletteKey = (e, items) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setPaletteIndex(i => Math.min(i + 1, items.length - 1)) }
    else if (e.key === "ArrowUp") { e.preventDefault(); setPaletteIndex(i => Math.max(i - 1, 0)) }
    else if (e.key === "Enter") { e.preventDefault(); runPaletteItem(items[paletteIndex]) }
  }

  // Detailed arc reactor behind the chat: glowing gradient core, a ring of coil
  // segments, a tick-marked bezel, and counter-rotating detail rings.
  const renderChatReactor = () => (
    <div className="chat-reactor-bg" aria-hidden="true">
      <svg viewBox="0 0 400 400" width="580" height="580">
        <defs>
          <radialGradient id="reactorCore">
            <stop offset="0%" stopColor="#eafcff" stopOpacity="1" />
            <stop offset="38%" stopColor="#67C7EB" stopOpacity="0.85" />
            <stop offset="100%" stopColor="#67C7EB" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="reactorHalo">
            <stop offset="0%" stopColor="#67C7EB" stopOpacity="0.22" />
            <stop offset="100%" stopColor="#67C7EB" stopOpacity="0" />
          </radialGradient>
        </defs>
        <circle cx="200" cy="200" r="150" fill="url(#reactorHalo)" />
        {/* outer bezel + tick marks */}
        <circle cx="200" cy="200" r="192" fill="none" stroke="#67C7EB" strokeWidth="1" opacity="0.4" />
        <circle cx="200" cy="200" r="184" fill="none" stroke="#67C7EB" strokeWidth="0.5" opacity="0.3" />
        <g stroke="#67C7EB" opacity="0.55">
          {Array.from({ length: 72 }).map((_, i) => (
            <line key={i} x1="200" y1="14" x2="200" y2={i % 6 === 0 ? "26" : "20"}
              strokeWidth={i % 6 === 0 ? 1.4 : 0.6} transform={`rotate(${i * 5} 200 200)`} />
          ))}
        </g>
        {/* rotating dashed scan ring */}
        <circle cx="200" cy="200" r="160" fill="none" stroke="#67C7EB" strokeWidth="1.5" strokeDasharray="2 12" opacity="0.6">
          <animateTransform attributeName="transform" type="rotate" from="0 200 200" to="360 200 200" dur="60s" repeatCount="indefinite" />
        </circle>
        {/* signature coil-segment ring */}
        <g opacity="0.7">
          {Array.from({ length: 10 }).map((_, i) => (
            <g key={i} transform={`rotate(${i * 36} 200 200)`}>
              <rect x="188" y="78" width="24" height="46" rx="5" fill="rgba(103,199,235,0.06)" stroke="#67C7EB" strokeWidth="1.4" />
              <line x1="200" y1="84" x2="200" y2="118" stroke="#67C7EB" strokeWidth="0.6" opacity="0.6" />
            </g>
          ))}
          <animateTransform attributeName="transform" type="rotate" from="0 200 200" to="360 200 200" dur="120s" repeatCount="indefinite" />
        </g>
        {/* inner rings */}
        <circle cx="200" cy="200" r="66" fill="none" stroke="#67C7EB" strokeWidth="2" opacity="0.85" />
        <circle cx="200" cy="200" r="54" fill="none" stroke="#67C7EB" strokeWidth="1" strokeDasharray="4 7" opacity="0.5">
          <animateTransform attributeName="transform" type="rotate" from="360 200 200" to="0 200 200" dur="26s" repeatCount="indefinite" />
        </circle>
        {/* glowing core */}
        <circle cx="200" cy="200" r="46" fill="url(#reactorCore)">
          <animate attributeName="opacity" values="0.7;1;0.7" dur="3s" repeatCount="indefinite" />
        </circle>
        <circle cx="200" cy="200" r="15" fill="#eafcff" opacity="0.95" />
      </svg>
    </div>
  )

  // --- Rendering Helpers ---
  const fmtUptime = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`

  const renderSparkline = (data) => {
    const w = 46, h = 14
    if (data.length < 2) {
      return <svg className="spark" width={w} height={h} viewBox={`0 0 ${w} ${h}`}><line x1="0" y1={h - 1} x2={w} y2={h - 1} stroke="currentColor" strokeWidth="1" opacity="0.4" /></svg>
    }
    const max = Math.max(...data), min = Math.min(...data), range = Math.max(max - min, 0.001)
    const pts = data.map((v, i) => `${(i / (data.length - 1) * w).toFixed(1)},${(h - ((v - min) / range) * (h - 2) - 1).toFixed(1)}`).join(" ")
    return <svg className="spark" width={w} height={h} viewBox={`0 0 ${w} ${h}`}><polyline points={pts} fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" /></svg>
  }

  const copyText = (text, e) => {
    navigator.clipboard.writeText(text)
    const btn = e.target
    btn.textContent = "OK"
    btn.classList.add("ok")
    setTimeout(() => { btn.textContent = "Copy"; btn.classList.remove("ok") }, 1200)
  }

  // Very basic markdown rendering for React (code blocks, bold, etc)
  const renderMessageContent = (content) => {
    if (!content) return null
    const parts = content.split(/(```[\s\S]*?```)/g)
    return parts.map((part, idx) => {
      if (part.startsWith("```") && part.endsWith("```")) {
        const code = part.slice(3, -3).replace(/^\w+\n/, "")
        return <pre key={idx}><code>{code}</code></pre>
      }
      const inlineParts = part.split(/(`[^`]+`)/g)
      return <span key={idx}>{inlineParts.map((inline, iIdx) => {
        if (inline.startsWith("`") && inline.endsWith("`")) return <code key={iIdx}>{inline.slice(1, -1)}</code>
        const bolds = inline.split(/(\*\*[^*]+\*\*)/g)
        return <span key={iIdx}>{bolds.map((b, bIdx) => {
          if (b.startsWith("**") && b.endsWith("**")) return <strong key={bIdx}>{b.slice(2, -2)}</strong>
          return b.split("\n").map((line, lIdx) => <React.Fragment key={lIdx}>{line}{lIdx < b.split("\n").length - 1 && <br/>}</React.Fragment>)
        })}</span>
      })}</span>
    })
  }

  if (booting) {
    return (
      <div className="boot-overlay" onClick={skipBoot} style={{ cursor: 'pointer' }} title="Click to skip">
        <div className="boot-grid" />
        <svg className="boot-reactor" width="160" height="160" viewBox="0 0 160 160">
          <circle cx="80" cy="80" r="76" fill="none" stroke="#67C7EB" strokeWidth="1" opacity="0.2" />
          <circle cx="80" cy="80" r="64" fill="none" stroke="#67C7EB" strokeWidth="1.5" strokeDasharray="14 8" opacity="0.45">
            <animateTransform attributeName="transform" type="rotate" from="0 80 80" to="360 80 80" dur="6s" repeatCount="indefinite" />
          </circle>
          <circle cx="80" cy="80" r="48" fill="none" stroke="#67C7EB" strokeWidth="1" strokeDasharray="4 6" opacity="0.5">
            <animateTransform attributeName="transform" type="rotate" from="360 80 80" to="0 80 80" dur="4s" repeatCount="indefinite" />
          </circle>
          <circle cx="80" cy="80" r="30" fill="none" stroke="#67C7EB" strokeWidth="2" opacity="0.7" />
          <circle cx="80" cy="80" r="18" fill="url(#bootCore)">
            <animate attributeName="opacity" values="0.5;1;0.5" dur="1.4s" repeatCount="indefinite" />
          </circle>
          <circle cx="80" cy="80" r="7" fill="#fff" opacity="0.95" />
          <defs>
            <radialGradient id="bootCore">
              <stop offset="0%" stopColor="#cdeeff" stopOpacity="1" />
              <stop offset="60%" stopColor="#67C7EB" stopOpacity="0.5" />
              <stop offset="100%" stopColor="#67C7EB" stopOpacity="0" />
            </radialGradient>
          </defs>
        </svg>
        <div className="boot-title">J.A.R.V.I.S</div>
        <div className="boot-log">
          <span className="boot-line" style={{animationDelay: '0.2s'}}><span>▸ Neural core</span><span className="bl-ok">ONLINE</span></span>
          <span className="boot-line" style={{animationDelay: '0.6s'}}><span>▸ Memory banks</span><span className="bl-ok">MOUNTED</span></span>
          <span className="boot-line" style={{animationDelay: '1.0s'}}><span>▸ Language model</span><span className="bl-ok">CALIBRATED</span></span>
          <span className="boot-line" style={{animationDelay: '1.4s'}}><span>▸ Secure uplink</span><span className="bl-ok">ESTABLISHED</span></span>
          <span className="boot-line" style={{animationDelay: '1.9s'}}><span style={{color: 'var(--alert-orange)'}}>▸ All systems</span><span className="bl-ok" style={{color: 'var(--alert-orange)'}}>ONLINE</span></span>
        </div>
        <div className="boot-bar"><div className="boot-bar-fill" /></div>
        <div className="boot-status">
          <span className="boot-pct">{bootPct}%</span>
          <span className="boot-skip-hint">Click anywhere to skip</span>
        </div>
      </div>
    )
  }

  if (!token) {
    return (
      <div className="login-overlay" style={{display: 'flex'}}>
        <div className="login-box">
          <svg className="login-reactor" width="56" height="56" viewBox="0 0 56 56">
            <circle cx="28" cy="28" r="26" fill="none" stroke="#67C7EB" strokeWidth="1.5" opacity="0.4" />
            <circle cx="28" cy="28" r="20" fill="none" stroke="#67C7EB" strokeWidth="1" strokeDasharray="6 4" opacity="0.5">
              <animateTransform attributeName="transform" type="rotate" from="0 28 28" to="360 28 28" dur="12s" repeatCount="indefinite" />
            </circle>
            <circle cx="28" cy="28" r="14" fill="none" stroke="#67C7EB" strokeWidth="1.5" opacity="0.7" />
            <circle cx="28" cy="28" r="7" fill="#67C7EB" opacity="0.3" />
            <circle cx="28" cy="28" r="3" fill="#67C7EB" opacity="0.8" />
          </svg>
          <div className="login-label">System Authorization</div>
          <input className="login-input" value={username} onChange={e=>setUsername(e.target.value)} placeholder="Identifier" />
          <input className="login-input" type="password" value={password} onChange={e=>setPassword(e.target.value)} onKeyDown={e=>{if(e.key==='Enter')doLogin()}} placeholder="Access Code" />
          <button className="login-btn" onClick={doLogin}>{loginStatus}</button>
          {loginError && <div className="login-error" style={{display: 'block'}}>{loginError}</div>}
        </div>
      </div>
    )
  }

  return (
    <div className={`app-container ${processing ? 'thinking' : ''}`}>
      <div className="hud-frame" aria-hidden="true">
        <span className="hud-corner tl" /><span className="hud-corner tr" />
        <span className="hud-corner bl" /><span className="hud-corner br" />
      </div>
      {/* Ambient parallax particle field — three drifting layers for depth. */}
      <div className="ambient-particles" aria-hidden="true">
        <span className="pfield p1" /><span className="pfield p2" /><span className="pfield p3" />
      </div>
      <div className={`sidebar-overlay ${sidebarOpen ? 'visible' : ''}`} onClick={() => setSidebarOpen(false)}></div>

      {paletteOpen && (() => {
        const items = paletteActions()
        const idx = Math.min(paletteIndex, Math.max(items.length - 1, 0))
        return (
          <div className="palette-overlay" onClick={() => setPaletteOpen(false)}>
            <div className="palette" onClick={e => e.stopPropagation()}>
              <input
                ref={paletteInputRef}
                className="palette-input"
                value={paletteQuery}
                onChange={e => { setPaletteQuery(e.target.value); setPaletteIndex(0) }}
                onKeyDown={e => onPaletteKey(e, items)}
                placeholder="Type a command or session…"
              />
              <div className="palette-list">
                {items.length === 0 && <div className="palette-empty">No matches</div>}
                {items.map((it, i) => (
                  <div key={i} className={`palette-item ${i === idx ? 'active' : ''}`}
                    onMouseEnter={() => setPaletteIndex(i)} onClick={() => runPaletteItem(it)}>
                    <span className="palette-tag">{it.tag}</span>
                    <span>{it.label}</span>
                  </div>
                ))}
              </div>
              <div className="palette-hint"><span>↑↓ navigate</span><span>⏎ run</span><span>esc close</span></div>
            </div>
          </div>
        )
      })()}
      
      <aside className={`sidebar ${sidebarOpen ? 'open' : ''} ${sidebarCollapsed ? 'collapsed' : ''}`}>
        <div className="sidebar-inner">
          <div className="sidebar-header">
            <svg className="reactor-logo" width="38" height="38" viewBox="0 0 38 38">
              <circle cx="19" cy="19" r="17" fill="none" stroke="#67C7EB" strokeWidth="1" opacity="0.35" />
              <circle cx="19" cy="19" r="13" fill="none" stroke="#67C7EB" strokeWidth="0.8" strokeDasharray="4 3" opacity="0.45">
                <animateTransform attributeName="transform" type="rotate" from="0 19 19" to="-360 19 19" dur="10s" repeatCount="indefinite" />
              </circle>
              <circle cx="19" cy="19" r="9" fill="none" stroke="#67C7EB" strokeWidth="1" opacity="0.6" />
              <circle cx="19" cy="19" r="4" fill="#67C7EB" opacity="0.4" />
              <circle cx="19" cy="19" r="2" fill="#67C7EB" opacity="0.9" />
            </svg>
            <div>
              <div className="sidebar-title">J.A.R.V.I.S</div>
              <div className="sidebar-subtitle">Stark Industries</div>
            </div>
          </div>

          <div className="sidebar-scroll">
          <div className="hud-panel">
            <div className="hud-label">Diagnostics</div>
            <div className="stat-row">
              <div className={`status-dot ${isOnline ? 'online' : 'offline'}`}></div>
              <span>{isOnline ? 'Online' : 'Offline'}</span>
            </div>
            <div className="stat-row">
              <span>Neural Core</span>
              <span className="stat-val">{modelName}</span>
            </div>
            <div className="stat-row">
              <span>Uplink</span>
              <span className="stat-val">{uplink}</span>
            </div>
          </div>

          <div className="hud-panel">
            <div className="hud-label">Access Control</div>
            <div className="panel-row">
              <button className="hud-btn" onClick={doLogout}>Disconnect</button>
              {role === "admin" && <button className="hud-btn warn" onClick={() => window.location.href='/admin'}>Admin</button>}
            </div>
          </div>

          <div className="hud-panel">
            <div className="hud-label">
              Interface Theme
              <button className="adv-btn" onClick={() => setThemeOpen(o => !o)}>{themeOpen ? '▾ Hide' : '▸ Show'}</button>
            </div>
            {themeOpen && (
              <div className="theme-grid">
                {[
                  { id: "stark", name: "Stark", color: "#67C7EB" },
                  { id: "cyberpunk", name: "Cyberpunk", color: "#ff4dd2" },
                  { id: "emerald", name: "Emerald", color: "#2fe6a0" },
                  { id: "ember", name: "Ember", color: "#ffae42" },
                ].map(t => (
                  <button key={t.id} className={`theme-chip ${theme === t.id ? "active" : ""}`} onClick={() => setTheme(t.id)}>
                    <span className="theme-dot" style={{ background: t.color }} />
                    {t.name}
                  </button>
                ))}
              </div>
            )}
          </div>

          <div className="hud-panel">
            <div className="hud-label">
              Parameters
              <button className="adv-btn" onClick={() => setAdvancedOpen(!advancedOpen)}>
                {advancedOpen ? '▾ Hide' : '▸ Advanced'}
              </button>
            </div>
            <div className="temp-gauge">
              <svg width="92" height="92" viewBox="0 0 92 92">
                <circle cx="46" cy="46" r="38" fill="none" stroke="rgba(103,199,235,0.15)" strokeWidth="4" />
                <circle className="tg-arc" cx="46" cy="46" r="38" fill="none" stroke="var(--holo-cyan)" strokeWidth="4" strokeLinecap="round"
                  strokeDasharray={2 * Math.PI * 38}
                  strokeDashoffset={2 * Math.PI * 38 * (1 - Math.min(temp / 2, 1))}
                  transform="rotate(-90 46 46)" />
              </svg>
              <div className="tg-center"><span className="tg-val">{temp.toFixed(2)}</span><span className="tg-lbl">TEMP</span></div>
            </div>
            <div className="slider-row">
              <label>Temp</label>
              <input type="range" min="0" max="2" step="0.05" value={temp} onChange={e => setTemp(parseFloat(e.target.value))} />
              <span className="slider-val">{temp.toFixed(2)}</span>
            </div>

            {advancedOpen && (
              <div className="adv-panel open">
                <div className="slider-row">
                  <label>Top-K</label>
                  <input type="range" min="0" max="100" step="1" value={topK} onChange={e => setTopK(parseInt(e.target.value))} />
                  <span className="slider-val">{topK}</span>
                </div>
                <div className="slider-row">
                  <label>Top-P</label>
                  <input type="range" min="0" max="1" step="0.05" value={topP} onChange={e => setTopP(parseFloat(e.target.value))} />
                  <span className="slider-val">{topP.toFixed(2)}</span>
                </div>
                <div className="slider-row">
                  <label>Min-P</label>
                  <input type="range" min="0" max="1" step="0.01" value={minP} onChange={e => setMinP(parseFloat(e.target.value))} />
                  <span className="slider-val">{minP.toFixed(2)}</span>
                </div>
                <div className="slider-row">
                  <label>Rep Pen</label>
                  <input type="range" min="1" max="2" step="0.05" value={repeatPenalty} onChange={e => setRepeatPenalty(parseFloat(e.target.value))} />
                  <span className="slider-val">{repeatPenalty.toFixed(2)}</span>
                </div>
                <div className="slider-row">
                  <label>Pres Pen</label>
                  <input type="range" min="0" max="2" step="0.05" value={presencePenalty} onChange={e => setPresencePenalty(parseFloat(e.target.value))} />
                  <span className="slider-val">{presencePenalty.toFixed(2)}</span>
                </div>
                <div className="slider-row">
                  <label>Freq Pen</label>
                  <input type="range" min="0" max="2" step="0.05" value={freqPenalty} onChange={e => setFreqPenalty(parseFloat(e.target.value))} />
                  <span className="slider-val">{freqPenalty.toFixed(2)}</span>
                </div>
                <div className="slider-row">
                  <label>Max Tok</label>
                  <input type="number" className="hud-input" value={nPredict} onChange={e => setNPredict(parseInt(e.target.value))} style={{flex: 1}} />
                </div>
                <div className="slider-row">
                  <label>Seed</label>
                  <input type="number" className="hud-input" value={seed} onChange={e => setSeed(parseInt(e.target.value))} style={{flex: 1}} />
                </div>
                <div className="toggle-row" style={{marginTop: '6px'}}>
                  <label>Voice Output</label>
                  <input type="checkbox" className="hud-toggle" checked={voice} onChange={e => setVoice(e.target.checked)} />
                </div>
                <div style={{marginTop: '8px'}}>
                  <span className="field-label">System Prompt Override</span>
                  <textarea className="hud-textarea" rows="3" value={sysPrompt} onChange={e => setSysPrompt(e.target.value)} placeholder="Leave blank for default..."></textarea>
                </div>
              </div>
            )}
          </div>

          <button className="new-session-btn" onClick={createSession}>
            <span>+</span> New Session
          </button>

          <div className="history-list">
            {sessions.map(s => (
              <div key={s.id} className={`history-item ${s.id === currentSessionId ? 'active' : ''}`} onClick={() => loadHistory(s.id)}>
                <span className="history-item-title">{s.title}</span>
                <div className="history-actions">
                  <button className="hist-btn" onClick={(e) => renameSession(e, s.id)}>[R]</button>
                  <button className="hist-btn" onClick={(e) => deleteSession(e, s.id)}>[D]</button>
                </div>
              </div>
            ))}
          </div>
          </div>

          <div className="sidebar-footer">
            <div className="sidebar-footer-text">J.A.R.V.I.S · {modelName} · Private Server</div>
          </div>
        </div>
      </aside>
      
      <main className="main-area">
        {/* Interactive arc reactor behind the chat — parallax-tilts to the cursor, ramps while thinking. */}
        {renderChatReactor()}
        <div className="top-bar">
          <button className="sidebar-toggle" onClick={toggleSidebar} aria-label="Toggle menu" title="Toggle sidebar">☰</button>
          <span className="top-title">{currentTitle}</span>
          <span className="top-model">{modelName}</span>
          <span className="top-speed">{speed}</span>
          <div className="top-spacer"></div>
          <button className="cmd-btn" onClick={() => { setPaletteQuery(""); setPaletteIndex(0); setPaletteOpen(true) }}
            title="Command palette (Ctrl/Cmd+K)" aria-label="Open command palette">⌘K</button>
          <div className="telemetry" aria-hidden="true">
            <span className="tele-item" title="generation speed (tok/s), last 24 replies">{renderSparkline(tokHistory)}</span>
            <span className="tele-item"><span className="tele-k">MSGS</span>{messages.length}</span>
            <span className="tele-item"><span className="tele-k">UP</span>{fmtUptime(uptime)}</span>
          </div>
          <div className="conn-badge">
            <div className={`status-dot ${isOnline ? 'online' : 'offline'}`}></div>
            <span>{isOnline ? 'Active' : 'Down'}</span>
          </div>
        </div>

        <div className="messages-container" ref={messagesContainerRef} onScroll={onMessagesScroll}>
          <div className="messages-inner">
            {messages.length === 0 && (
              <div className="welcome-screen">
                <div className="welcome-reactor">
                  <svg width="110" height="110" viewBox="0 0 110 110">
                    <circle cx="55" cy="55" r="52" fill="none" stroke="#67C7EB" strokeWidth="1" opacity="0.25" />
                    <circle cx="55" cy="55" r="46" fill="none" stroke="#67C7EB" strokeWidth="1.5" strokeDasharray="12 6" opacity="0.4">
                      <animateTransform attributeName="transform" type="rotate" from="0 55 55" to="360 55 55" dur="20s" repeatCount="indefinite" />
                    </circle>
                    <circle cx="55" cy="55" r="38" fill="none" stroke="#67C7EB" strokeWidth="1" opacity="0.3" />
                    <circle cx="55" cy="55" r="30" fill="none" stroke="#67C7EB" strokeWidth="1.5" strokeDasharray="8 5" opacity="0.5">
                      <animateTransform attributeName="transform" type="rotate" from="360 55 55" to="0 55 55" dur="14s" repeatCount="indefinite" />
                    </circle>
                    <circle cx="55" cy="55" r="22" fill="none" stroke="#67C7EB" strokeWidth="1.5" opacity="0.6" />
                    <circle cx="55" cy="55" r="14" fill="url(#coreGrad)" opacity="0.7">
                      <animate attributeName="opacity" values="0.5;0.8;0.5" dur="3s" repeatCount="indefinite" />
                    </circle>
                    <circle cx="55" cy="55" r="6" fill="#67C7EB" opacity="0.9" />
                    <defs>
                      <radialGradient id="coreGrad">
                        <stop offset="0%" stopColor="#67C7EB" stopOpacity="0.8" />
                        <stop offset="70%" stopColor="#67C7EB" stopOpacity="0.2" />
                        <stop offset="100%" stopColor="#67C7EB" stopOpacity="0" />
                      </radialGradient>
                    </defs>
                    <line x1="55" y1="3" x2="55" y2="15" stroke="#67C7EB" strokeWidth="0.5" opacity="0.3" />
                    <line x1="55" y1="95" x2="55" y2="107" stroke="#67C7EB" strokeWidth="0.5" opacity="0.3" />
                    <line x1="3" y1="55" x2="15" y2="55" stroke="#67C7EB" strokeWidth="0.5" opacity="0.3" />
                    <line x1="95" y1="55" x2="107" y2="55" stroke="#67C7EB" strokeWidth="0.5" opacity="0.3" />
                  </svg>
                </div>
                <h1 className="welcome-title">J.A.R.V.I.S</h1>
                <p className="welcome-sub">Just A Rather Very Intelligent System<br/>All systems operational. Private server. Local processing.</p>
                <div className="welcome-grid">
                  <button className="sug-btn" onClick={() => send("What can you help me with?")}><span className="sug-icon">[SYS]</span> What can you help me with?</button>
                  <button className="sug-btn" onClick={() => send("Tell me a fun fact about technology")}><span className="sug-icon">[DATA]</span> Fun fact about technology</button>
                  <button className="sug-btn" onClick={() => send("Explain quantum computing simply")}><span className="sug-icon">[CALC]</span> Explain quantum computing</button>
                  <button className="sug-btn" onClick={() => send("Write a short poem about AI")}><span className="sug-icon">[GEN]</span> Write a poem about AI</button>
                </div>
              </div>
            )}
            
            {messages.map((m, i) => (
              <div key={i} className="message">
                <div className={`msg-avatar ${m.role}`}>{m.role === 'user' ? 'U' : 'J'}</div>
                <div className={`msg-body ${m.isStreaming ? 'streaming' : ''}`}>
                  <div className="msg-head">
                    <span className={`msg-sender ${m.role}`}>{m.role === 'user' ? 'You' : 'Jarvis'}</span>
                    <div className="msg-actions">
                      <button className="copy-btn" onClick={(e) => copyText(m.content, e)}>Copy</button>
                    </div>
                  </div>
                  <div className="msg-content">
                    {m.isStreaming && m.content === "" ? (
                      <div className="typing-indicator" style={{margin:0}}>
                        <div className="typing-dots" style={{padding:'5px 10px'}}>
                          <div className="typing-dot"></div><div className="typing-dot"></div><div className="typing-dot"></div>
                        </div>
                      </div>
                    ) : (
                      <>
                        {renderMessageContent(m.content)}
                        {m.isStreaming && <span className="stream-cursor" aria-hidden="true" />}
                      </>
                    )}
                  </div>
                </div>
              </div>
            ))}
            <div ref={messagesEndRef} />
          </div>
        </div>
        
        <div className="input-area">
          <div className="input-wrap">
            <textarea 
              ref={inputRef}
              className="input-field" 
              value={input} 
              onChange={e => {
                setInput(e.target.value)
                e.target.style.height = 'auto'
                e.target.style.height = Math.min(e.target.scrollHeight, 140) + 'px'
              }}
              onKeyDown={e => { if(e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }}
              placeholder="Enter query..." 
              rows={1}
            />
            <span className="char-ct">{input.length}</span>
            <button className={`send-btn ${processing ? 'stop' : ''}`} onClick={() => processing ? stopGeneration() : send()}
              aria-label={processing ? 'Stop' : 'Send message'} title={processing ? 'Stop' : 'Send'}>
              {processing ? '■' : '▶'}
            </button>
          </div>
          <div className="input-hint">Enter to transmit · Shift+Enter new line · <span className="kbd" role="button" tabIndex={0} onClick={() => { setPaletteQuery(""); setPaletteIndex(0); setPaletteOpen(true) }} style={{cursor:'pointer'}}>⌘K</span> commands</div>
        </div>
      </main>
    </div>
  )
}

export default App
