#!/bin/bash
# Jarvis voice listener: continuous transcription (whisper-stream) → wake-word bridge → POST /inbox.
#
# The bridge (voice_bridge.py) reads whisper-stream's transcript, gates on the wake word, and
# POSTs the command as JSON via urllib — NO shell, so transcribed audio can never be executed as
# a command. (This replaces the old whisper-command `-cmd "curl … %s"` line, which was both
# unsafe-by-design and non-functional: `-cmd` is a *commands file*, not a shell template.)
#
# Needs a voice-listener API key (a normal per-user key — /inbox isn't device-scoped):
#   uv run python src/scripts/manage.py mint-key admin voice-listener > config/voice_listener.key
#   chmod 600 config/voice_listener.key
# Tune the wake word / whisper flags via env vars — see voice_bridge.py.
cd /srv/jarvis || exit 1
exec uv run python src/scripts/voice_bridge.py "$@"
