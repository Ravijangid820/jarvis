import { useState, useEffect, Fragment } from 'react'

// Admin console, rendered by App when the path is /admin and the user is an admin.
// Lives in the React app so it inherits the HUD styling, fonts, and active theme.
const TABS = [
  { id: "overview", label: "Overview" },
  { id: "users", label: "Users" },
  { id: "keys", label: "Keys" },
  { id: "faces", label: "Faces" },
  { id: "household", label: "Household" },
  { id: "smarthome", label: "Smart Home" },
  { id: "system", label: "System" },
]

const KNOWLEDGE_CATEGORIES = ["home", "household", "rooms", "devices", "people", "location", "other"]

export default function Admin({ token, onExit, apiBase = "" }) {
  const [tab, setTab] = useState("overview")
  const [stats, setStats] = useState({})
  const [users, setUsers] = useState([])
  const [keys, setKeys] = useState([])
  const [faces, setFaces] = useState([])
  const [services, setServices] = useState([])
  const [sysInfo, setSysInfo] = useState({})   // { version, summary:{up,total,operational}, checkedAt }
  const [uName, setUName] = useState("")
  const [uPass, setUPass] = useState("")
  const [kUser, setKUser] = useState("")
  const [kDesc, setKDesc] = useState("")
  const [kDev, setKDev] = useState("")
  const [minted, setMinted] = useState("")
  const [mintedDev, setMintedDev] = useState("")
  const [expanded, setExpanded] = useState(null)   // person id whose embeddings are shown
  const [embs, setEmbs] = useState([])
  const [enrollUser, setEnrollUser] = useState("")   // user id the face is enrolled for
  const [enrollDev, setEnrollDev] = useState("")
  const [enrollReqs, setEnrollReqs] = useState([])
  const [recogs, setRecogs] = useState([])           // recent face_seen events (recognitions feed)
  const [present, setPresent] = useState([])         // people the cameras see right now
  const [verifying, setVerifying] = useState(null)   // {id, device, status, text, ok} for the verify flow
  const [preview, setPreview] = useState(null)     // {image, captured, total} during an active enroll
  const [globalFacts, setGlobalFacts] = useState([])   // household/global knowledge
  const [gContent, setGContent] = useState("")
  const [gCat, setGCat] = useState("home")
  const [gChatLog, setGChatLog] = useState([])         // admin "global chat" transcript
  const [gChatInput, setGChatInput] = useState("")
  const [ha, setHa] = useState(null)                   // /admin/home-assistant snapshot
  const [haUrl, setHaUrl] = useState("")
  const [haToken, setHaToken] = useState("")           // new token to save (blank = keep stored)
  const [haDevices, setHaDevices] = useState([])       // entities from HA for the picker
  const [haAllowed, setHaAllowed] = useState([])       // working allowlist (entity_ids)
  const [haMsg, setHaMsg] = useState("")
  const [haBusy, setHaBusy] = useState(false)
  const [audit, setAudit] = useState([])               // audit-log entries
  const [backups, setBackups] = useState([])
  const [backingUp, setBackingUp] = useState(false)
  const [err, setErr] = useState("")

  const api = async (path, method = "GET", body) => {
    const opts = { method, headers: { Authorization: "Bearer " + token } }
    if (body) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body) }
    const r = await fetch(apiBase + path, opts)
    if (!r.ok) {
      if (r.status === 401 || r.status === 403) { onExit(); return {} }
      const d = await r.json().catch(() => ({}))
      throw new Error(d.detail || d.error || `Request failed (${r.status})`)
    }
    return r.json()
  }

  const load = async () => {
    try {
      const [s, u, k, f, sv, er, rc, gk, pr] = await Promise.all([
        api("/admin/stats"), api("/admin/users"), api("/admin/api_keys"),
        api("/admin/faces"), api("/admin/services"), api("/admin/faces/enroll-requests"),
        api("/admin/events?type=face_seen&limit=20"), api("/admin/knowledge/global"), api("/presence")])
      setStats(s); setUsers(u.users || []); setKeys(k.keys || [])
      setFaces(f.faces || []); setServices(sv.services || []); setEnrollReqs(er.requests || [])
      setSysInfo({ version: sv.version, summary: sv.summary, checkedAt: Date.now() })
      setRecogs(rc.events || []); setGlobalFacts(gk.facts || []); setPresent(pr.present || []); setErr("")
    } catch (e) { setErr(e.message) }
  }
  useEffect(() => { load() }, [])  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (tab === "system") { loadAudit(); loadBackups() } }, [tab])  // eslint-disable-line react-hooks/exhaustive-deps

  const loadHa = async () => {
    try {
      const d = await api("/admin/home-assistant")
      setHa(d); setHaUrl(d.url || ""); setHaAllowed(d.allowed_entities || []); setHaToken("")
    } catch (e) { setErr(e.message) }
  }
  useEffect(() => { if (tab === "smarthome") loadHa() }, [tab])  // eslint-disable-line react-hooks/exhaustive-deps

  const testHa = async () => {
    setHaBusy(true); setHaMsg("Testing…")
    try {
      const d = await api("/admin/home-assistant/test", "POST", { url: haUrl, token: haToken || undefined })
      setHaMsg((d.ok ? "✓ " : "✗ ") + d.detail)
    } catch (e) { setHaMsg("✗ " + e.message) } finally { setHaBusy(false) }
  }
  const saveHa = async () => {
    setHaBusy(true); setHaMsg("Saving…")
    try {
      const d = await api("/admin/home-assistant", "PUT", { url: haUrl, token: haToken || undefined, allowed_entities: haAllowed })
      setHaMsg(d.connected ? "✓ Saved — connected." : "Saved (not reachable — check URL/token).")
      await loadHa()
    } catch (e) { setHaMsg("✗ " + e.message) } finally { setHaBusy(false) }
  }
  const loadHaDevices = async () => {
    setHaBusy(true); setHaMsg("")
    try {
      const d = await api("/admin/home-assistant/entities")
      setHaDevices(d.entities || [])
      if (!(d.entities || []).length) setHaMsg("No devices returned — save a working connection first.")
    } catch (e) { setHaMsg("✗ " + e.message) } finally { setHaBusy(false) }
  }
  const toggleAllowed = (eid) =>
    setHaAllowed(a => a.includes(eid) ? a.filter(x => x !== eid) : [...a, eid])
  // Refresh status periodically (services, faces, enroll requests) without disrupting form edits.
  useEffect(() => {
    const t = setInterval(async () => {
      try {
        const [sv, f, er, rc, pr] = await Promise.all([
          api("/admin/services"), api("/admin/faces"), api("/admin/faces/enroll-requests"),
          api("/admin/events?type=face_seen&limit=20"), api("/presence")])
        setServices(sv.services || []); setFaces(f.faces || []); setEnrollReqs(er.requests || [])
        setSysInfo({ version: sv.version, summary: sv.summary, checkedAt: Date.now() })
        setRecogs(rc.events || []); setPresent(pr.present || [])
      } catch { /* keep last */ }
    }, 15000)
    return () => clearInterval(t)
  }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  // While an enroll is in progress, poll its live preview (fast) + status/faces (so it flips to DONE
  // and the new person appears promptly).
  const activeReqId = enrollReqs.find(r => r.status === "pending")?.id
  useEffect(() => {
    if (!activeReqId) { setPreview(null); return }
    let alive = true, waited = 0
    const STOP_AFTER = 120000   // a capture takes seconds; stop polling a stalled request (server expires it too)
    // Smooth live preview: one streaming connection that pushes each frame the agent sends (~10 fps),
    // instead of polling per frame. The status poll below still detects completion.
    const ctrl = new AbortController()
    const stream = async () => {
      try {
        const res = await fetch(`${apiBase}/faces/enroll-preview-stream?request_id=${activeReqId}`,
          { headers: { Authorization: "Bearer " + token }, signal: ctrl.signal })
        if (!res.ok || !res.body) return
        const reader = res.body.getReader(), dec = new TextDecoder()
        let buf = ""
        while (alive) {
          const { done, value } = await reader.read()
          if (done) break
          buf += dec.decode(value, { stream: true })
          let nl
          while ((nl = buf.indexOf("\n")) >= 0) {
            const line = buf.slice(0, nl); buf = buf.slice(nl + 1)
            if (line.trim()) { try { if (alive) setPreview(JSON.parse(line)) } catch { /* ignore */ } }
          }
        }
      } catch { /* aborted / network — status poll keeps the UI honest */ }
    }
    const st = async () => {
      try {
        const d = await api("/admin/faces/enroll-requests")
        if (!alive) return
        setEnrollReqs(d.requests || [])
        // refresh the people list once, when our request leaves 'pending' (don't re-fetch every tick)
        if (!(d.requests || []).some(r => r.id === activeReqId && r.status === "pending")) {
          const f = await api("/admin/faces"); if (alive) setFaces(f.faces || [])
        }
      } catch { /* ignore */ }
    }
    stream(); st()
    const t2 = setInterval(() => {
      waited += 4000
      st()
      if (waited >= STOP_AFTER) { clearInterval(t2); ctrl.abort() }   // give up; server marks it failed
    }, 4000)
    return () => { alive = false; clearInterval(t2); ctrl.abort() }
  }, [activeReqId])  // eslint-disable-line react-hooks/exhaustive-deps

  const createUser = async () => {
    if (!uName || !uPass) return setErr("Username and password required")
    try { await api("/admin/users", "POST", { username: uName, password: uPass, role: "user" }); setUName(""); setUPass(""); load() }
    catch (e) { setErr(e.message) }
  }
  const delUser = async (id) => {
    if (!confirm("Terminate this user and purge their data?")) return
    try { await api("/admin/users/" + id, "DELETE"); load() } catch (e) { setErr(e.message) }
  }
  const setRole = async (id, role) => {
    const verb = role === "admin" ? "Grant admin clearance to" : "Revoke admin clearance from"
    if (!confirm(verb + " this user?")) return
    try { await api("/admin/users/" + id + "/role", "PUT", { role }); load() } catch (e) { setErr(e.message) }
  }
  const adminCount = users.filter(u => u.role === "admin").length
  const userName = (id) => { const u = users.find(x => x.id === id); return u ? u.username : "#" + id }
  const createKey = async () => {
    if (!kUser || !kDesc) return setErr("Select a user and a designation")
    try {
      const d = await api("/admin/api_keys", "POST", { user_id: Number(kUser), description: kDesc, device_id: kDev.trim() || null })
      setMinted(d.key); setMintedDev(d.device_id || ""); setKUser(""); setKDesc(""); setKDev(""); load()
    } catch (e) { setErr(e.message) }
  }
  const delKey = async (id) => {
    if (!confirm("Sever this uplink? External scripts lose access immediately.")) return
    try { await api("/admin/api_keys/" + id, "DELETE"); load() } catch (e) { setErr(e.message) }
  }
  const delFace = async (id) => {
    if (!confirm("Delete this person and all their face embeddings?")) return
    try { await api("/admin/faces/" + id, "DELETE"); setExpanded(null); load() } catch (e) { setErr(e.message) }
  }
  const linkFace = async (id, userId) => {
    try { await api("/admin/faces/" + id, "PUT", { user_id: userId ? Number(userId) : null }); load() }
    catch (e) { setErr(e.message) }
  }
  const renameFace = async (id, current) => {
    const name = prompt("Rename person:", current)
    if (!name || name.trim() === current) return
    try { await api("/admin/faces/" + id, "PUT", { name: name.trim() }); load() } catch (e) { setErr(e.message) }
  }
  const viewEmbs = async (id) => {
    if (expanded === id) { setExpanded(null); return }
    try { const d = await api(`/admin/faces/${id}/embeddings`); setEmbs(d.embeddings || []); setExpanded(id) }
    catch (e) { setErr(e.message) }
  }
  const delEmb = async (embId, personId) => {
    if (!confirm("Delete this one embedding?")) return
    try {
      await api("/admin/faces/embeddings/" + embId, "DELETE")
      const d = await api(`/admin/faces/${personId}/embeddings`); setEmbs(d.embeddings || [])
      load()                                   // refresh the person's count
    } catch (e) { setErr(e.message) }
  }

  const requestEnroll = async () => {
    if (!enrollUser || !enrollDev) return setErr("Pick a user and a camera")
    try { await api("/admin/faces/enroll-request", "POST", { user_id: Number(enrollUser), device_id: enrollDev }); setEnrollUser(""); load() }
    catch (e) { setErr(e.message) }
  }

  const loadGlobal = async () => {
    try { const d = await api("/admin/knowledge/global"); setGlobalFacts(d.facts || []) } catch (e) { setErr(e.message) }
  }
  const addGlobal = async () => {
    const content = gContent.trim()
    if (!content) return setErr("Enter a household fact")
    try { await api("/admin/knowledge/global", "POST", { content, category: gCat }); setGContent(""); loadGlobal() }
    catch (e) { setErr(e.message) }
  }
  const editGlobal = async (f) => {
    const content = prompt("Edit household fact:", f.content)
    if (content == null || content.trim() === f.content) return
    try { await api("/admin/knowledge/global/" + f.id, "PUT", { content: content.trim(), category: f.category }); loadGlobal() }
    catch (e) { setErr(e.message) }
  }
  const delGlobal = async (id) => {
    if (!confirm("Delete this household fact?")) return
    try { await api("/admin/knowledge/global/" + id, "DELETE"); loadGlobal() } catch (e) { setErr(e.message) }
  }
  const sendGlobalChat = async () => {
    const text = gChatInput.trim()
    if (!text) return
    setGChatLog(l => [...l, { role: "you", text }])
    setGChatInput("")
    try {
      const d = await api("/admin/knowledge/global/chat", "POST", { text })
      setGChatLog(l => [...l, { role: "jarvis", text: d.reply }])
      loadGlobal()
    } catch (e) { setGChatLog(l => [...l, { role: "jarvis", text: "⚠ " + e.message }]) }
  }
  const loadAudit = async () => {
    try { const d = await api("/admin/audit?limit=200"); setAudit(d.entries || []) } catch (e) { setErr(e.message) }
  }
  const loadBackups = async () => {
    try { const d = await api("/admin/backups"); setBackups(d.backups || []) } catch (e) { setErr(e.message) }
  }
  const createBackup = async () => {
    setBackingUp(true)
    try { await api("/admin/backup", "POST"); await loadBackups() } catch (e) { setErr(e.message) } finally { setBackingUp(false) }
  }
  const delBackup = async (name) => {
    if (!confirm("Delete backup " + name + "?")) return
    try { await api("/admin/backups/" + encodeURIComponent(name), "DELETE"); loadBackups() } catch (e) { setErr(e.message) }
  }
  const downloadBackup = async (name) => {
    try {
      const res = await fetch(apiBase + "/admin/backups/" + encodeURIComponent(name), { headers: { Authorization: "Bearer " + token } })
      if (!res.ok) throw new Error("Download failed")
      const url = URL.createObjectURL(await res.blob())
      const a = document.createElement("a"); a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove()
      URL.revokeObjectURL(url)
    } catch (e) { setErr(e.message) }
  }
  const fmtBytes = (n) => n > 1e6 ? (n / 1e6).toFixed(1) + " MB" : (n / 1e3).toFixed(0) + " KB"

  // Verify recognition for one enrolled person. Recognition is motion-gated, so a new face_seen
  // only fires when the person moves — we therefore accept the latest sighting that's either NEW
  // since the click (skew-free) OR recent (within FRESH). We resolve INSTANTLY on a correct match,
  // but tolerate transient "unknown"/wrong reads until a short deadline before reporting failure.
  const verifyFace = async (face) => {
    const device = cameraDevices.length === 1 ? cameraDevices[0] : (enrollDev || cameraDevices[0])
    if (!device) return setErr("No camera available to verify on")
    let startId = 0
    try { const d0 = await api("/admin/events?type=face_seen&limit=1"); startId = d0.events?.[0]?.id || 0 }
    catch (e) { return setErr(e.message) }
    setVerifying({ id: face.id, device, ok: null, text: `Look at the camera on “${device}” (move a little)…` })
    const ageMs = (s) => { const t = Date.parse((s || "").replace(" ", "T") + "Z"); return isNaN(t) ? 0 : Date.now() - t }
    const FRESH = 12000, deadline = Date.now() + 18000
    let lastSeen = null
    const tick = async () => {
      try {
        const d = await api("/admin/events?type=face_seen&limit=10")
        const evs = (d.events || []).filter(e => e.device_id === device)   // newest first
        const newest = evs[0]
        const live = newest && (newest.id > startId || ageMs(newest.created_at) <= FRESH) ? newest : null
        if (live) {
          lastSeen = live
          if (live.data?.name === face.name) {                              // correct match → done now
            setVerifying({ id: face.id, device, ok: true, text: `✓ Recognized as ${live.data.name} (score ${live.data.score})` })
            return
          }
        }
      } catch { /* keep polling */ }
      if (Date.now() > deadline) {
        const nm = lastSeen?.data?.name, sc = lastSeen?.data?.score
        if (lastSeen && nm !== "unknown" && nm != null)
          setVerifying({ id: face.id, device, ok: false, text: `⚠ Recognized as “${nm}” (score ${sc}), not ${face.name}.` })
        else if (lastSeen)
          setVerifying({ id: face.id, device, ok: false, text: `✗ Not recognized (best score ${sc}). Try better lighting / more angles.` })
        else
          setVerifying({ id: face.id, device, ok: false, text: `No face seen on “${device}” — be in frame, move a little, and make sure the agent is running (not --dry-run).` })
        return
      }
      setTimeout(tick, 1000)
    }
    tick()
  }

  const cameras = services.filter(s => s.name.startsWith("Camera"))
  const cameraDevices = cameras.map(s => s.name.replace(/^Camera · /, "")).filter(d => d && d !== "agent")

  return (
    <div className="adm">
      <div className="adm-bar">
        <span className="adm-title">J.A.R.V.I.S Command Center</span>
        <button className="hud-btn" onClick={onExit}>Return to Console</button>
      </div>

      <div className="adm-tabs">
        {TABS.map(t => (
          <button key={t.id} className={"adm-tab" + (tab === t.id ? " active" : "")} onClick={() => setTab(t.id)}>
            {t.label}
            {t.id === "users" && <span className="adm-tab-badge">{users.length}</span>}
            {t.id === "keys" && <span className="adm-tab-badge">{keys.length}</span>}
            {t.id === "faces" && <span className="adm-tab-badge">{faces.length}</span>}
          </button>
        ))}
      </div>

      {err && <div className="adm-error">{err}</div>}

      {tab === "overview" && (
        <>
          <div className="adm-stats">
            <div className="adm-stat"><div className="adm-stat-val">{stats.users ?? "—"}</div><div className="adm-stat-lbl">Authorized Users</div></div>
            <div className="adm-stat"><div className="adm-stat-val">{stats.chats ?? "—"}</div><div className="adm-stat-lbl">Active Sessions</div></div>
            <div className="adm-stat"><div className="adm-stat-val">{stats.messages ?? "—"}</div><div className="adm-stat-lbl">Total Exchanges</div></div>
          </div>

          <div className="adm-panel">
            <h2>Present now</h2>
            <p className="adm-hint">People the cameras have recognized in the last few minutes — the
              assistant is aware of who's around.</p>
            {present.length > 0
              ? <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  {present.map(n => <span key={n} className="adm-svc-state active" style={{ padding: "4px 12px" }}>● {n}</span>)}
                </div>
              : <p className="adm-empty">No one recognized right now.</p>}
          </div>

          <div className="adm-panel">
            <h2>System Services
              {sysInfo.version && (
                <span style={{ opacity: 0.55, fontWeight: 400, fontSize: "0.68em", marginLeft: 8 }}>
                  Jarvis v{sysInfo.version}
                </span>
              )}
            </h2>
            {sysInfo.summary && (
              <p className="adm-hint">
                <span className={"adm-svc-state " + (sysInfo.summary.operational ? "active" : "inactive")}>
                  {sysInfo.summary.up}/{sysInfo.summary.total} operational
                </span>
                {sysInfo.checkedAt ? ` · checked ${new Date(sysInfo.checkedAt).toLocaleTimeString()}` : ""} · auto-refreshes every 15s
              </p>
            )}
            <p className="adm-hint">Live status of each subsystem. Camera agents report active when the
              edge device (Pi / laptop) is running and reaching the server.</p>
            <div className="adm-services">
              {services.map((sv, i) => (
                <div className="adm-svc" key={i}>
                  <span className={"status-dot " + (sv.status === "active" ? "online" : "offline")}></span>
                  <span className="adm-svc-name">{sv.name}</span>
                  <span className={"adm-svc-state " + sv.status}>{sv.status === "active" ? "ACTIVE" : "INACTIVE"}</span>
                  <span className="adm-svc-detail">{sv.detail}</span>
                </div>
              ))}
              {services.length === 0 && <div className="adm-empty">No status reported</div>}
            </div>
          </div>
        </>
      )}

      {tab === "users" && (
        <div className="adm-panel">
          <h2>User Registry</h2>
          <div className="adm-form">
            <input className="hud-input" placeholder="USERNAME" autoComplete="off" value={uName} onChange={e => setUName(e.target.value)} />
            <input className="hud-input" type="password" placeholder="PASSWORD" autoComplete="new-password" value={uPass} onChange={e => setUPass(e.target.value)} />
            <button className="hud-btn" onClick={createUser}>Authorize User</button>
          </div>
          <table className="adm-table">
            <thead><tr><th>UID</th><th>Username</th><th>Clearance</th><th>Sessions</th><th>Msgs</th><th>Established</th><th>Actions</th></tr></thead>
            <tbody>
              {users.map(u => {
                const lastAdmin = u.role === "admin" && adminCount <= 1
                return (
                  <tr key={u.id}>
                    <td>#{u.id}</td><td className="adm-em">{u.username}</td>
                    <td>{u.role === "admin" ? <span className="adm-em">admin</span> : "user"}</td>
                    <td>{u.total_chats}</td><td>{u.total_messages}</td><td>{u.created_at}</td>
                    <td style={{ display: "flex", gap: 6 }}>
                      {u.role === "admin"
                        ? <button className="hud-btn" disabled={lastAdmin}
                            title={lastAdmin ? "Can't revoke the last admin" : ""}
                            onClick={() => setRole(u.id, "user")}>Revoke admin</button>
                        : <button className="hud-btn" onClick={() => setRole(u.id, "admin")}>Make admin</button>}
                      <button className="hud-btn warn" disabled={lastAdmin}
                        title={lastAdmin ? "Can't delete the last admin" : ""}
                        onClick={() => delUser(u.id)}>Terminate</button>
                    </td>
                  </tr>
                )
              })}
              {users.length === 0 && <tr><td colSpan="7" className="adm-empty">No users</td></tr>}
            </tbody>
          </table>
        </div>
      )}

      {tab === "keys" && (
        <div className="adm-panel">
          <h2>Machine Integration Keys</h2>
          <p className="adm-hint">Set a <strong>Device ID</strong> (e.g. <code>laptop-cam</code>) to mint a
            <strong> device-bound</strong> key — required for a camera/edge agent (it may only post events as
            that device). Leave it blank for a generic integration key (e.g. Home Assistant).</p>
          <div className="adm-form">
            <select className="hud-input" value={kUser} onChange={e => setKUser(e.target.value)} style={{ maxWidth: 200 }}>
              <option value="">— select user —</option>
              {users.map(u => <option key={u.id} value={u.id}>{u.username}{u.role === "admin" ? " (admin)" : ""}</option>)}
            </select>
            <input className="hud-input" placeholder="DESIGNATION (e.g. Living-room camera)" value={kDesc} onChange={e => setKDesc(e.target.value)} />
            <input className="hud-input" placeholder="DEVICE ID (optional, e.g. laptop-cam)" value={kDev} onChange={e => setKDev(e.target.value)} style={{ maxWidth: 230 }} />
            <button className="hud-btn" onClick={createKey}>Generate Uplink</button>
          </div>
          {minted && <div className="adm-minted">
            UPLINK ESTABLISHED · copy now (shown once): <strong>{minted}</strong>
            {mintedDev && <div style={{ marginTop: 8, fontSize: '0.78rem' }}>
              Device-bound to <strong>{mintedDev}</strong> · on that device save it with
              <code> set-key.ps1 {minted}</code> (Unix: <code>bash set-key.sh {minted}</code>), then run
              <code> .venv\Scripts\python -m jarvis_camera.agent</code>.
            </div>}
          </div>}
          <table className="adm-table">
            <thead><tr><th>Key</th><th>User</th><th>Designation</th><th>Device</th><th>Requests</th><th>Last Ping</th><th>Established</th><th>Override</th></tr></thead>
            <tbody>
              {keys.map(k => (
                <tr key={k.id}>
                  <td><code>{k.key_string}</code></td><td className="adm-em">{userName(k.user_id)}</td><td>{k.description}</td>
                  <td>{k.device_id ? <span className="adm-em">{k.device_id}</span> : <span style={{ opacity: 0.4 }}>—</span>}</td>
                  <td>{k.usage_count || 0}</td><td>{k.last_used_at || "Never"}</td><td>{k.created_at}</td>
                  <td><button className="hud-btn warn" onClick={() => delKey(k.id)}>Sever</button></td>
                </tr>
              ))}
              {keys.length === 0 && <tr><td colSpan="8" className="adm-empty">No keys</td></tr>}
            </tbody>
          </table>
        </div>
      )}

      {tab === "faces" && (
        <>
          <div className="adm-panel">
            <h2>Camera Agents</h2>
            <p className="adm-hint">Where face recognition runs. Active = the edge device is running and
              reaching the server (heartbeat within 90s). To connect one: mint a <strong>device-bound key</strong>
              in the <button className="adm-link" onClick={() => setTab("keys")}>Keys</button> tab, save it to
              <code> camera/config/agent.key</code> on that device, and run the agent.</p>
            <div className="adm-services">
              {cameras.map((sv, i) => (
                <div className="adm-svc" key={i}>
                  <span className={"status-dot " + (sv.status === "active" ? "online" : "offline")}></span>
                  <span className="adm-svc-name">{sv.name}</span>
                  <span className={"adm-svc-state " + sv.status}>{sv.status === "active" ? "ACTIVE" : "INACTIVE"}</span>
                  <span className="adm-svc-detail">{sv.detail}</span>
                </div>
              ))}
              {cameras.length === 0 && <div className="adm-empty">No camera agent has reported yet</div>}
            </div>
          </div>

          <div className="adm-panel">
            <h2>Enroll a face (from a camera)</h2>
            <p className="adm-hint">Pick the user and a camera — the request goes to that device's agent,
              which captures + registers the face on-device (the person there should look at the camera) and
              links it to the chosen account. Run it again for the same user to add more angles.
              <strong> CLI alternative:</strong>
              <code> .venv\Scripts\python -m jarvis_camera.facecli add --name "Name"</code>.</p>
            <div className="adm-form">
              <select className="hud-input" value={enrollUser} onChange={e => setEnrollUser(e.target.value)} style={{ maxWidth: 220 }}>
                <option value="">— select user —</option>
                {users.map(u => <option key={u.id} value={u.id}>{u.username}</option>)}
              </select>
              <select className="hud-input" value={enrollDev} onChange={e => setEnrollDev(e.target.value)} style={{ maxWidth: 220 }}>
                <option value="">— select camera —</option>
                {cameraDevices.map(d => <option key={d} value={d}>{d}</option>)}
              </select>
              <button className="hud-btn" onClick={requestEnroll} disabled={cameraDevices.length === 0 || users.length === 0}>Request Enrollment</button>
            </div>
            {cameraDevices.length === 0 && <p className="adm-hint">No camera agents seen yet — start one (run the agent) so it can receive the request.</p>}
            {activeReqId && (
              <div style={{ margin: "12px 0" }}>
                {preview && preview.image ? (
                  <>
                    <img src={`data:image/jpeg;base64,${preview.image}`} alt="live enroll preview"
                         style={{ maxWidth: 480, width: "100%", border: "1px solid var(--holo-cyan)", display: "block", clipPath: "var(--clip-angle-sm)" }} />
                    <p className="adm-hint">● LIVE — capturing {preview.captured}/{preview.total}. Look at the camera; the green box is the detected face.</p>
                  </>
                ) : (
                  <p className="adm-hint">Waiting for the camera feed… make sure the agent is running on that device.</p>
                )}
              </div>
            )}
            {enrollReqs.length > 0 && (
              <table className="adm-table" style={{ marginTop: 4 }}>
                <thead><tr><th>Requested</th><th>Name</th><th>Camera</th><th>Status</th></tr></thead>
                <tbody>
                  {enrollReqs.slice(0, 6).map(r => (
                    <tr key={r.id}>
                      <td>{r.created_at}</td><td className="adm-em">{r.name}</td><td>{r.device_id}</td>
                      <td><span className={"adm-svc-state " + (r.status === "done" ? "active" : r.status === "failed" ? "inactive" : "")}>
                        {r.status.toUpperCase()}</span>{r.detail ? ` · ${r.detail}` : ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <div className="adm-panel">
            <h2>Enrolled People</h2>
            <p className="adm-hint">
              Link a person to a user to gate device actions by who's present; expand the embedding
              count to view/remove individual captures. More embeddings (different angles) = better accuracy.
            </p>
            <table className="adm-table">
              <thead><tr><th>Name</th><th>Linked User (authorization)</th><th>Embeddings</th><th>Last seen</th><th>Actions</th></tr></thead>
              <tbody>
                {faces.map(f => (
                  <Fragment key={f.id}>
                    <tr>
                      <td className="adm-em">{f.name}</td>
                      <td>
                        <select className="hud-input" value={f.user_id || ""} onChange={e => linkFace(f.id, e.target.value)} style={{ maxWidth: 200 }}>
                          <option value="">— not linked —</option>
                          {users.map(u => <option key={u.id} value={u.id}>{u.username}</option>)}
                        </select>
                      </td>
                      <td>
                        <button className="adm-link" onClick={() => viewEmbs(f.id)}>
                          {f.embedding_count} {f.embedding_count === 1 ? "embedding" : "embeddings"} {expanded === f.id ? "▾" : "▸"}
                        </button>
                      </td>
                      <td>{f.last_seen || <span style={{ opacity: 0.4 }}>never</span>}</td>
                      <td style={{ display: "flex", gap: 6 }}>
                        <button className="hud-btn" onClick={() => verifyFace(f)}
                                disabled={cameraDevices.length === 0 || (verifying && verifying.ok === null)}>Verify</button>
                        <button className="hud-btn" onClick={() => renameFace(f.id, f.name)}>Rename</button>
                        <button className="hud-btn warn" onClick={() => delFace(f.id)}>Delete</button>
                      </td>
                    </tr>
                    {verifying && verifying.id === f.id && (
                      <tr><td colSpan="5" className={verifying.ok === true ? "adm-em" : ""}
                              style={{ color: verifying.ok === true ? "var(--holo-cyan)" : verifying.ok === false ? "var(--holo-amber, #f0a)" : undefined }}>
                        {verifying.ok === null ? "● " : ""}{verifying.text}
                      </td></tr>
                    )}
                    {expanded === f.id && (
                      <tr><td colSpan="5" style={{ background: "rgba(103,199,235,0.03)" }}>
                        {embs.length === 0 ? <span className="adm-empty">No embeddings (re-enroll to add one)</span> : (
                          <table className="adm-table" style={{ margin: 0 }}>
                            <thead><tr><th>#</th><th>Source</th><th>Added</th><th></th></tr></thead>
                            <tbody>
                              {embs.map(e => (
                                <tr key={e.id}>
                                  <td>{e.id}</td><td>{e.source || "—"}</td><td>{e.created_at}</td>
                                  <td><button className="hud-btn warn" onClick={() => delEmb(e.id, f.id)}>Delete</button></td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        )}
                      </td></tr>
                    )}
                  </Fragment>
                ))}
                {faces.length === 0 && <tr><td colSpan="5" className="adm-empty">No one enrolled yet — use the enroll command above.</td></tr>}
              </tbody>
            </table>
          </div>

          <div className="adm-panel">
            <h2>Recent recognitions</h2>
            <p className="adm-hint">Live face sightings reported by the cameras (most recent first, auto-refreshing).
              A name = matched an enrolled person; <code>unknown</code> = a face below the match threshold.</p>
            <table className="adm-table">
              <thead><tr><th>When</th><th>Who</th><th>Score</th><th>Camera</th></tr></thead>
              <tbody>
                {recogs.map(e => (
                  <tr key={e.id}>
                    <td>{e.created_at}</td>
                    <td className={e.data?.name && e.data.name !== "unknown" ? "adm-em" : ""}
                        style={{ opacity: e.data?.name === "unknown" ? 0.6 : 1 }}>{e.data?.name || "—"}</td>
                    <td>{e.data?.score ?? "—"}</td>
                    <td>{e.device_id}</td>
                  </tr>
                ))}
                {recogs.length === 0 && <tr><td colSpan="4" className="adm-empty">No sightings yet — run a camera (not --dry-run) and step into frame.</td></tr>}
              </tbody>
            </table>
          </div>
        </>
      )}

      {tab === "household" && (
        <>
        <div className="adm-panel">
          <h2>Teach JARVIS (global chat)</h2>
          <p className="adm-hint">
            Just tell JARVIS about the home — <strong>each line you send becomes a household fact</strong>.
            Admin-only; nothing here is personal. (For precise edits, use the table below.)
          </p>
          {gChatLog.length > 0 && (
            <div className="adm-chatlog" style={{ maxHeight: 200, overflowY: "auto", margin: "8px 0", display: "flex", flexDirection: "column", gap: 4 }}>
              {gChatLog.map((m, i) => (
                <div key={i} style={{ opacity: m.role === "jarvis" ? 0.8 : 1 }}>
                  <span className="adm-em">{m.role === "jarvis" ? "JARVIS" : "You"}:</span> {m.text}
                </div>
              ))}
            </div>
          )}
          <div className="adm-form">
            <input className="hud-input" placeholder="e.g. The WiFi password is hunter2 — or paste several lines"
                   value={gChatInput} onChange={e => setGChatInput(e.target.value)}
                   onKeyDown={e => { if (e.key === "Enter") sendGlobalChat() }} style={{ flex: 1 }} />
            <button className="hud-btn" onClick={sendGlobalChat}>Send</button>
          </div>
        </div>

        <div className="adm-panel">
          <h2>Household knowledge (shared)</h2>
          <p className="adm-hint">
            Facts about <strong>this home</strong> — rooms, address, who sleeps where, device
            locations. These are added to <em>every</em> user's prompt (personal chats stay private).
            Admin-curated only. You can also load these programmatically via
            <code> POST /admin/knowledge/global</code>.
          </p>
          <div className="adm-form">
            <select className="hud-input" value={gCat} onChange={e => setGCat(e.target.value)} style={{ maxWidth: 160 }}>
              {KNOWLEDGE_CATEGORIES.map(c => <option key={c} value={c}>{c}</option>)}
            </select>
            <input className="hud-input" placeholder="e.g. The master bedroom is on the top floor"
                   value={gContent} onChange={e => setGContent(e.target.value)}
                   onKeyDown={e => { if (e.key === "Enter") addGlobal() }} style={{ flex: 1 }} />
            <button className="hud-btn" onClick={addGlobal}>Add fact</button>
          </div>
          <table className="adm-table" style={{ marginTop: 4 }}>
            <thead><tr><th>Category</th><th>Fact</th><th>Updated</th><th>Actions</th></tr></thead>
            <tbody>
              {globalFacts.map(f => (
                <tr key={f.id}>
                  <td className="adm-em">{f.category}</td>
                  <td>{f.content}</td>
                  <td>{f.updated_at}</td>
                  <td style={{ display: "flex", gap: 6 }}>
                    <button className="hud-btn" onClick={() => editGlobal(f)}>Edit</button>
                    <button className="hud-btn warn" onClick={() => delGlobal(f.id)}>Delete</button>
                  </td>
                </tr>
              ))}
              {globalFacts.length === 0 && <tr><td colSpan="4" className="adm-empty">No household facts yet — add the home's shared details above.</td></tr>}
            </tbody>
          </table>
        </div>
        </>
      )}

      {tab === "smarthome" && (
        <>
        <div className="adm-panel">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <h2>Home Assistant</h2>
            <span className={"adm-pill" + (ha?.connected ? " ok" : "")}>
              {ha == null ? "…" : ha.connected ? "Connected" : ha.configured ? "Not reachable" : "Not configured"}
            </span>
          </div>
          <p className="adm-hint">Control your smart-home devices by chat or voice. Paste your Home
            Assistant URL and a <b>long-lived access token</b> (create one from a dedicated non-admin HA
            user → Profile → Security). The token is stored on the server and never shown to the AI —
            it can only act on the devices you allow below. See <code>docs/setup/home-assistant.md</code>.</p>

          {ha?.env_managed ? (
            <p className="adm-hint" style={{ opacity: 0.9 }}>⚙️ Configured via environment variables
              (<code>HA_URL</code>/<code>HA_TOKEN</code>) — edit those to change. URL: <code>{ha.url}</code></p>
          ) : (
            <>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap", margin: "8px 0" }}>
                <input className="hud-input" placeholder="http://homeassistant.local:8123"
                  value={haUrl} onChange={e => setHaUrl(e.target.value)} style={{ flex: "1 1 320px" }} />
                <input className="hud-input" type="password" autoComplete="new-password"
                  placeholder={ha?.token_set ? "•••••••• (saved — blank to keep)" : "long-lived access token"}
                  value={haToken} onChange={e => setHaToken(e.target.value)} style={{ flex: "1 1 320px" }} />
              </div>
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <button className="hud-btn" onClick={testHa} disabled={haBusy}>Test connection</button>
                <button className="hud-btn" onClick={saveHa} disabled={haBusy}>Save</button>
                {haMsg && <span className="adm-hint" style={{ margin: 0 }}>{haMsg}</span>}
              </div>
            </>
          )}
        </div>

        <div className="adm-panel">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <h2>Allowed devices <span className="adm-tab-badge">{haAllowed.length}</span></h2>
            <button className="hud-btn" onClick={loadHaDevices} disabled={haBusy || !ha?.configured}>Load devices from HA</button>
          </div>
          <p className="adm-hint">Tick the devices Jarvis may control. Only these can ever be actuated —
            everything else in your home stays off-limits, even if asked. Click <b>Load devices</b> to pull
            the list from Home Assistant, then <b>Save</b> on the panel above.</p>
          {haDevices.length > 0 ? (
            <table className="adm-table">
              <thead><tr><th>Allow</th><th>Device</th><th>Entity ID</th><th>Domain</th><th>State</th></tr></thead>
              <tbody>
                {haDevices.map(d => (
                  <tr key={d.entity_id}>
                    <td><input type="checkbox" checked={haAllowed.includes(d.entity_id)}
                      onChange={() => toggleAllowed(d.entity_id)} /></td>
                    <td className="adm-em">{d.name}</td>
                    <td style={{ opacity: 0.8, fontFamily: "monospace" }}>{d.entity_id}</td>
                    <td>{d.domain}</td>
                    <td>{d.state}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : haAllowed.length > 0 ? (
            <ul className="adm-hint">{haAllowed.map(e => <li key={e}><code>{e}</code></li>)}</ul>
          ) : (
            <p className="adm-empty">No devices allowlisted yet — Load devices, tick some, and Save.</p>
          )}
        </div>
        </>
      )}

      {tab === "system" && (
        <>
        <div className="adm-panel">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <h2>Backups</h2>
            <button className="hud-btn" onClick={createBackup} disabled={backingUp}>{backingUp ? "Backing up…" : "Back up now"}</button>
          </div>
          <p className="adm-hint">A snapshot of your data — the database (users, knowledge, history) +
            the vector store. Sensitive (kept owner-only on the server); download to keep a copy off-box.
            Restore is manual — see <code>docs/setup/backup.md</code>.</p>
          <table className="adm-table">
            <thead><tr><th>Backup</th><th>Size</th><th>Created</th><th>Actions</th></tr></thead>
            <tbody>
              {backups.map(b => (
                <tr key={b.name}>
                  <td className="adm-em">{b.name}</td>
                  <td>{fmtBytes(b.size)}</td>
                  <td>{b.created_at}</td>
                  <td style={{ display: "flex", gap: 6 }}>
                    <button className="hud-btn" onClick={() => downloadBackup(b.name)}>Download</button>
                    <button className="hud-btn warn" onClick={() => delBackup(b.name)}>Delete</button>
                  </td>
                </tr>
              ))}
              {backups.length === 0 && <tr><td colSpan="4" className="adm-empty">No backups yet — click "Back up now".</td></tr>}
            </tbody>
          </table>
        </div>

        <div className="adm-panel">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <h2>Audit log</h2>
            <button className="hud-btn" onClick={loadAudit}>Refresh</button>
          </div>
          <p className="adm-hint">Who did what — device control and admin changes (most recent first).</p>
          <table className="adm-table">
            <thead><tr><th>When</th><th>User</th><th>Action</th><th>Detail</th></tr></thead>
            <tbody>
              {audit.map(e => (
                <tr key={e.id}>
                  <td>{e.created_at}</td>
                  <td className="adm-em">{e.username || (e.user_id != null ? "#" + e.user_id : "—")}</td>
                  <td>{e.action}</td>
                  <td style={{ opacity: 0.85 }}>{e.detail}</td>
                </tr>
              ))}
              {audit.length === 0 && <tr><td colSpan="4" className="adm-empty">No audit entries yet.</td></tr>}
            </tbody>
          </table>
        </div>
        </>
      )}
    </div>
  )
}
