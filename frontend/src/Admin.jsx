import { useState, useEffect } from 'react'

// Admin console, rendered by App when the path is /admin and the user is an admin.
// Lives in the React app so it inherits the HUD styling, fonts, and active theme.
export default function Admin({ token, onExit }) {
  const [stats, setStats] = useState({})
  const [users, setUsers] = useState([])
  const [keys, setKeys] = useState([])
  const [faces, setFaces] = useState([])
  const [uName, setUName] = useState("")
  const [uPass, setUPass] = useState("")
  const [kUser, setKUser] = useState("")
  const [kDesc, setKDesc] = useState("")
  const [minted, setMinted] = useState("")
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
      const [s, u, k, f] = await Promise.all([api("/admin/stats"), api("/admin/users"), api("/admin/api_keys"), api("/admin/faces")])
      setStats(s); setUsers(u.users || []); setKeys(k.keys || []); setFaces(f.faces || []); setErr("")
    } catch (e) { setErr(e.message) }
  }
  useEffect(() => { load() }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  const createUser = async () => {
    if (!uName || !uPass) return setErr("Username and password required")
    try { await api("/admin/users", "POST", { username: uName, password: uPass, role: "user" }); setUName(""); setUPass(""); load() }
    catch (e) { setErr(e.message) }
  }
  const delUser = async (id) => {
    if (!confirm("Terminate this user and purge their data?")) return
    try { await api("/admin/users/" + id, "DELETE"); load() } catch (e) { setErr(e.message) }
  }
  const createKey = async () => {
    if (!kUser || !kDesc) return setErr("Target UID and designation required")
    try { const d = await api("/admin/api_keys", "POST", { user_id: Number(kUser), description: kDesc }); setMinted(d.key); setKUser(""); setKDesc(""); load() }
    catch (e) { setErr(e.message) }
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

  return (
    <div className="adm">
      <div className="adm-bar">
        <span className="adm-title">J.A.R.V.I.S Command Center</span>
        <button className="hud-btn" onClick={onExit}>Return to Console</button>
      </div>

      {err && <div className="adm-error">{err}</div>}

      <div className="adm-stats">
        <div className="adm-stat"><div className="adm-stat-val">{stats.users ?? "—"}</div><div className="adm-stat-lbl">Authorized Users</div></div>
        <div className="adm-stat"><div className="adm-stat-val">{stats.chats ?? "—"}</div><div className="adm-stat-lbl">Active Sessions</div></div>
        <div className="adm-stat"><div className="adm-stat-val">{stats.messages ?? "—"}</div><div className="adm-stat-lbl">Total Exchanges</div></div>
      </div>

      <div className="adm-panel">
        <h2>User Registry</h2>
        <div className="adm-form">
          <input className="hud-input" placeholder="USERNAME" autoComplete="off" value={uName} onChange={e => setUName(e.target.value)} />
          <input className="hud-input" type="password" placeholder="PASSWORD" autoComplete="new-password" value={uPass} onChange={e => setUPass(e.target.value)} />
          <button className="hud-btn" onClick={createUser}>Authorize User</button>
        </div>
        <table className="adm-table">
          <thead><tr><th>UID</th><th>Username</th><th>Clearance</th><th>Sessions</th><th>Msgs</th><th>Established</th><th>Override</th></tr></thead>
          <tbody>
            {users.map(u => (
              <tr key={u.id}>
                <td>#{u.id}</td><td className="adm-em">{u.username}</td><td>{u.role}</td>
                <td>{u.total_chats}</td><td>{u.total_messages}</td><td>{u.created_at}</td>
                <td><button className="hud-btn warn" onClick={() => delUser(u.id)}>Terminate</button></td>
              </tr>
            ))}
            {users.length === 0 && <tr><td colSpan="7" className="adm-empty">No users</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="adm-panel">
        <h2>Machine Integration Keys</h2>
        <div className="adm-form">
          <input className="hud-input" placeholder="TARGET UID" value={kUser} onChange={e => setKUser(e.target.value)} style={{ maxWidth: 130 }} />
          <input className="hud-input" placeholder="DESIGNATION (e.g. Home Assistant)" value={kDesc} onChange={e => setKDesc(e.target.value)} />
          <button className="hud-btn" onClick={createKey}>Generate Uplink</button>
        </div>
        {minted && <div className="adm-minted">UPLINK ESTABLISHED · copy now (shown once): <strong>{minted}</strong></div>}
        <table className="adm-table">
          <thead><tr><th>Key</th><th>UID</th><th>Designation</th><th>Requests</th><th>Last Ping</th><th>Established</th><th>Override</th></tr></thead>
          <tbody>
            {keys.map(k => (
              <tr key={k.id}>
                <td><code>{k.key_string}</code></td><td>#{k.user_id}</td><td>{k.description}</td>
                <td>{k.usage_count || 0}</td><td>{k.last_used_at || "Never"}</td><td>{k.created_at}</td>
                <td><button className="hud-btn warn" onClick={() => delKey(k.id)}>Sever</button></td>
              </tr>
            ))}
            {keys.length === 0 && <tr><td colSpan="7" className="adm-empty">No keys</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="adm-panel">
        <h2>Enrolled Faces</h2>
        <p style={{ fontSize: '0.75rem', color: 'rgba(103,199,235,0.6)', marginBottom: '14px', lineHeight: 1.6 }}>
          Enroll on the device (which has the camera + embedding model):
          <code> uv run --no-project python -m jarvis_edge.enroll --name "Name" </code>.
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
    </div>
  )
}
