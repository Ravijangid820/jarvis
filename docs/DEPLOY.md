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

## Network exposure: Tailscale + localhost only

`uvicorn` binds `0.0.0.0` (so the local voice listener on loopback and remote devices on
the Tailscale interface both work), and a host firewall limits `tcp:5000` to the `lo` and
`tailscale0` interfaces — dropping plaintext access from the rest of the LAN.

Example with `nftables` (adjust the Tailscale interface name if different):

```bash
sudo nft add table inet jarvis
sudo nft add chain inet jarvis input '{ type filter hook input priority 0; }'
# allow loopback + tailscale to port 5000, drop everyone else
sudo nft add rule inet jarvis input iifname "lo" tcp dport 5000 accept
sudo nft add rule inet jarvis input iifname "tailscale0" tcp dport 5000 accept
sudo nft add rule inet jarvis input tcp dport 5000 drop
```

(Equivalent `ufw`: `ufw allow in on tailscale0 to any port 5000` + `ufw deny 5000`.)

The LLM server (`llama-fast`) already binds `127.0.0.1` only and is never network-exposed.

## Adding TLS (HTTPS)

The firewall above keeps the API off the open LAN, but traffic is still **plaintext HTTP**
(bearer tokens in the clear) over `lo` + `tailscale0`. Put a TLS terminator in front and
bind the orchestrator to **loopback only** so nothing speaks plaintext on a network interface.

First, in `config/jarvis.json` set the orchestrator to loopback (the reverse proxy reaches it
locally; the firewall rules above are then optional):

```jsonc
"orchestrator": { "host": "127.0.0.1", "port": 5000 }
```

### Option A — Tailscale Serve (recommended; you already run Tailscale)

Tailscale provisions a valid Let's Encrypt cert for your tailnet name and proxies HTTPS to the
local app — no domain, no certbot, no extra daemon:

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:5000
sudo tailscale serve status          # shows https://<machine>.<tailnet>.ts.net → 127.0.0.1:5000
```

Now reach Jarvis at `https://<machine>.<tailnet>.ts.net` from any tailnet device. Remove the
`--https` mapping with `sudo tailscale serve --https=443 off`.

### Option B — Caddy (a real domain, automatic certs)

```caddy
# /etc/caddy/Caddyfile
jarvis.example.com {
    reverse_proxy 127.0.0.1:5000
}
```

`sudo systemctl reload caddy` — Caddy fetches and renews the cert automatically. (nginx works
too; point a `server { listen 443 ssl; location / { proxy_pass http://127.0.0.1:5000; } }` at
your cert.)

### Note on the login rate limiter

`/auth/login` is throttled per **connecting IP**. Behind a proxy that IP becomes the proxy's
(`127.0.0.1`), so the limit applies globally (8 logins/min total) rather than per client — more
restrictive, not less, so it's safe to leave. If you want per-client limiting through a proxy,
have the proxy set `X-Forwarded-For` and we can teach the limiter to read it (only when behind a
trusted proxy — never trust that header on a directly-exposed bind).

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
