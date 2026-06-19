import { useState, useEffect } from 'react'

// Admin console, rendered by App when the path is /admin and the user is an admin.
// Lives in the React app so it inherits the HUD styling, fonts, and active theme.
const TABS = [
  { id: "overview", label: "Overview" },
  { id: "users", label: "Users" },
  { id: "keys", label: "Keys" },
  { id: "faces", label: "Faces" },
]

export default function Admin({ token, onExit }) {
  const [tab, setTab] = useState("overview")
  const [stats, setStats] = useState({})
  const [users, setUsers] = useState([])
  const [keys, setKeys] = useState([])
  const [faces, setFaces] = useState([])
  const [services, setServices] = useState([])
  const [uName, setUName] = useState("")
  const [uPass, setUPass] = useState("")
  const [kUser, setKUser] = useState("")
  const [kDesc, setKDesc] = useState("")
  const [kDev, setKDev] = useState("")
  const [minted, setMinted] = useState("")
  const [mintedDev, setMintedDev] = useState("")
  const [err, setErr] = useState("")

  const api = async (path, method = "GET", body) => {
    const opts = { method, headers: { Authorization: "Bearer " + token } }
    if (body) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body) }
    const r = await fetch(path, opts)
    if (!r.ok) {
      if (r.status === 401 || r.status === 403) { onExit(); return {} }
      const d = await r.json().catch(() => ({}))
      throw new Error(d.detail || "Request failed")
    }
    return r.json()
  }

  const load = async () => {
    try {
      const [s, u, k, f, sv] = await Promise.all([
        api("/admin/stats"), api("/admin/users"), api("/admin/api_keys"),
        api("/admin/faces"), api("/admin/services")])
      setStats(s); setUsers(u.users || []); setKeys(k.keys || [])
      setFaces(f.faces || []); setServices(sv.services || []); setErr("")
    } catch (e) { setErr(e.message) }
  }
  useEffect(() => { load() }, [])  // eslint-disable-line react-hooks/exhaustive-deps
  // Refresh service status periodically so green/red reflects reality without a manual reload.
  useEffect(() => {
    const t = setInterval(async () => {
      try { const sv = await api("/admin/services"); setServices(sv.services || []) } catch { /* keep last */ }
    }, 15000)
    return () => clearInterval(t)
  }, [])  // eslint-disable-line react-hooks/exhaustive-deps

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
  const createKey = async () => {
    if (!kUser || !kDesc) return setErr("Target UID and designation required")
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
    if (!confirm("Delete this enrolled face?")) return
    try { await api("/admin/faces/" + id, "DELETE"); load() } catch (e) { setErr(e.message) }
  }
  const linkFace = async (id, userId) => {
    try { await api("/admin/faces/" + id, "PUT", { user_id: userId ? Number(userId) : null }); load() }
    catch (e) { setErr(e.message) }
  }

  const cameras = services.filter(s => s.name.startsWith("Camera"))

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
            <h2>System Services</h2>
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
            <input className="hud-input" placeholder="TARGET UID" value={kUser} onChange={e => setKUser(e.target.value)} style={{ maxWidth: 130 }} />
            <input className="hud-input" placeholder="DESIGNATION (e.g. Living-room camera)" value={kDesc} onChange={e => setKDesc(e.target.value)} />
            <input className="hud-input" placeholder="DEVICE ID (optional, e.g. laptop-cam)" value={kDev} onChange={e => setKDev(e.target.value)} style={{ maxWidth: 230 }} />
            <button className="hud-btn" onClick={createKey}>Generate Uplink</button>
          </div>
          {minted && <div className="adm-minted">
            UPLINK ESTABLISHED · copy now (shown once): <strong>{minted}</strong>
            {mintedDev && <div style={{ marginTop: 8, fontSize: '0.78rem' }}>
              Device-bound to <strong>{mintedDev}</strong> · on that device, save this into
              <code> camera/config/agent.key</code> (Windows: <code>camera\config\agent.key</code>), then run
              <code> .venv\Scripts\python -m jarvis_camera.agent</code>.
            </div>}
          </div>}
          <table className="adm-table">
            <thead><tr><th>Key</th><th>UID</th><th>Designation</th><th>Device</th><th>Requests</th><th>Last Ping</th><th>Established</th><th>Override</th></tr></thead>
            <tbody>
              {keys.map(k => (
                <tr key={k.id}>
                  <td><code>{k.key_string}</code></td><td>#{k.user_id}</td><td>{k.description}</td>
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
            <h2>Enrolled Faces</h2>
            <p className="adm-hint">
              Enroll on the device (which has the camera + embedding model + an admin key):
              <code> .venv\Scripts\python -m jarvis_camera.facecli add --name "Name" </code>.
              Link a face to a user to gate device actions by who's present.
            </p>
            <table className="adm-table">
              <thead><tr><th>Name</th><th>Linked User (authorization)</th><th>Enrolled</th><th>Override</th></tr></thead>
              <tbody>
                {faces.map(f => (
                  <tr key={f.id}>
                    <td className="adm-em">{f.name}</td>
                    <td>
                      <select className="hud-input" value={f.user_id || ""} onChange={e => linkFace(f.id, e.target.value)} style={{ maxWidth: 200 }}>
                        <option value="">— not linked —</option>
                        {users.map(u => <option key={u.id} value={u.id}>{u.username}</option>)}
                      </select>
                    </td>
                    <td>{f.created_at}</td>
                    <td><button className="hud-btn warn" onClick={() => delFace(f.id)}>Delete</button></td>
                  </tr>
                ))}
                {faces.length === 0 && <tr><td colSpan="4" className="adm-empty">No faces enrolled — use the enroll command above.</td></tr>}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}
