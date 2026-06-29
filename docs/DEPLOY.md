# Deploying Jarvis changes

The repo at `/srv/jarvis` is the source of truth. The systemd units are copied to
`/etc/systemd/system/`. Runtime data (DB, vectors, logs, config with the master key)
lives on the box and is gitignored.

## One-time config additions
Add to `/srv/jarvis/config/jarvis.json` (see `jarvis.example.json` for the full shape):

```jsonc
"orchestrator": {
  "host": "0.0.0.0",            // loopback + Tailscale both need to reach it; firewall restricts LAN
  "allowed_origins": []          // [] = no cross-origin; the SPA/admin are same-origin
}
```

## Coordinated deploy (run on the box)

```bash
cd /srv/jarvis

# 1) Install the updated units
sudo cp systemd/llama-fast.service systemd/jarvis-orchestrator.service /etc/systemd/system/
sudo systemctl daemon-reload

# 2) Build the frontend — REQUIRED whenever anything under frontend/ changed.
#    The server serves frontend/dist (gitignored, not built automatically); without
#    this, GET / returns 404 and you keep serving the old bundle.
cd frontend && npm ci && npm run build && cd ..

# 3) Stop the orchestrator so the vector re-embed can run without races
sudo systemctl stop jarvis-orchestrator

# 4) Restart the LLM with the larger context window (-c 4096)
sudo systemctl restart llama-fast

# 5) One-time memory migration: rebuild the vector store into the cosine collection
uv run python src/scripts/reembed_memory.py

# 6) Start the orchestrator (init_db drops the legacy FTS tables, starts the workers)
sudo systemctl start jarvis-orchestrator

# 7) Verify
curl -s http://localhost:5000/health
sudo systemctl status jarvis-orchestrator --no-pager | head -20
journalctl -u jarvis-orchestrator -n 40 --no-pager
```

Steps 4–5 (LLM restart, vector re-embed) are one-time migrations. For a **routine update**
the cycle is: pull, rebuild the frontend if `frontend/` changed (step 2), then
`sudo systemctl restart jarvis-orchestrator`. A backend-only change needs just the restart;
a frontend-only change needs just the rebuild (the bundle is served fresh, no restart needed).

## Network exposure (this deployment: Proxmox host + Tailscale subnet router)

The actual topology here is three tiers, and Tailscale does **not** run in the orchestrator
container:

```
tailnet device ──WireGuard (encrypted)──► subnet-router LXC ──HTTPS (local CA) on 192.168.1.0/24──► app LXC :5000
   (phone/laptop)                          (runs tailscaled,                 (192.168.1.20,
                                            advertises 192.168.1.0/24)        uvicorn :5000, TLS)
```

- The Proxmox host and a dedicated **subnet-router LXC** run Tailscale; the router advertises
  `192.168.1.0/24` so the other VMs/containers reach the tailnet **without** installing Tailscale.
- **Remote access is encrypted** by WireGuard from the device up to the subnet router, and the
  **router → app hop is now HTTPS** too (local CA, see below) — so there's no plaintext segment.

The orchestrator binds `0.0.0.0:5000`, so without a firewall *any* host on `192.168.1.0/24`
could hit it in plaintext. Restrict `:5000` to loopback (the local voice listener) + the subnet
router. Persisted in `/etc/nftables.conf` on the **app container** (run as root):

```nft
#!/usr/sbin/nft -f
flush ruleset
table inet filter {
    chain input {
        type filter hook input priority filter;
        # Jarvis :5000 — only loopback + the Tailscale subnet router (192.168.1.2).
        tcp dport 5000 iif lo accept
        tcp dport 5000 ip saddr 192.168.1.2 accept
        tcp dport 5000 drop
    }
    chain forward { type filter hook forward priority filter; }
    chain output  { type filter hook output priority filter; }
}
```

```bash
nft -f /etc/nftables.conf        # apply
systemctl enable nftables        # load at boot
```

> NOTE: this assumes the subnet router **SNATs** routed traffic to its own LAN IP (the Tailscale
> default), so packets arrive `from 192.168.1.2`. If you set `--snat-subnet-routes=false` on the
> router, allow the tailnet CGNAT range instead: `tcp dport 5000 ip saddr 100.64.0.0/10 accept`.

The LLM server (`llama-fast`) already binds `127.0.0.1` only and is never network-exposed.

## TLS (HTTPS) — terminated in the app container via a local CA

TLS now runs **directly on the orchestrator** (uvicorn `--ssl-*`), so the entire path — including
the router→app hop on the Proxmox bridge — is encrypted, and bearer tokens / events / enroll-preview
frames are never plaintext. Because the app container has no tailnet identity (it's behind the subnet
router), it can't get a `tailscale cert`, so we use a **per-deployment local CA**:

```bash
bash src/scripts/setup_tls.sh                  # generate the CA + server cert (prints CA fingerprint)
sudo mkdir -p /etc/systemd/system/jarvis-orchestrator.service.d
sudo cp systemd/jarvis-orchestrator.service.d/tls.conf /etc/systemd/system/jarvis-orchestrator.service.d/
sudo systemctl daemon-reload && sudo systemctl restart jarvis-orchestrator
curl --cacert tls/ca.crt https://127.0.0.1:5000/health      # verify
```

The server publishes its public CA at `GET /ca.crt`; copy `tls/ca.crt` to each device/browser to
trust it (camera agents: put it at `camera/config/ca.crt`). Full walkthrough incl. browser +
Android/iOS: **[setup/tls.md](setup/tls.md)**.
Reversible: remove the drop-in → `daemon-reload` → restart (back to HTTP). Keep the `:5000` firewall
above as defense-in-depth.

**Alternatives** (if you ever expose beyond the LAN): terminate TLS on the subnet router with
`tailscale serve --bg --https=443 http://192.168.1.20:5000` (uses `*.ts.net` certs), or Caddy with a
real domain — `jarvis.example.com { reverse_proxy 192.168.1.20:5000 }`.

### Note on the login rate limiter

`/auth/login` is throttled per **username** (8 attempts/min/account). This targets the actual
brute-force surface and — unlike IP keying — can't cause a global login lockout behind the shared
subnet-router source IP. The tradeoff is that an attacker can briefly throttle one specific
account; acceptable for this single-operator deployment.

## Installing the services (root or a dedicated non-root user)

`src/scripts/install_services.sh` installs **both** units (orchestrator + `llama-fast`) and works
from any checkout path. It auto-detects the repo, `uv`, the `llama-server` binary and the GGUF, and
generates the units for the mode you choose:

```bash
sudo bash src/scripts/install_services.sh                      # run as ROOT (simplest)
sudo JARVIS_USER=jarvis bash src/scripts/install_services.sh   # dedicated NON-ROOT user (hardened — recommended)
```

Non-root mode (`JARVIS_USER`) additionally: creates the system user; copies `uv` to
`/usr/local/bin` and the HuggingFace cache under the repo's `.cache/`; makes only the **writable
data dirs** (`memory`/`logs`/`.cache`/`.venv`/`config`) owned by the user while source + `.git`
stay root-owned (read-only to the service); relocates a `/root` llama build to `/opt`; and runs
`llama-server` non-root too. `ProtectSystem=strict` in both units. Preview with
`DRY_RUN=1 …` (writes to `systemd/generated/`, no root needed). The stricter
`SystemCallFilter`/`MemoryDenyWriteExecute` directives are intentionally omitted — add and test
them per box if you want (native libs can trip a syscall filter).

> After a non-root install the repo is root-owned and the service writes only its data dirs. If you
> run `git` **as root** on the box and see "dubious ownership", add the exception once:
> `git config --global --add safe.directory <repo>`.

## Auth model & the admin CLI

There is **no master API key**. Authentication is either a web-login session token or a
per-user API key (the `api_keys` table). The voice listener uses a real, revocable API key
read from `config/voice_listener.key` (gitignored).

`src/scripts/manage.py` is the local recovery/admin tool (talks straight to the DB):

```bash
uv run python src/scripts/manage.py list-users
uv run python src/scripts/manage.py create-admin <user> <password>   # bootstrap / lockout recovery
uv run python src/scripts/manage.py reset-password <user> <password>
uv run python src/scripts/manage.py mint-key <user> voice-listener   # prints a new key

# (Re)provision the voice listener's key:
uv run python src/scripts/manage.py mint-key admin voice-listener > config/voice_listener.key
chmod 600 config/voice_listener.key
```

To revoke a key, delete its row from `api_keys` (admin panel, or the API).
