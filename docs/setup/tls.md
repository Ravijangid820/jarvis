# TLS / HTTPS setup (local CA)

Run the orchestrator over **HTTPS** so everything on the LAN is encrypted (login + session tokens,
machine API keys, vision events, and the enroll-preview frames) and clients can authenticate the
server against a man-in-the-middle.

> **Why a local CA, not Let's Encrypt:** the box is reached by **LAN IP / a local hostname**, not a
> public domain, so public ACME can't issue for it. Instead each deployment generates **its own**
> Certificate Authority, trusts it on its devices, and issues the server a cert for its IP/hostnames.
> The CA is **per-deployment** — certs are never committed to the repo.

---

## 1. Server: generate the CA + cert

On the box (one time):

```bash
bash src/scripts/setup_tls.sh
#   custom IP / hostnames:
#   TLS_IP=192.168.1.50 TLS_HOSTS="localhost jarvis.lan" bash src/scripts/setup_tls.sh
```

This writes to `tls/` (gitignored):

| File | What | Secrecy |
|---|---|---|
| `ca.crt` | CA certificate (the trust anchor) | **public** — distribute to devices |
| `ca.key` | CA private key (signs certs) | **secret** — root-only, never leaves the box |
| `server.crt` / `server.key` | the server's cert + key (read by the service) | key is service-readable only |

It prints the **CA fingerprint (SHA-256)** — note it; devices verify against it.

**Hostname / SAN:** the cert is valid only for the names/IPs in its *Subject Alternative Name* list
(default: `127.0.0.1`, `192.168.1.20`, `localhost`, `jarvis.local`). A client must connect using one
of those **and** that name must resolve to the box. The **IP works with no DNS**; a hostname like
`jarvis.local` needs mDNS, a hosts-file entry, or your router's DNS. Keep the box on a **static IP**
so the cert/URLs don't break.

## 2. Server: enable HTTPS

A systemd drop-in adds the cert to uvicorn (`--ssl-certfile/--ssl-keyfile`):

```bash
sudo mkdir -p /etc/systemd/system/jarvis-orchestrator.service.d
sudo cp systemd/jarvis-orchestrator.service.d/tls.conf /etc/systemd/system/jarvis-orchestrator.service.d/
sudo systemctl daemon-reload && sudo systemctl restart jarvis-orchestrator
# verify:
curl --cacert tls/ca.crt https://127.0.0.1:5000/health      # {"status":"ok",...}
```

Reversible: remove the drop-in → `daemon-reload` → restart to go back to HTTP. The server now also
publishes its public CA at **`GET https://<server>:5000/ca.crt`** (no auth — it's the public cert).

## 3. Devices: trust the CA

Each device needs **that server's** `ca.crt`. Get it once — copy `tls/ca.crt` off the box, or open
`https://<server>:5000/ca.crt` and save it. **Compare its SHA-256 to the fingerprint from step 1**
before trusting it.

### Camera agent (laptop / Pi)
Put the file at **`camera/config/ca.crt`** (copy/paste it there). The config defaults to
`server.url: https://…` + `ca_cert: config/ca.crt`, so the agent then verifies the server.
Verification is never disabled — without the CA it fails closed.

### Desktop browser
Open `https://<server>:5000/ca.crt`, then import it as a **trusted root**:
- **Windows:** double-click → Install → *Local Machine* / *Current User* → *Trusted Root Certification Authorities*.
- **macOS:** add to **Keychain** → set *Always Trust*.
- **Firefox:** Settings → Privacy → Certificates → Authorities → Import.

### Phone (browses the web UI only — no agent)
- **Android:** open `https://<server>:5000/ca.crt` to download → **Settings → Security → Encryption &
  credentials → Install a certificate → CA certificate** → pick the file. Android warns "network may
  be monitored" (normal for a private CA); Chrome then trusts the site.
- **iOS:** open the URL → install the profile → **Settings → General → About → Certificate Trust
  Settings** → enable full trust for the Jarvis CA.

---

## Renewing / regenerating

- The **server cert** is valid 825 days; re-run `setup_tls.sh` to re-issue it (the CA is **reused**, so
  devices that already trust the CA keep working — just restart the service).
- If you **regenerate the CA** (delete `tls/` first), every device must re-copy `ca.crt` / re-import.

## Notes / honest caveats

- The CA **private key** (`ca.key`) stays root-only on the box and is gitignored — the public `ca.crt`
  alone can't be used to impersonate the server.
- The one trust-on-first-use moment is the initial `/ca.crt` fetch; the fingerprint check closes it.
- This is LAN/HTTPS by IP. If you ever expose the box to the internet, use a **real domain + Let's
  Encrypt** instead (and reconsider exposure — see [DEPLOY.md](../DEPLOY.md)).
