# Voice listener setup (wake word → `/inbox`)

An optional on-box listener: **whisper-stream** transcribes the mic continuously, a small bridge
(`voice_bridge.py`) gates on the wake word ("Jarvis"), and POSTs the command to `/inbox` as JSON —
**no shell**, so transcribed audio can never be executed. Replies can be spoken back via Piper.

> Runs **on the server box** (it needs the mic + speakers + the whisper binary/model from
> `build_native.sh` / `download_models.sh`). Talks to the orchestrator over loopback.

## 1. Mint a key

`/inbox` is a normal user endpoint (not device-scoped), so use a per-user key:

```bash
uv run python src/scripts/manage.py mint-key admin voice-listener > config/voice_listener.key
chmod 600 config/voice_listener.key
```

## 2. Run it

```bash
# HTTPS is on, so point at https and let Python trust the local CA via SSL_CERT_FILE:
SSL_CERT_FILE=/srv/jarvis/tls/ca.crt \
JARVIS_SERVER_URL=https://localhost:5000 \
bash src/scripts/run_listener.sh
```

(If you haven't enabled [TLS](tls.md) yet, drop `SSL_CERT_FILE` and use `http://localhost:5000`.)

## Tuning (env vars, see `voice_bridge.py`)

| Var | Default | Purpose |
|---|---|---|
| `JARVIS_SERVER_URL` | `http://localhost:5000` | orchestrator URL (use `https://…` with TLS) |
| `SSL_CERT_FILE` | — | path to `tls/ca.crt` so Python verifies the local-CA HTTPS |
| `VOICE_WAKE_WORD` | `jarvis` | wake word that gates a command |
| `VOICE_KEY_FILE` | `config/voice_listener.key` | the API key file |
| `WHISPER_BIN` / `WHISPER_MODEL` | `whisper/build/bin/whisper-stream` · `ggml-base.en.bin` | transcription binary + model |

## Voice volume control

Spoken volume commands work out of the box once the **Windows volume agent** is running (see
[volume-agent.md](volume-agent.md)) — say e.g. *"Jarvis, set volume to 50%"*, *"Jarvis, volume up"*,
*"Jarvis, mute"*. The server recognizes these and enqueues the command to the agent (no LLM round-trip),
replying with a short confirmation. Requirements:

- The listener's key must belong to a user allowed to control devices (admin, or a user with
  `can_control_devices`) — the default `mint-key admin voice-listener` qualifies.
- Commands target the device id **`laptop`** (the volume agent's default `device`).

## Notes

- Spoken replies need an audio output device + a player (`paplay`/`aplay`/`ffplay`) on the box —
  on-hardware tuning required.
- Security: the bridge POSTs via urllib with **no shell**; the key is read from a 0600 file. See the
  voice flow in [../WORKFLOWS.md](../WORKFLOWS.md).
