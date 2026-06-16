#!/bin/bash
# Bridge Script: Connects Whisper Command tool to our FastAPI Orchestrator
# Updated: Uses API key authentication

cd /srv/ai/whisper

# Load API key from config
API_KEY=$(python3 -c "import json; print(json.load(open('/srv/ai/config/jarvis.json'))['api_key'])")

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
