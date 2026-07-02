#!/usr/bin/env bash
# Build the native C++ inference engines into the repo:
#   • llama.cpp   — the LLM server (REQUIRED). Built FIRST so nothing else can block it.
#   • whisper.cpp — STT for the voice listener (OPTIONAL). Built last; its failure never blocks the LLM.
# Target here is Sandy Bridge — AVX but NO AVX2; adjust the -DGGML_* flags for your CPU (e.g. drop the
# AVX overrides on a modern machine).
#
#   bash src/scripts/build_native.sh                    # build both
#   SKIP_WHISPER=1 bash src/scripts/build_native.sh     # LLM only (no voice → no SDL2 needed)
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cyan() { printf '\033[1;36m▸ %s\033[0m\n' "$1"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$1"; }

for tool in git cmake; do
  command -v "$tool" >/dev/null || { echo "missing prerequisite: $tool (install build-essential + cmake)"; exit 1; }
done

# Cap parallel compile jobs by RAM. cc1plus on llama.cpp's large model translation units can use ~2 GB
# each, so an unbounded `-j` OOM-kills the compiler ("cc1plus: Killed"). Scale to the RAM actually
# AVAILABLE right now (a box may already be using memory — editor server, other services) — MemAvailable,
# falling back to MemTotal on old kernels. Budget ~2 GB per job, keep ~1 GB in reserve; ≥1, ≤ CPU count.
# Override with BUILD_JOBS=<n>.
if [ -z "${BUILD_JOBS:-}" ]; then
  _mem_kb=$(awk '/^MemAvailable:/{print $2; exit}' /proc/meminfo 2>/dev/null)
  [ -n "${_mem_kb:-}" ] || _mem_kb=$(awk '/^MemTotal:/{print $2; exit}' /proc/meminfo 2>/dev/null)
  _mem_gb=$(( ${_mem_kb:-4194304} / 1024 / 1024 ))
  _cpus=$(nproc 2>/dev/null || echo 2)
  BUILD_JOBS=$(( (_mem_gb - 1) / 2 )); [ "$BUILD_JOBS" -lt 1 ] && BUILD_JOBS=1
  [ "$BUILD_JOBS" -gt "$_cpus" ] && BUILD_JOBS="$_cpus"
fi
cyan "compile jobs: -j ${BUILD_JOBS}  (from available RAM — override with BUILD_JOBS=<n>, e.g. BUILD_JOBS=1)"

# --- llama.cpp (REQUIRED) — build the LLM server first; a failure here is fatal (as it should be) ---
cyan "llama.cpp (llama-server)"
LLAMA_CPP_REF="${LLAMA_CPP_REF:-}"
if [ ! -d "$REPO/llama.cpp/.git" ]; then
  if [ -n "$LLAMA_CPP_REF" ]; then
    git clone --branch "$LLAMA_CPP_REF" --depth 1 https://github.com/ggml-org/llama.cpp "$REPO/llama.cpp"
  else
    echo "  ! LLAMA_CPP_REF unset — cloning upstream HEAD (not pinned). Set LLAMA_CPP_REF=<tag> to pin." >&2
    git clone --depth 1 https://github.com/ggml-org/llama.cpp "$REPO/llama.cpp"
  fi
fi
# Containers/Codespaces often check the tree out under a different uid → git's "dubious ownership"
# guard. Mark these build dirs safe so rev-parse (and any git op) works regardless of owner.
git config --global --add safe.directory "$REPO/llama.cpp" 2>/dev/null || true
echo "  llama.cpp at commit: $(git -C "$REPO/llama.cpp" rev-parse HEAD 2>/dev/null || echo unknown)"
cmake -S "$REPO/llama.cpp" -B "$REPO/llama.cpp/build" -DGGML_AVX=ON -DGGML_AVX2=OFF -DGGML_NATIVE=OFF
cmake --build "$REPO/llama.cpp/build" -j "$BUILD_JOBS" --target llama-server
echo "  ✓ llama-server: $REPO/llama.cpp/build/bin/llama-server"

# --- whisper.cpp (OPTIONAL, voice only) — never blocks the LLM above ---
if [ "${SKIP_WHISPER:-}" = 1 ]; then
  cyan "whisper.cpp — skipped (SKIP_WHISPER=1); voice listener disabled"
else
  # whisper-stream (the live-mic transcriber the voice listener uses) needs SDL2. Best-effort
  # auto-install on apt systems so voice builds with no manual step; otherwise warn and carry on.
  ensure_sdl2() {
    { pkg-config --exists sdl2 2>/dev/null || [ -e /usr/include/SDL2/SDL.h ]; } && return 0
    if command -v apt-get >/dev/null 2>&1; then
      local SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo"
      cyan "installing libsdl2-dev (whisper voice dependency)…"
      $SUDO apt-get update -qq && $SUDO apt-get install -y libsdl2-dev
    else
      warn "libsdl2-dev not found (whisper voice needs it) — install it via your package manager"
      return 1
    fi
  }
  whisper_build() {
    ensure_sdl2 || warn "SDL2 not installed — the whisper build will likely fail (voice only)"
    [ -d "$REPO/whisper/.git" ] || git clone --branch v1.8.6 --depth 1 https://github.com/ggerganov/whisper.cpp "$REPO/whisper" || return 1
    cmake -S "$REPO/whisper" -B "$REPO/whisper/build" -DGGML_AVX=ON -DGGML_AVX2=OFF -DGGML_NATIVE=OFF -DWHISPER_SDL2=ON || return 1
    cmake --build "$REPO/whisper/build" -j "$BUILD_JOBS" || return 1
  }
  cyan "whisper.cpp (v1.8.6) — STT for the voice listener; optional"
  if whisper_build; then
    echo "  ✓ whisper: $REPO/whisper/build/bin/"
  else
    warn "whisper build failed → voice unavailable. Install libsdl2-dev and re-run, or SKIP_WHISPER=1. The LLM is built and fine."
  fi
fi

cyan "Done."
echo "  llama-server: $REPO/llama.cpp/build/bin/llama-server"
