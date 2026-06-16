#!/bin/bash
# Bridge Script: Connects Whisper Command tool to our FastAPI Orchestrator
# Updated: Uses API key authentication

cd /srv/jarvis/whisper

# Load the voice-listener API key (a real, revocable api_keys row — mint with
# `uv run python src/scripts/manage.py mint-key <user> voice-listener`).
KEY_FILE=/srv/jarvis/config/voice_listener.key
if [ ! -r "$KEY_FILE" ]; then
  echo "FATAL: $KEY_FILE missing. Mint one: uv run python src/scripts/manage.py mint-key admin voice-listener > $KEY_FILE" >&2
  exit 1
fi
API_KEY=$(tr -d '[:space:]' < "$KEY_FILE")

echo "Starting Jarvis Voice Listener..."
echo "Waiting for wake word: 'Jarvis'"

# The whisper-command tool listens continuously.
# When it hears the wake word, it records the following sentence.
# We map the output command to a curl request to our locally hosted Python API.

./build/bin/whisper-command \
  -m ./models/ggml-base.en.bin \
  -t 2 \
  -c 0 \
  -vth 0.6 \
  -prompt "Jarvis" \
  -cmd "curl -X POST http://localhost:5000/inbox -H 'Content-Type: application/json' -H 'Authorization: Bearer ${API_KEY}' -d '{\"text\": \"%s\"}'"
