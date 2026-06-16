#!/bin/bash
# Script to install Piper TTS and the default British Male voice.
# Path derives from the repo location, so it works in any checkout/container.
set -e

PIPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/piper"
mkdir -p "$PIPER_DIR"
cd "$PIPER_DIR"

echo "Downloading Piper binary for Linux x86_64..."
wget -qO piper.tar.gz "https://github.com/rhasspy/piper/releases/latest/download/piper_linux_x86_64.tar.gz"

echo "Extracting Piper..."
tar -xzf piper.tar.gz --strip-components=1
rm -f piper.tar.gz

echo "Downloading Alan (British Male) Medium Quality Voice..."
mkdir -p voices
wget -qO voices/en_GB-alan-medium.onnx "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/en_GB-alan-medium.onnx"
wget -qO voices/en_GB-alan-medium.onnx.json "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/en_GB-alan-medium.onnx.json"

echo "Setup complete! Piper is ready at $PIPER_DIR/piper"
