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

# 2) Stop the orchestrator so the vector re-embed can run without races
sudo systemctl stop jarvis-orchestrator

# 3) Restart the LLM with the larger context window (-c 4096)
sudo systemctl restart llama-fast

# 4) One-time memory migration: rebuild the vector store into the cosine collection
uv run python src/scripts/reembed_memory.py

# 5) Start the orchestrator (init_db drops the legacy FTS tables, starts the workers)
sudo systemctl start jarvis-orchestrator

# 6) Verify
curl -s http://localhost:5000/health
sudo systemctl status jarvis-orchestrator --no-pager | head -20
journalctl -u jarvis-orchestrator -n 40 --no-pager
```

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

## Rotate the master key (recommended)
The previous key was committed in plaintext during development. Generate a new one and
update the voice listener's source (it reads from `config/jarvis.json` automatically):

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"   # paste into config api_key
sudo systemctl restart jarvis-orchestrator
```
