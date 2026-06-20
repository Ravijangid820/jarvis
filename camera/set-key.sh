#!/usr/bin/env bash
# Save the camera's API key without quoting pitfalls (no trailing newline; 0600).
#   bash set-key.sh jk-xxxxxxxxxxxx            # device key -> config/agent.key
#   bash set-key.sh jk-xxxxxxxxxxxx --admin    # admin key  -> config/admin.key
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config"
KEY="${1:-}"
[ -n "$KEY" ] || { echo "usage: bash set-key.sh <key> [--admin]"; exit 1; }
FILE="$DIR/agent.key"
[ "${2:-}" = "--admin" ] && FILE="$DIR/admin.key"
mkdir -p "$DIR"
printf '%s' "$KEY" > "$FILE"
chmod 600 "$FILE" 2>/dev/null || true
echo "Wrote $FILE (${#KEY} chars)"
