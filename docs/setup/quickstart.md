# Quick start — server → device, end to end

The whole path in one place. For depth on any piece, see the per-component guides: **server**
([server.md](server.md)), **TLS** ([tls.md](tls.md)), **camera** ([camera.md](camera.md)), **voice**
([voice.md](voice.md)), **volume agent** ([volume-agent.md](volume-agent.md)).

Mental model: **server** = one script (bootstrap + HTTPS) + mint a key; **each device** = one setup
script + drop in two files (`ca.crt`, `agent.key`) + run.

---

## Part A — Server (the box, e.g. `192.168.1.20`)

```bash
git clone https://github.com/Ravijangid820/jarvis.git /srv/jarvis && cd /srv/jarvis
sudo bash src/scripts/setup-server.sh        # bootstrap + systemd services + local-CA HTTPS
curl --cacert tls/ca.crt https://127.0.0.1:5000/health        # → {"status":"ok",...}
```

Notes: the embedding model is Gemma-gated → `uv run huggingface-cli login` (or `HF_TOKEN=…`) first;
set `LLM_GGUF_URL=<url>` if the LLM isn't downloaded; options: `JARVIS_USER=`, `SKIP_TLS=1`,
`ADMIN_USER=`/`ADMIN_PASS=`. Note the **CA fingerprint** the script prints.

Mint the camera's key (under a **non-admin** user — or do it in the web UI, Admin → Keys):

```bash
uv run python src/scripts/manage.py mint-key <non-admin-user> laptop-cam laptop-cam   # prints jk-…
```

---

## Part B — Device (the laptop / Pi with the camera)

**Windows**
```powershell
winget install astral-sh.uv                                   # once; open a new terminal after
git clone https://github.com/Ravijangid820/jarvis.git ; cd jarvis\camera
powershell -ExecutionPolicy Bypass -File setup.ps1            # venv + deps + face models
```

**Linux / macOS / Raspberry Pi**
```bash
git clone https://github.com/Ravijangid820/jarvis.git && cd jarvis/camera
bash setup.sh                                                 # auto-detects platform; deps + models
```

Then on the device, **by hand** (two files):

1. **Trust the server** — copy the server's `tls/ca.crt` to **`camera/config/ca.crt`** (grab it off the
   box, or download `https://<server>:5000/ca.crt`). Optionally check its SHA-256 matches the
   fingerprint from Part A.
2. **Authenticate** — save the `jk-…` key to **`camera/config/agent.key`**:
   - Windows: `powershell -ExecutionPolicy Bypass -File set-key.ps1 jk-…`
   - Linux/Pi: `bash set-key.sh jk-…`
3. Check `camera/config/config.json` → `server.url` is `https://<server-ip>:5000`.

Run it:
```powershell
powershell -ExecutionPolicy Bypass -File run.ps1 --dry-run    # test (Windows; Linux: bash run.sh --dry-run)
powershell -ExecutionPolicy Bypass -File run.ps1              # live → turns GREEN in Admin → Overview
powershell -ExecutionPolicy Bypass -File service.ps1 install  # optional: autostart at logon
```

---

## Part C — Use it (browser / phone)

- **Trust the CA in your browser** so `https://…` is clean: import `ca.crt` →
  - Windows: `Import-Certificate -FilePath .\camera\config\ca.crt -CertStoreLocation Cert:\CurrentUser\Root`
    (or double-click → Install → Current User → *Trusted Root Certification Authorities*).
  - Firefox keeps its own store: Settings → Certificates → Authorities → Import.
- Browse **`https://192.168.1.20:5000`** → log in → **Admin → Faces → "Enroll a face"** (pick the
  camera + a name, watch the live preview).
- **Phone:** open `https://<server>:5000/ca.crt`, install it (Android: Settings → Security → Install a
  certificate → CA certificate; iOS: install profile → Certificate Trust Settings), then browse the UI.

---

## Recap (least commands)

| | Command(s) |
|---|---|
| **Server** | `git clone …` → `sudo bash src/scripts/setup-server.sh` → `mint-key …` |
| **Device** | `setup.ps1`/`setup.sh` → copy `ca.crt` + `set-key …` → `run.ps1`/`run.sh` |
| **Browser** | import `ca.crt` → open `https://<server>:5000` |
