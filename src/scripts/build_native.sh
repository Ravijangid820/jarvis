#!/usr/bin/env bash
# Build the native C++ inference engines into the repo: whisper.cpp (STT) and
# llama.cpp (LLM server). Target here is Sandy Bridge — AVX but NO AVX2; adjust the
# -DGGML_* flags for your CPU (e.g. drop the AVX overrides on a modern machine).
#
#   bash src/scripts/build_native.sh
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cyan() { printf '\033[1;36m▸ %s\033[0m\n' "$1"; }

for tool in git cmake; do
  command -v "$tool" >/dev/null || { echo "missing prerequisite: $tool"; exit 1; }
done

# whisper.cpp — pinned to the version the project was built against.
cyan "whisper.cpp (v1.8.6)"
[ -d "$REPO/whisper/.git" ] || git clone --branch v1.8.6 --depth 1 https://github.com/ggerganov/whisper.cpp "$REPO/whisper"
cmake -S "$REPO/whisper" -B "$REPO/whisper/build" -DGGML_AVX=ON -DGGML_AVX2=OFF -DGGML_NATIVE=OFF -DWHISPER_SDL2=ON
cmake --build "$REPO/whisper/build" -j

# llama.cpp — AVX-only build of the server.
cyan "llama.cpp (llama-server)"
[ -d "$REPO/llama.cpp/.git" ] || git clone --depth 1 https://github.com/ggml-org/llama.cpp "$REPO/llama.cpp"
cmake -S "$REPO/llama.cpp" -B "$REPO/llama.cpp/build" -DGGML_AVX=ON -DGGML_AVX2=OFF -DGGML_NATIVE=OFF
cmake --build "$REPO/llama.cpp/build" -j --target llama-server

cyan "Done."
echo "  llama-server: $REPO/llama.cpp/build/bin/llama-server"
echo "  whisper:      $REPO/whisper/build/bin/"
echo "Point systemd/llama-fast.service (ExecStart + the GGUF -m path) at these locations."
