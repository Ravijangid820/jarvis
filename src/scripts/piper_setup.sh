#!/bin/bash
# Script to install Piper TTS and the default British Male voice.
# Path derives from the repo location, so it works in any checkout/container.
set -e

PIPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/piper"
mkdir -p "$PIPER_DIR"
cd "$PIPER_DIR"

# Supply-chain: a native binary + an ONNX model (both executed/deserialized by the TTS pipeline) are
# fetched here, so both are PINNED + SHA-256-verified by default (tamper-evident). Overrides:
#   PIPER_VERSION=<tag|latest>   release tag (default below is rhasspy/piper's final release)
#   PIPER_SHA256=<hash>          binary tarball hash (defaulted only for the default version)
#   VOICE_SHA256=<hash>          .onnx voice hash (the voice URL is a mutable HF ref — this pins it)
PIPER_DEFAULT_VERSION="2023.11.14-2"
PIPER_VERSION="${PIPER_VERSION:-$PIPER_DEFAULT_VERSION}"
if [ "$PIPER_VERSION" = "$PIPER_DEFAULT_VERSION" ]; then
  PIPER_SHA256="${PIPER_SHA256:-a50cb45f355b7af1f6d758c1b360717877ba0a398cc8cbe6d2a7a3a26e225992}"
fi
VOICE_SHA256="${VOICE_SHA256:-0a309668932205e762801f1efc2736cd4b0120329622adf62be09e56339d3330}"
[ "$PIPER_VERSION" = "latest" ] && echo "  ! PIPER_VERSION=latest — mutable, unverified (pin a tag for a tamper-evident install)." >&2

# Resilient download: resume (-C -) + retry up to 5×, so a flaky/slow network doesn't abort the
# install (mirrors the Docker model fetch). Version-agnostic — works on any curl.
DL() {
  dest="$1"; url="$2"; n=0
  until curl -fL -C - -o "$dest" "$url"; do
    n=$((n + 1)); [ "$n" -ge 5 ] && { echo "  download failed after 5 attempts: $url" >&2; return 1; }
    echo "  download dropped, retry $n/5 in 5s…" >&2; sleep 5
  done
}
if [ "$PIPER_VERSION" = "latest" ]; then
  PIPER_URL="https://github.com/rhasspy/piper/releases/latest/download/piper_linux_x86_64.tar.gz"
else
  PIPER_URL="https://github.com/rhasspy/piper/releases/download/${PIPER_VERSION}/piper_linux_x86_64.tar.gz"
fi

echo "Downloading Piper binary for Linux x86_64..."
DL piper.tar.gz "$PIPER_URL"
if [ -n "${PIPER_SHA256:-}" ]; then
  echo "${PIPER_SHA256}  piper.tar.gz" | sha256sum -c - || { echo "Piper checksum MISMATCH"; rm -f piper.tar.gz; exit 1; }
fi

echo "Extracting Piper..."
tar -xzf piper.tar.gz --strip-components=1
rm -f piper.tar.gz

echo "Downloading Alan (British Male) Medium Quality Voice..."
mkdir -p voices
DL voices/en_GB-alan-medium.onnx "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/en_GB-alan-medium.onnx"
DL voices/en_GB-alan-medium.onnx.json "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/en_GB-alan-medium.onnx.json"
if [ -n "${VOICE_SHA256:-}" ]; then
  echo "${VOICE_SHA256}  voices/en_GB-alan-medium.onnx" | sha256sum -c - || { echo "voice checksum MISMATCH"; rm -f voices/en_GB-alan-medium.onnx; exit 1; }
fi

echo "Setup complete! Piper is ready at $PIPER_DIR/piper"
