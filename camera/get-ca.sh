#!/usr/bin/env bash
# Download THIS deployment's CA certificate from its own server into config/ca.crt, so the agent can
# verify the server over HTTPS. The bootstrap fetch is over an untrusted connection (you don't trust
# the CA yet) — VERIFY the printed fingerprint matches what setup_tls.sh showed on the server.
#
#   bash get-ca.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$DIR/.venv/bin/python"
CFG="$DIR/config/config.json"
[ -f "$CFG" ] || { echo "No config/config.json — run setup.sh first."; exit 1; }
URL=$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['server']['url'].rstrip('/'))" "$CFG")

mkdir -p "$DIR/config"
OUT="$DIR/config/ca.crt"
echo "Fetching CA from $URL/ca.crt (untrusted bootstrap)..."
curl -fsSk "$URL/ca.crt" -o "$OUT"      # -k: we don't trust the CA yet — that's what we're fetching
echo "Saved $OUT"
echo "VERIFY this SHA-256 matches the server's setup_tls.sh output before trusting it:"
if command -v sha256sum >/dev/null 2>&1; then sha256sum "$OUT" | awk '{print "  "$1}'; else shasum -a 256 "$OUT" | awk '{print "  "$1}'; fi
