import { useState, useEffect, useRef, useId, useMemo, memo } from 'react'
import './index.css'
import Admin from './Admin'

const API = ""

// Arc reactor — Mark I "PROOF THAT TONY STARK HAS A HEART" style: a brushed-steel ring with engraved
// text, alternating copper wound coils + blue-glow panels, a bolt ring, and a layered blue core.
// Recreated as vector art (iterated via headless render against the reference photo). Static.
// Reused at every size; the engraved text auto-hides on the tiny logo/login sizes to stay clean.
// useId() keeps gradient + textPath ids unique per instance.
function ArcReactor({ size = 120, className = "" }) {
  const id = useId()
  const u = (s) => `${id}-${s}`
  const showText = size >= 110
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 400 400" aria-hidden="true">
      <defs>
        <radialGradient id={u("core")}>
          <stop offset="0%" stopColor="#eafaff" /><stop offset="30%" stopColor="#7fd0ff" />
          <stop offset="70%" stopColor="#2e8fe0" /><stop offset="100%" stopColor="#2e8fe0" stopOpacity="0" />
        </radialGradient>
        <radialGradient id={u("bloom")}>
          <stop offset="0%" stopColor="#4aa6ff" stopOpacity="0.55" /><stop offset="100%" stopColor="#4aa6ff" stopOpacity="0" />
        </radialGradient>
        <linearGradient id={u("blue")} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#bfeaff" /><stop offset="45%" stopColor="#4aa6ff" /><stop offset="100%" stopColor="#1c5fa8" />
        </linearGradient>
        <linearGradient id={u("steel")} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#d8e0e4" /><stop offset="20%" stopColor="#b1bbc1" />
          <stop offset="58%" stopColor="#8a949b" /><stop offset="100%" stopColor="#717b82" />
        </linearGradient>
        <linearGradient id={u("steelV")} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#c2ccd2" /><stop offset="50%" stopColor="#525d64" /><stop offset="100%" stopColor="#222a30" />
        </linearGradient>
        <linearGradient id={u("wire")} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#6f3c13" /><stop offset="34%" stopColor="#e2924a" />
          <stop offset="54%" stopColor="#ffdcae" /><stop offset="76%" stopColor="#d6843e" /><stop offset="100%" stopColor="#54300f" />
        </linearGradient>
        <path id={u("top")} d="M 36 200 A 164 164 0 0 1 364 200" fill="none" />
        <path id={u("bot")} d="M 24 200 A 176 176 0 0 0 376 200" fill="none" />
      </defs>
      <circle cx="200" cy="200" r="195" fill={`url(#${u("bloom")})`} opacity="0.4" />
      {/* steel housing + engraved text */}
      <path d="M200 8 A192 192 0 1 0 200.01 8 Z M200 52 A148 148 0 1 0 200.01 52 Z" fill={`url(#${u("steel")})`} fillRule="evenodd" />
      <circle cx="200" cy="200" r="192" fill="none" stroke="#eef3f5" strokeWidth="1" opacity="0.6" />
      <circle cx="200" cy="200" r="148" fill="none" stroke="#10161a" strokeWidth="2.5" opacity="0.85" />
      {showText && (
        <g fill="#181f25" fontSize="23" style={{ fontFamily: "'Arial Narrow', Arial, sans-serif", fontWeight: 700, letterSpacing: "2px" }}>
          <text><textPath href={`#${u("top")}`} startOffset="50%" textAnchor="middle">PROOF THAT TONY STARK</textPath></text>
          <text><textPath href={`#${u("bot")}`} startOffset="50%" textAnchor="middle">HAS A HEART</textPath></text>
        </g>
      )}
      <circle cx="200" cy="200" r="120" fill={`url(#${u("bloom")})`} opacity="0.5" />
      {/* alternating blue-glow panels + copper wound coils */}
      {Array.from({ length: 16 }).map((_, i) => {
        const rot = `rotate(${i * 22.5} 200 200)`
        if (i % 2 === 0) return (
          <g key={i} transform={rot}>
            <path d="M187 56 L213 56 L208 104 L192 104 Z" fill={`url(#${u("blue")})`} stroke="#bfe9ff" strokeWidth="1.2" />
            <path d="M191 60 L209 60 L206 100 L194 100 Z" fill="#9fe0ff" opacity="0.25" />
          </g>
        )
        return (
          <g key={i} transform={rot}>
            <rect x="183" y="55" width="34" height="52" rx="4" fill="#0a1218" stroke="#2f3a42" strokeWidth="1.3" />
            <rect x="184" y="56" width="32" height="5" rx="2" fill={`url(#${u("steelV")})`} />
            {Array.from({ length: 9 }).map((_, t) => (
              <rect key={t} x="186" y={(60 + t * 5).toFixed(1)} width="28" height="3.8" rx="1.9" fill={`url(#${u("wire")})`} />
            ))}
            <rect x="184" y="101" width="32" height="5" rx="2" fill={`url(#${u("steelV")})`} />
          </g>
        )
      })}
      {/* bolt ring */}
      {Array.from({ length: 28 }).map((_, i) => (
        <circle key={i} cx="200" cy="113" r="2.6" fill="#0a1117" stroke="#5b6973" strokeWidth="0.8" transform={`rotate(${i * (360 / 28)} 200 200)`} />
      ))}
      {/* center: concentric rings, hole ring, dark hub + blue core */}
      <circle cx="200" cy="200" r="86" fill="#08141e" stroke={`url(#${u("steelV")})`} strokeWidth="6" />
      <circle cx="200" cy="200" r="80" fill="none" stroke="#3a6a96" strokeWidth="1" opacity="0.6" />
      <circle cx="200" cy="200" r="64" fill="none" stroke={`url(#${u("steelV")})`} strokeWidth="4" />
      {Array.from({ length: 14 }).map((_, i) => (
        <circle key={i} cx="200" cy="150" r="2" fill="#0a1620" transform={`rotate(${i * (360 / 14)} 200 200)`} />
      ))}
      <circle cx="200" cy="200" r="44" fill={`url(#${u("core")})`} />
      <circle cx="200" cy="200" r="30" fill="#1a2026" stroke="#3a6a96" strokeWidth="1.5" />
      {Array.from({ length: 4 }).map((_, i) => (
        <rect key={i} x="197" y="178" width="6" height="20" rx="2" fill="#11181e" transform={`rotate(${i * 90} 200 200)`} />
      ))}
      <circle cx="200" cy="200" r="15" fill={`url(#${u("core")})`} />
      <circle cx="200" cy="200" r="6" fill="#ffffff" />
    </svg>
  )
}

// JARVIS-style greeting: addresses the user as "sir", and varies by the time of day, the day of week,
// and a bit of "the moment" (late nights, weekends). Re-rolled each session so it never feels canned.
function jarvisGreeting(_name) {
  const now = new Date()
  const h = now.getHours()
  const weekend = now.getDay() === 0 || now.getDay() === 6
  const pick = arr => arr[Math.floor(Math.random() * arr.length)]

  // time bucket → fitting openers ("the moment")
  const part = h < 5 ? "late" : h < 12 ? "morning" : h < 17 ? "afternoon" : h < 21 ? "evening" : "night"
  const openers = {
    late:      ["You're up late, sir.", "Burning the midnight oil, sir?", "Still awake at this hour, sir?"],
    morning:   ["Good morning, sir.", "Morning, sir.", "A fresh start, sir."],
    afternoon: ["Good afternoon, sir.", "Afternoon, sir.", "Hope the day's going well, sir."],
    evening:   ["Good evening, sir.", "Evening, sir.", "Winding down, sir?"],
    night:     ["Good evening, sir.", "Getting late, sir.", "Late shift, sir?"],
  }

  // taglines: a shared pool plus a few that fit the current moment
  const taglines = [
    "At your service.",
    "How may I help you today?",
    "All systems operational — local processing, private server.",
    "Standing by, as ever.",
    "Ready when you are.",
    "A pleasure, as always.",
    "Everything is running smoothly.",
    "What shall we work on?",
  ]
  if (part === "late" || part === "night") taglines.push("Do get some rest soon, sir.", "I'll keep things quiet.")
  if (part === "morning") taglines.push("Shall I run through what's pending?", "Let's make a good start.")
  if (weekend) taglines.push("Enjoying the weekend, sir?", "No rush today, sir.")

  return `${pick(openers[part])} ${pick(taglines)}`
}

// --- Message rendering (module scope: stable identity so memo() works) ---
function copyText(text, e) {
  navigator.clipboard.writeText(text)
  const btn = e.target
  btn.textContent = "OK"
  btn.classList.add("ok")
  setTimeout(() => { btn.textContent = "Copy"; btn.classList.remove("ok") }, 1200)
}

// Inline markdown: **bold**, `code`, and [links](url). Builds React text nodes
// only (no dangerouslySetInnerHTML), so it's XSS-safe by construction.
function renderInline(text) {
  const nodes = []
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)\s]+\))/g
  let last = 0, m, i = 0
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) nodes.push(text.slice(last, m.index))
    const tok = m[0]
    if (tok.startsWith("**")) nodes.push(<strong key={i}>{tok.slice(2, -2)}</strong>)
    else if (tok.startsWith("`")) nodes.push(<code key={i}>{tok.slice(1, -1)}</code>)
    else {
      const mm = tok.match(/\[([^\]]+)\]\(([^)\s]+)\)/)
      const href = /^https?:\/\//i.test(mm[2]) ? mm[2] : "#"   // only allow http(s) links
      nodes.push(<a key={i} href={href} target="_blank" rel="noopener noreferrer">{mm[1]}</a>)
    }
    last = m.index + tok.length; i++
  }
  if (last < text.length) nodes.push(text.slice(last))
  return nodes
}

// Block markdown: fenced code, #/##/### headings, -/* and 1. lists, paragraphs.
function renderMessageContent(content) {
  if (!content) return null
  return content.split(/(```[\s\S]*?```)/g).map((part, idx) => {
    if (part.startsWith("```") && part.endsWith("```")) {
      const code = part.slice(3, -3).replace(/^\w+\n/, "")
      return <pre key={idx}><code>{code}</code></pre>
    }
    const blocks = []
    let list = null
    const flush = () => {
      if (!list) return
      const Tag = list.type
      blocks.push(<Tag key={blocks.length} className="md-list">{list.items.map((it, i) => <li key={i}>{renderInline(it)}</li>)}</Tag>)
      list = null
    }
    part.split("\n").forEach(line => {
      const h = line.match(/^(#{1,3})\s+(.*)$/)
      const ul = line.match(/^\s*[-*]\s+(.*)$/)
      const ol = line.match(/^\s*\d+\.\s+(.*)$/)
      if (h) { flush(); blocks.push(<div key={blocks.length} className={`md-h md-h${h[1].length}`}>{renderInline(h[2])}</div>) }
      else if (ul) { if (!list || list.type !== "ul") { flush(); list = { type: "ul", items: [] } } list.items.push(ul[1]) }
      else if (ol) { if (!list || list.type !== "ol") { flush(); list = { type: "ol", items: [] } } list.items.push(ol[1]) }
      else if (line.trim() === "") { flush(); blocks.push(<div key={blocks.length} className="md-gap" />) }
      else { flush(); blocks.push(<div key={blocks.length} className="md-line">{renderInline(line)}</div>) }
    })
    flush()
    return <span key={idx}>{blocks}</span>
  })
}

// One chat message. memo()'d so streaming a token re-renders only the LAST message
// instead of re-parsing every message's markdown each token (that was the scroll jank).
const MessageItem = memo(function MessageItem({ role, content, isStreaming }) {
  return (
    <div className="message">
      <div className={`msg-avatar ${role}`}>{role === 'user' ? 'U' : 'J'}</div>
      <div className={`msg-body ${isStreaming ? 'streaming' : ''}`}>
        <div className="msg-head">
          <span className={`msg-sender ${role}`}>{role === 'user' ? 'You' : 'Jarvis'}</span>
          <div className="msg-actions">
            <button className="copy-btn" onClick={(e) => copyText(content, e)}>Copy</button>
          </div>
        </div>
        <div className="msg-content">
          {isStreaming && content === "" ? (
            <div className="typing-indicator" style={{margin:0}}>
              <div className="typing-dots" style={{padding:'5px 10px'}}>
                <div className="typing-dot"></div><div className="typing-dot"></div><div className="typing-dot"></div>
              </div>
            </div>
          ) : (
            <>
              {renderMessageContent(content)}
              {isStreaming && <span className="stream-cursor" aria-hidden="true" />}
            </>
          )}
        </div>
      </div>
    </div>
  )
})

// Static "neural activity" waveform path (two harmonics; seamless loop over the tile).
const OSC_PATH = (() => {
  const w = 480, mid = 30, amp = 18, P = 8, steps = 240
  let d = `M0 ${mid}`
  for (let i = 1; i <= steps; i++) {
    const t = i / steps, x = t * w
    const y = mid + amp * (0.7 * Math.sin(t * 2 * Math.PI * P) + 0.3 * Math.sin(t * 2 * Math.PI * P * 2))
    d += ` L${x.toFixed(1)} ${y.toFixed(1)}`
  }
  return d
})()

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
  const [greetTyped, setGreetTyped] = useState("")
  // eslint-disable-next-line react-hooks/exhaustive-deps -- recompute on login so the name appears
  const greeting = useMemo(() => jarvisGreeting((localStorage.getItem("jarvis_user") || "").trim()), [token])
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
  const [nPredict, setNPredict] = useState(2048)   // backend clamps this to fit the 4096-token context
  const [seed, setSeed] = useState(-1)
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
  const [sys, setSys] = useState({})   // live host stats from /system (CPU/RAM/uptime)

  // Command palette (⌘K / Ctrl+K).
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [paletteQuery, setPaletteQuery] = useState("")
  const [paletteIndex, setPaletteIndex] = useState(0)

  // Theme switcher + synthesized UI sound (both persisted, sound off by default).
  const [theme, setTheme] = useState(() => localStorage.getItem("jarvis_theme") || "stark")
  const [sound, setSound] = useState(() => localStorage.getItem("jarvis_sound") === "1")
  const [perfMode, setPerfMode] = useState(() => localStorage.getItem("jarvis_perf") === "1")
  const audioCtxRef = useRef(null)
  const greetSpokenRef = useRef(false)   // speak the greeting at most once per session

  // Speak arbitrary text via the server's Piper TTS (used for the JARVIS greeting).
  const speak = async (text, tok = token) => {
    if (!text || !tok) return
    try {
      const res = await fetch(API + "/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": "Bearer " + tok },
        body: JSON.stringify({ text }),
      })
      if (!res.ok) return
      const { audio } = await res.json()
      if (audio) { const a = new Audio("data:audio/wav;base64," + audio); a.play().catch(() => {}) }
    } catch { /* ignore */ }
  }

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
  const scrollRafRef = useRef(0)   // coalesces per-token auto-scrolls into one per frame

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
    // eslint-disable-next-line react-hooks/exhaustive-deps -- run on auth change only
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
    if (!stickToBottomRef.current || scrollRafRef.current) return
    // Coalesce rapid streaming-token updates into ONE scroll per animation frame, so we
    // don't force a layout read on every token. Instant while streaming; smooth to settle.
    scrollRafRef.current = requestAnimationFrame(() => {
      scrollRafRef.current = 0
      el.scrollTo({ top: el.scrollHeight, behavior: processing ? "auto" : "smooth" })
    })
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

  // (Cursor parallax removed — the UI is intentionally static: calmer, prettier, and zero
  //  per-pointer-move repaints. Depth now comes from static gradients + glows, not motion.)

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

  // Poll live host telemetry (CPU/RAM/uptime) for the diagnostics panel.
  useEffect(() => {
    if (!token) return
    let active = true
    const fetchSys = async () => {
      try {
        const r = await fetch(API + "/system", { headers: { Authorization: "Bearer " + token } })
        if (r.ok && active) setSys(await r.json())
      } catch { /* ignore transient errors */ }
    }
    fetchSys()
    const id = setInterval(fetchSys, 5000)
    return () => { active = false; clearInterval(id) }
  }, [token])

  // Theme: a global hue/saturation tint applied via [data-theme] on <html>.
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme)
    localStorage.setItem("jarvis_theme", theme)
  }, [theme])
  useEffect(() => { localStorage.setItem("jarvis_sound", sound ? "1" : "0") }, [sound])
  // Type out the JARVIS greeting on the welcome screen (empty chat), one character at a time.
  useEffect(() => {
    if (messages.length !== 0) return
    setGreetTyped("")
    let i = 0
    const id = setInterval(() => {
      setGreetTyped(greeting.slice(0, ++i))
      if (i >= greeting.length) clearInterval(id)
    }, 42)
    return () => clearInterval(id)
  }, [greeting, messages.length])
  // Speak the greeting on the first user gesture (browsers block autoplay until one) while on the
  // welcome screen — covers page reload; login speaks it directly. Sound toggle gates it.
  useEffect(() => {
    if (!token || !sound || greetSpokenRef.current || messages.length !== 0) return
    const fire = () => {
      if (greetSpokenRef.current) return
      greetSpokenRef.current = true
      speak(greeting)
      window.removeEventListener("pointerdown", fire); window.removeEventListener("keydown", fire)
    }
    window.addEventListener("pointerdown", fire); window.addEventListener("keydown", fire)
    return () => { window.removeEventListener("pointerdown", fire); window.removeEventListener("keydown", fire) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, sound, messages.length, greeting])
  // Reduce-effects mode: drop the heavy ambient GPU work (floating particles + the frosted-glass
  // backdrop blur, which re-blurs every frame as particles drift) for smooth scrolling on lighter
  // clients. Applied via a <html class="perf"> hook so it's pure CSS.
  useEffect(() => {
    localStorage.setItem("jarvis_perf", perfMode ? "1" : "0")
    document.documentElement.classList.toggle("perf", perfMode)
  }, [perfMode])

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
    } catch {
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
      localStorage.setItem("jarvis_user", username.trim())
      if (sound) { greetSpokenRef.current = true; speak(jarvisGreeting(username.trim()), data.token) }
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
    localStorage.removeItem("jarvis_user")
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
    } catch { /* ignore */ }
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
    } catch { /* ignore */ }
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
    } catch { /* ignore */ }
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
    } catch { /* ignore */ }
  }

  const deleteSession = async (e, sid) => {
    e.stopPropagation()
    if (!confirm("Delete this session?")) return
    try {
      await fetch(API + "/sessions/" + sid, { method: "DELETE", headers: { "Authorization": "Bearer " + token } })
      if (sid === currentSessionId) loadHistory("default")
      loadSessions()
    } catch { /* ignore */ }
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
      } catch { /* ignore */ }
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
      voice_feedback: sound,   // JARVIS speaks the reply when the voice toggle is on
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
      let buffer = ""   // an SSE line can span reads (the done event's base64 audio is ~50 KB) — buffer it

      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        let nl
        while ((nl = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, nl)
          buffer = buffer.slice(nl + 1)
          if (line.startsWith("data: ")) {
            const dataStr = line.slice(6)
            if (dataStr === "[DONE]") continue
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
                  } catch { /* ignore */ }
                }
              }
            } catch { /* ignore */ }
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
      { tag: "CFG", label: `${advancedOpen ? "Hide" : "Show"} advanced parameters`, run: () => setAdvancedOpen(o => !o) },
      { tag: "IN", label: "Focus message input", run: () => inputRef.current?.focus() },
      { tag: "VOX", label: `JARVIS voice: ${sound ? "on" : "off"} — toggle (greeting + spoken replies)`, run: () => setSound(s => !s) },
      { tag: "FX", label: `Reduce effects: ${perfMode ? "on" : "off"} — toggle (smoother scroll)`, run: () => setPerfMode(p => !p) },
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

  // Realistic Stark-style arc reactor behind the chat: a metallic bezel with bolts,
  // a ring of wound copper coils (trapezoids with winding detail), radial spokes,
  // the iconic center triangle, and a hot gradient core.
  const renderChatReactor = () => (
    <div className="chat-reactor-bg" aria-hidden="true">
      <ArcReactor size={560} />
    </div>
  )

  // --- Rendering Helpers ---
  const fmtUptime = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`
  const fmtDuration = (s) => {
    if (s == null) return "—"
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60)
    return d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m`
  }

  const renderSparkline = (data) => {
    const w = 46, h = 14
    if (data.length < 2) {
      return <svg className="spark" width={w} height={h} viewBox={`0 0 ${w} ${h}`}><line x1="0" y1={h - 1} x2={w} y2={h - 1} stroke="currentColor" strokeWidth="1" opacity="0.4" /></svg>
    }
    const max = Math.max(...data), min = Math.min(...data), range = Math.max(max - min, 0.001)
    const pts = data.map((v, i) => `${(i / (data.length - 1) * w).toFixed(1)},${(h - ((v - min) / range) * (h - 2) - 1).toFixed(1)}`).join(" ")
    return <svg className="spark" width={w} height={h} viewBox={`0 0 ${w} ${h}`}><polyline points={pts} fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" /></svg>
  }

  // copyText / renderInline / renderMessageContent + the memoized <MessageItem>
  // now live at module scope (top of file) so memo() can skip unchanged messages.

  if (booting) {
    return (
      <div className="boot-overlay" onClick={skipBoot} style={{ cursor: 'pointer' }} title="Click to skip">
        <div className="boot-grid" />
        <ArcReactor size={150} className="boot-reactor" />
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
          <ArcReactor size={64} className="login-reactor" />
          <div className="login-label">System Authorization</div>
          <input className="login-input" value={username} onChange={e=>setUsername(e.target.value)} placeholder="Identifier" />
          <input className="login-input" type="password" value={password} onChange={e=>setPassword(e.target.value)} onKeyDown={e=>{if(e.key==='Enter')doLogin()}} placeholder="Access Code" />
          <button className="login-btn" onClick={doLogin}>{loginStatus}</button>
          {loginError && <div className="login-error" style={{display: 'block'}}>{loginError}</div>}
        </div>
      </div>
    )
  }

  // Admin console lives at /admin within the SPA (so it inherits HUD styling + theme).
  if (window.location.pathname === "/admin") {
    if (role !== "admin") { window.location.href = "/"; return null }
    return <Admin token={token} onExit={() => { window.location.href = "/" }} />
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
            <ArcReactor size={40} className="reactor-logo" />
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
            <div className="gauge-row">
              <span>CPU</span>
              <div className="mini-bar"><div className={`mini-bar-fill ${sys.cpu_pct > 85 ? 'hot' : ''}`} style={{ width: (sys.cpu_pct ?? 0) + '%' }} /></div>
              <span className="stat-val">{sys.cpu_pct != null ? sys.cpu_pct + '%' : '—'}</span>
            </div>
            <div className="gauge-row">
              <span>RAM</span>
              <div className="mini-bar"><div className={`mini-bar-fill ${sys.mem_pct > 85 ? 'hot' : ''}`} style={{ width: (sys.mem_pct ?? 0) + '%' }} /></div>
              <span className="stat-val">{sys.mem_pct != null ? sys.mem_pct + '%' : '—'}</span>
            </div>
            <div className="stat-row">
              <span>Host Up</span>
              <span className="stat-val">{fmtDuration(sys.uptime_sec)}</span>
            </div>
            <div className="oscilloscope">
              <svg className="osc-svg" viewBox="0 0 240 60" preserveAspectRatio="none">
                <path className="osc-wave" d={OSC_PATH} fill="none" stroke="var(--holo-cyan)" strokeWidth="1.5" />
              </svg>
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
            <button className="adv-btn perf-toggle" onClick={() => setPerfMode(p => !p)}
                    title="Disable ambient particles + glass blur for smoother scrolling">
              Reduce effects: {perfMode ? 'ON' : 'OFF'}
            </button>
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
                  <label>JARVIS Voice</label>
                  <input type="checkbox" className="hud-toggle" checked={sound} onChange={e => setSound(e.target.checked)} />
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
                  <ArcReactor size={128} />
                </div>
                <h1 className="welcome-title">J.A.R.V.I.S</h1>
                <p className="welcome-greeting">{greetTyped}{greetTyped.length < greeting.length && <span className="greet-cursor" />}</p>
                <p className="welcome-sub">Just A Rather Very Intelligent System · Local processing · Private server</p>
                <div className="welcome-grid">
                  <button className="sug-btn" onClick={() => send("What can you help me with?")}><span className="sug-icon">[SYS]</span> What can you help me with?</button>
                  <button className="sug-btn" onClick={() => send("Tell me a fun fact about technology")}><span className="sug-icon">[DATA]</span> Fun fact about technology</button>
                  <button className="sug-btn" onClick={() => send("Explain quantum computing simply")}><span className="sug-icon">[CALC]</span> Explain quantum computing</button>
                  <button className="sug-btn" onClick={() => send("Write a short poem about AI")}><span className="sug-icon">[GEN]</span> Write a poem about AI</button>
                </div>
              </div>
            )}
            
            {messages.map((m, i) => (
              <MessageItem key={i} role={m.role} content={m.content} isStreaming={m.isStreaming} />
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
