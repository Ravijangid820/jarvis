#!/bin/bash
# Script to install Piper TTS and the default British Male voice.
# Path derives from the repo location, so it works in any checkout/container.
set -e

PIPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/piper"
mkdir -p "$PIPER_DIR"
cd "$PIPER_DIR"

# Supply-chain note: a native binary + an ONNX model (both executed/deserialized by the TTS
# pipeline) are fetched here. Pin a release and verify checksums for a tamper-evident install:
#   PIPER_VERSION=2023.11.14-2   (default 'latest' is mutable — pinning is recommended)
#   PIPER_SHA256=<hash>          (optional; verifies the binary tarball)
#   VOICE_SHA256=<hash>          (optional; verifies the .onnx voice)
PIPER_VERSION="${PIPER_VERSION:-latest}"
[ "$PIPER_VERSION" = "latest" ] && echo "  ! PIPER_VERSION unset — using mutable 'latest' (set it to pin)." >&2
if [ "$PIPER_VERSION" = "latest" ]; then
  PIPER_URL="https://github.com/rhasspy/piper/releases/latest/download/piper_linux_x86_64.tar.gz"
else
  PIPER_URL="https://github.com/rhasspy/piper/releases/download/${PIPER_VERSION}/piper_linux_x86_64.tar.gz"
fi

echo "Downloading Piper binary for Linux x86_64..."
wget -qO piper.tar.gz "$PIPER_URL"
if [ -n "${PIPER_SHA256:-}" ]; then
  echo "${PIPER_SHA256}  piper.tar.gz" | sha256sum -c - || { echo "Piper checksum MISMATCH"; rm -f piper.tar.gz; exit 1; }
fi

echo "Extracting Piper..."
tar -xzf piper.tar.gz --strip-components=1
rm -f piper.tar.gz

echo "Downloading Alan (British Male) Medium Quality Voice..."
mkdir -p voices
wget -qO voices/en_GB-alan-medium.onnx "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/en_GB-alan-medium.onnx"
wget -qO voices/en_GB-alan-medium.onnx.json "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/en_GB-alan-medium.onnx.json"
if [ -n "${VOICE_SHA256:-}" ]; then
  echo "${VOICE_SHA256}  voices/en_GB-alan-medium.onnx" | sha256sum -c - || { echo "voice checksum MISMATCH"; rm -f voices/en_GB-alan-medium.onnx; exit 1; }
fi

echo "Setup complete! Piper is ready at $PIPER_DIR/piper"
