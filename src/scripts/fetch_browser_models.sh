#!/usr/bin/env bash
# Fetch the BROWSER-SIDE model assets into a models root, laid out exactly as the web UI
# addresses them.
#
#   bash src/scripts/fetch_browser_models.sh <models-root>
#
# These three bundles are downloaded by a Web Worker in the user's browser, from whichever
# origin serves the SPA — never from the API origin (the workers build their URLs from Vite's
# BASE_URL; see frontend/src/{face,wake,whisper}-worker.js). So EVERY image that serves the SPA
# has to carry them, and this script is the one place their sources and hashes live:
#
#   <root>/face                              -> /face-models/…   YuNet + SFace     (~38 MB)
#   <root>/wake                              -> /wake-models/…   openWakeWord      (~3.6 MB)
#   <root>/stt/onnx-community/whisper-base.en -> /stt-models/…   Whisper base.en   (~76 MB)
#
# Callers: src/scripts/download_models.sh (native install), Dockerfile.orchestrator,
# Dockerfile.combined, Dockerfile.frontend, and .github/workflows/deploy-pages.yml.
#
# Every file is pinned by SHA-256 against its FIRST-PARTY source. A hash mismatch deletes the
# file and fails — a build must not ship weights we cannot vouch for.
#
# Env (all optional):
#   SKIP_FACE_MODELS=1   skip YuNet + SFace       (browser face enrol/recognition goes dark)
#   SKIP_WAKE_MODELS=1   skip openWakeWord        (the "hey jarvis" wake word goes dark)
#   SKIP_STT_MODEL=1     skip the Whisper copy    (browser STT then depends on huggingface.co)
#   FACE_ZOO / WAKE_BASE / STT_BASE   override the upstream base URLs (mirrors, air-gapped hosts)
set -uo pipefail

ROOT="${1:-}"
[ -n "$ROOT" ] || { echo "usage: $0 <models-root>" >&2; exit 2; }
mkdir -p "$ROOT" || exit 1

cyan() { printf '\033[1;36m▸ %s\033[0m\n' "$1"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$1"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$1"; }

# $1=absolute dest  $2=url  $3=sha256 — skip when present+verified; else download (retry+resume).
fetch() {
  dest="$1"; url="$2"; sum="$3"
  if [ -f "$dest" ] && echo "$sum  $dest" | sha256sum -c - >/dev/null 2>&1; then return 0; fi
  mkdir -p "$(dirname "$dest")"
  curl -L --fail --retry 5 --retry-all-errors --retry-delay 5 -C - -o "$dest" "$url" || return 1
  echo "$sum  $dest" | sha256sum -c - >/dev/null 2>&1 \
    || { warn "$(basename "$dest") SHA-256 MISMATCH — deleting"; rm -f "$dest"; return 1; }
}

RC=0

# --- Face: YuNet detector + SFace recognizer, from the OFFICIAL OpenCV Zoo ---------------------
# The SAME two files (and hashes) the camera agent uses — identical weights on both sides is what
# makes a face enrolled in a browser comparable with one enrolled by a Pi. Unlike the STT model
# there is NO upstream fallback: the Zoo serves these over git-LFS without permissive CORS, so a
# browser cannot fetch them cross-origin. Whoever serves the SPA must host them.
FACE_ZOO="${FACE_ZOO:-https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models}"
if [ "${SKIP_FACE_MODELS:-0}" = "1" ]; then
  warn "SKIP_FACE_MODELS=1 — browser face enrollment/recognition will be unavailable"
else
  cyan "Browser face models: YuNet + SFace (~38 MB)"
  if fetch "$ROOT/face/face_detection_yunet_2023mar.onnx" \
       "$FACE_ZOO/face_detection_yunet/face_detection_yunet_2023mar.onnx" \
       8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4 \
  && fetch "$ROOT/face/face_recognition_sface_2021dec.onnx" \
       "$FACE_ZOO/face_recognition_sface/face_recognition_sface_2021dec.onnx" \
       0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79; then
    ok "face models present + verified ($ROOT/face)"
  else
    warn "face models incomplete"; RC=1
  fi
fi

# --- Wake word: openWakeWord "hey jarvis", from the project's own GitHub release ---------------
# Three models run as a chain: melspectrogram -> speech embedding -> the "hey jarvis" classifier.
# Together ~3.6 MB and cheap enough to run continuously — the whole point of a keyword spotter.
WAKE_BASE="${WAKE_BASE:-https://github.com/dscripka/openWakeWord/releases/download/v0.5.1}"
if [ "${SKIP_WAKE_MODELS:-0}" = "1" ]; then
  warn "SKIP_WAKE_MODELS=1 — the wake word will be unavailable in the browser"
else
  cyan "Browser wake-word models: openWakeWord hey_jarvis (~3.6 MB)"
  if fetch "$ROOT/wake/melspectrogram.onnx"  "$WAKE_BASE/melspectrogram.onnx" \
       ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f \
  && fetch "$ROOT/wake/embedding_model.onnx" "$WAKE_BASE/embedding_model.onnx" \
       70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f \
  && fetch "$ROOT/wake/hey_jarvis_v0.1.onnx" "$WAKE_BASE/hey_jarvis_v0.1.onnx" \
       94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb; then
    ok "wake-word models present + verified ($ROOT/wake)"
  else
    warn "wake-word models incomplete"; RC=1
  fi
fi

# --- STT: Whisper base.en (ONNX q8) — the FAILSAFE copy ---------------------------------------
# base.en, not the multilingual base of the same size: measured against Piper ground truth the
# multilingual model scored 3.7% WER and base.en 0%, at identical quantisation and size. MUST stay
# in step with MODEL_ID in frontend/src/whisper-worker.js — if they diverge, the offline path
# silently serves a different model than the online one. The browser normally pulls this straight
# from huggingface.co (the official first-party source); this copy exists only so voice input still
# works when that fetch can't happen: an air-gapped LAN, blocked egress, or an HF outage.
STT_BASE="${STT_BASE:-https://huggingface.co/onnx-community/whisper-base.en/resolve/main}"
STT_DIR="$ROOT/stt/onnx-community/whisper-base.en"
if [ "${SKIP_STT_MODEL:-0}" = "1" ]; then
  warn "SKIP_STT_MODEL=1 — browser STT will rely on huggingface.co with no local fallback"
else
  cyan "Browser STT model: whisper-base.en q8 (failsafe copy, ~76 MB)"
  if fetch "$STT_DIR/config.json"              "$STT_BASE/config.json" \
       c8a0de5ed8a083565a4319db29d0c210fda35b4d6076c2d711cae53ae00f3cb1 \
  && fetch "$STT_DIR/generation_config.json"   "$STT_BASE/generation_config.json" \
       3479b1f44a07e41db799e22599222fee5816738036def94a39841cb9cdbb4120 \
  && fetch "$STT_DIR/preprocessor_config.json" "$STT_BASE/preprocessor_config.json" \
       a6a76d28c93edb273669eb9e0b0636a2bddbb1272c3261e47b7ca6dfdbac1b8d \
  && fetch "$STT_DIR/tokenizer.json"           "$STT_BASE/tokenizer.json" \
       5eb60cec1e77aeeb6869a2bb5a8e01a84c3fe5d072d75369343021fe6f5310d0 \
  && fetch "$STT_DIR/tokenizer_config.json"    "$STT_BASE/tokenizer_config.json" \
       93879c3dccdd4b976f709acd85b44778873f30c275e67026f30ca1e4c975230c \
  && fetch "$STT_DIR/onnx/encoder_model_quantized.onnx" \
       "$STT_BASE/onnx/encoder_model_quantized.onnx" \
       6e8001198c490bbae018c0044f630c2915efb826bad957006ce36152d0ab2a10 \
  && fetch "$STT_DIR/onnx/decoder_model_merged_quantized.onnx" \
       "$STT_BASE/onnx/decoder_model_merged_quantized.onnx" \
       dd4761a3f7add26afda3512abff4706920404c2517e85a9f2ff090b0c0987909; then
    ok "browser STT failsafe bundle present + verified ($STT_DIR)"
  else
    warn "STT failsafe bundle incomplete"; RC=1
  fi
fi

exit "$RC"
