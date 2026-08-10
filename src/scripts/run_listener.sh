#!/bin/bash
# Jarvis voice listener: continuous transcription (whisper-stream) → wake-word bridge → POST /inbox.
#
# The bridge (voice_bridge.py) reads whisper-stream's transcript, gates on the wake word, and
# POSTs the command as JSON via urllib — NO shell, so transcribed audio can never be executed as
# a command. (This replaces the old whisper-command `-cmd "curl … %s"` line, which was both
# unsafe-by-design and non-functional: `-cmd` is a *commands file*, not a shell template.)
#
# Needs a voice-listener API key. Mint it DEVICE-SCOPED (third arg), never as a plain admin key:
#   uv run python src/scripts/manage.py mint-key <you> voice-listener voice-lab > config/voice_listener.key
#   chmod 600 config/voice_listener.key
#
# Why the device_id matters: an always-on microphone in a room is a principal anyone within earshot
# can drive. A device-scoped key authenticates everywhere a user key does (so /inbox, memory and
# device control still work) but the middleware strips admin from it unconditionally — see the
# `is_admin = (role == "admin") and not device_id` guard in main.py. So a mis-transcription or a
# stolen key can't reach /admin/*. It is also independently revocable and shows up in the audit log
# as the mic rather than as you. See docs/planning/voice-wake.md §3.
# Tune the wake word / whisper flags via env vars — see voice_bridge.py.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)" || exit 1   # repo root (any checkout)
exec uv run python src/scripts/voice_bridge.py "$@"
