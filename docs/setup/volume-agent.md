# Jarvis volume agent (Windows)

Controls the laptop's system/Bluetooth volume on command from Jarvis. **Outbound-only**: it
polls the orchestrator for commands and applies them locally via the Windows Core Audio API
(`pycaw`). It opens no listening port, runs no shell, and understands only a 4-word command
vocabulary — so the worst case if anything went wrong is "the volume changes."

> **Code:** [`clients/volume-agent/`](../../clients/volume-agent/) in the repo. Run the commands below **on the Windows laptop, from that directory.**

> Your BT speaker is just the laptop's default output device, so setting the master volume
> controls it. No Bluetooth-specific code needed.

## Security properties
- **No inbound socket** → cannot be a network entry point; no firewall hole needed (keep inbound blocked).
- **No shell-out** → no command injection; volume is set via `pycaw` (in-process API).
- **Validated commands only**: `set 0–100`, `step ±n`, `mute`, `unmute`. Anything else is ignored.
- **Authenticated** with its own revocable machine API key; commands are authorized server-side
  (by the caller's identity/permissions) *before* they're queued.
- **Least privilege**: run as your normal user — **no admin**.
- **Encryption**: point `server.url` at the **HTTPS/Tailscale** address for best security. On a
  fully-trusted LAN, plain HTTP is your accepted risk (commands are harmless + authenticated).

## Setup (on the Windows laptop)
```powershell
# 1. Python 3 installed, then:
pip install -r requirements.txt

# 2. On the SERVER, mint a key for this device:
#      uv run python src/scripts/manage.py mint-key <user> laptop-volume
#    and save the printed key into this folder as  agent.key

# 3. Configure and run:
copy config.example.json config.json   # edit server.url if needed
python volume_agent.py
```
Auto-start at logon: add it as a **Task Scheduler** task (trigger "At log on", action = `python ...\volume_agent.py`), running as your user.

## Triggering volume changes
Anything authorized can enqueue a command (the agent applies it within ~1 poll):
```bash
curl -X POST <server>/devices/volume -H "Authorization: Bearer <your token/key>" \
  -H "Content-Type: application/json" -d '{"action":"set","value":40}'
#  {"action":"step","value":-10}   {"action":"mute"}   {"action":"unmute"}
```
From the Jarvis browser UI you're already logged in, so your account's permissions apply
(admins, or users with `can_control_devices`). Later this becomes the LLM's `set_volume` tool,
and the in-room voice path uses the camera-identified person for authorization.
