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
| `VOICE_CAPTURE_ID` | from `config/active_mic.json` | capture device index; `-1` = system default. Overrides the UI choice |

## Choosing which microphone

There are two separate choices, because there are two separate things listening.

### “Microphone” — where *your* voice is picked up (any user)

**⊕ → Microphone**, or `/mic` in the command palette. One list, both kinds of hardware:

| Source | What it is |
|---|---|
| **This device** | inputs the browser can see — the laptop array, a headset, a USB mic plugged in where you're sitting |
| **On the Jarvis server** | microphones attached to the box, streamed to your tab as audio |

Pick whichever is physically closest to you. If the good mic is plugged into the server and you're
sitting next to the server, that beats the laptop lid; if you've carried the mic to your desk, the
opposite. Both push-to-talk voice typing and the live voice page use the choice.

**Transcription always stays in your tab.** Choosing the server's microphone moves where sound is
*captured*, not where it is *understood* — the server sends raw PCM and your browser runs Whisper on
it, so the box's CPU is not doing STT either way. Constraints worth knowing:

- **One session at a time.** It is one piece of hardware; a second session gets `409 busy`.
- **The wake-word listener holds it** if that service is running — stop it to use the mic from a browser.
- **No echo cancellation.** The browser can't run AEC on another machine's audio, so the half-duplex
  gate (Jarvis is deaf while speaking) is the only thing preventing a reply-loop. Headphones help.
- **It's a live feed of the room the server is in.** Any household member can open it, each session
  is written to the audit log, and it is capped at 15 minutes and refused outright in demo households.

### “Wake-word listener mic” — the always-on listener (admin)

**⊕ → Wake-word listener mic**, or `/listener-mic`. Sets the device `whisper-stream` opens for the
always-on wake word. The list comes from the server, and this is the one that needs a restart.

The listener holds its capture stream open, so a change applies on restart:

```bash
sudo systemctl restart jarvis-listener      # or re-run run_listener.sh
```

To check what the server can see without opening the UI:

```bash
uv run python src/scripts/list_mics.py
# {"driver": "alsa", "devices": [{"id": 0, "name": "Boya BY-M1"}], "error": null}
```

**In a container, the host's sound card is not visible by default.** An empty list on a box with a
mic plugged in almost always means `/dev/snd` hasn't been passed through to the LXC. For an
unprivileged Proxmox container, add to `/etc/pve/lxc/<id>.conf`:

```
lxc.cgroup2.devices.allow: c 116:* rwm
lxc.mount.entry: /dev/snd dev/snd none bind,optional,create=dir
```

then restart the container and re-open the panel. Add the container user to `audio` if ALSA reports
permission errors.

> **Why the choice is stored by name, not just by number.** whisper-stream selects a mic with
> `-c <index>`, and those indices are positional — unplug a device and everything after it shifts
> down one, so yesterday's index 1 can be today's different microphone. The selection records the
> device *name* too and re-resolves it at startup; if the named mic isn't attached, the listener
> falls back to the system default and logs that, rather than opening whatever inherited the number.

## Wake phrases (browser)

The live voice page answers to more than one phrase. Two detectors run side by side while armed:

| Detector | Phrases | Speed | Cost |
|---|---|---|---|
| **openWakeWord** (trained model) | `hey jarvis` only | instant — no pause, no transcription | ~3 tiny ONNX models per 80 ms |
| **Phrase list** (transcript match) | anything you type | after a ~0.4 s pause | one short Whisper run per brief utterance |

Defaults: `hey jarvis`, `ok jarvis`, `okay jarvis`, `jarvis`, `wake up jarvis`, `jarvis wake up`,
`jarvis are you there`, `you there jarvis`. Edit them in **⚙ → Wake phrases** on the voice page.

Say the command in the same breath and it is answered directly — *"Jarvis, what's the weather"*
wakes it and asks in one go, rather than making you wait for an acknowledgement first.

**Why the list can't just be fed to the trained model.** openWakeWord's v0.5.1 release contains
exactly six pre-trained models — `alexa`, `hey_jarvis`, `hey_mycroft`, `hey_rhasspy`, `timer`,
`weather`. Each is a classifier for one phrase. Adding `jarvis are you there` as an *instant* phrase
would mean training a new model (synthetic TTS data, augmentation, roughly an hour on a GPU); the
runtime cost would be negligible, since the mel and embedding stages are shared and only the small
final head is per-phrase. The transcript path gets you the same phrases today without any of that.

**What this means for the room.** While armed, short utterances are transcribed *in the tab* so they
can be matched. Audio never leaves the browser, nothing is stored, and anything that does not name
Jarvis is discarded — but it is more work than a pure keyword spotter, and anything longer than
~3.5 s is dropped without being transcribed at all.

## Voice volume control

Spoken volume commands work out of the box once the **Windows volume agent** is running (see
[volume-agent.md](volume-agent.md)) — say e.g. *"Jarvis, set volume to 50%"*, *"Jarvis, volume up"*,
*"Jarvis, mute"*. The server recognizes these and enqueues the command to the agent (no LLM round-trip),
replying with a short confirmation. Requirements:

- The listener's key must belong to a user allowed to control devices (admin, or a user with
  `can_control_devices`) — the default `mint-key admin voice-listener` qualifies.
- Commands target the device id **`laptop`** (the volume agent's default `device`).

### Hands-free (gesture) volume

Say **"Jarvis, volume"** to enter gesture mode, then **raise/lower your hand** to adjust the volume;
**make a fist** (or stop) to end it. This needs the **camera agent running** with `mediapipe`
installed (see [camera.md](camera.md)); by default it engages camera `laptop-cam`. The camera only
tracks your hand during this short, voice-authorized window — authorization stays on the server.

## Notes

- Spoken replies need an audio output device + a player (`paplay`/`aplay`/`ffplay`) on the box —
  on-hardware tuning required.
- Security: the bridge POSTs via urllib with **no shell**; the key is read from a 0600 file. See the
  voice flow in [../WORKFLOWS.md](../WORKFLOWS.md).
