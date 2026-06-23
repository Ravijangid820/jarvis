#!/usr/bin/env bash
# Jarvis data backup → backups/jarvis-backup-<ts>.tar.gz  (the DB + the vector store).
# Safe to run while the service is up: SQLite ".backup" is an online, consistent copy. Good for cron
# or a systemd timer. The archive holds password/token HASHES + face embeddings — it's chmod 600;
# move copies off-box over a secure channel.
#
#   bash src/scripts/backup.sh                 # one backup, keep newest 14
#   KEEP=30 bash src/scripts/backup.sh         # change retention
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DB="$REPO/memory/jarvis.db"
KEEP="${KEEP:-14}"
TS="$(date -u +%Y%m%d-%H%M%S)"
OUT="$REPO/backups/jarvis-backup-$TS.tar.gz"

mkdir -p "$REPO/backups"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
sqlite3 "$DB" ".backup '$TMP/jarvis.db'"                       # consistent online snapshot
[ -d "$REPO/memory/chroma_db" ] && cp -a "$REPO/memory/chroma_db" "$TMP/chroma_db"
tar -czf "$OUT" -C "$TMP" .
chmod 600 "$OUT"
echo "backup written: $OUT ($(du -h "$OUT" | cut -f1))"

# retention: keep the newest $KEEP
ls -1t "$REPO"/backups/jarvis-backup-*.tar.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
