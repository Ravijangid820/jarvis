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

  // Sidebar Mobile Toggle
  const [sidebarOpen, setSidebarOpen] = useState(false)

  // Parameters
  const [advancedOpen, setAdvancedOpen] = useState(false)
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

  const messagesEndRef = useRef(null)
  const inputRef = useRef(null)

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
    // While streaming this fires on every token, so use an instant scroll — a
    // smooth-scroll animation restarted dozens of times/sec is the main chat jank.
    // Smooth only once the response has settled.
    messagesEndRef.current?.scrollIntoView({ behavior: processing ? "auto" : "smooth" })
  }, [messages, processing])

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

    try {
      const startTime = performance.now()
      const res = await fetch(API + "/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token },
        body: JSON.stringify(payload)
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
                const wallTimeSecs = (performance.now() - startTime) / 1000
                // Honest, clearly-approximate estimate (~4 chars/token), guarded for div-by-zero.
                if (answer && wallTimeSecs > 0) {
                  const tokens = answer.length / 4
                  setSpeed(`~${(tokens / wallTimeSecs).toFixed(1)} tok/s`)
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
    } finally {
      setProcessing(false)
      loadSessions()
      if (inputRef.current) inputRef.current.focus()
    }
  }

  // --- Rendering Helpers ---
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
          <span style={{animationDelay: '0.2s'}}>▸ Initializing neural core…</span>
          <span style={{animationDelay: '0.6s'}}>▸ Mounting memory banks…</span>
          <span style={{animationDelay: '1.0s'}}>▸ Calibrating language model…</span>
          <span style={{animationDelay: '1.4s'}}>▸ Establishing secure uplink…</span>
          <span style={{animationDelay: '1.9s', color: 'var(--alert-orange)'}}>▸ All systems online.</span>
        </div>
        <div className="boot-bar"><div className="boot-bar-fill" /></div>
        <div className="boot-skip-hint">Click anywhere to skip</div>
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
    <div className="app-container">
      <div className="hud-frame" aria-hidden="true">
        <span className="hud-corner tl" /><span className="hud-corner tr" />
        <span className="hud-corner bl" /><span className="hud-corner br" />
      </div>
      <div className={`sidebar-overlay ${sidebarOpen ? 'visible' : ''}`} onClick={() => setSidebarOpen(false)}></div>
      
      <aside className={`sidebar ${sidebarOpen ? 'open' : ''}`}>
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
              Parameters
              <button className="adv-btn" onClick={() => setAdvancedOpen(!advancedOpen)}>
                {advancedOpen ? '▾ Hide' : '▸ Advanced'}
              </button>
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

          <div className="sidebar-footer">
            <div className="sidebar-footer-text">J.A.R.V.I.S · Qwen3.5-2B · Private Server</div>
          </div>
        </div>
      </aside>
      
      <main className="main-area">
        <div className="top-bar">
          <button className="sidebar-toggle" onClick={() => setSidebarOpen(true)} aria-label="Open menu" title="Menu">☰</button>
          <span className="top-title">{currentTitle}</span>
          <span className="top-model">QW-2B</span>
          <span className="top-speed">{speed}</span>
          <div className="top-spacer"></div>
          <div className="conn-badge">
            <div className={`status-dot ${isOnline ? 'online' : 'offline'}`}></div>
            <span>{isOnline ? 'Active' : 'Down'}</span>
          </div>
        </div>

        <div className="messages-container">
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
                <div className="msg-body">
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
            <button className={`send-btn ${processing ? 'stop' : ''}`} onClick={() => send()}
              aria-label={processing ? 'Stop' : 'Send message'} title={processing ? 'Stop' : 'Send'}>
              {processing ? '■' : '▶'}
            </button>
          </div>
          <div className="input-hint">Enter to transmit · Shift+Enter new line</div>
        </div>
      </main>
    </div>
  )
}

export default App
