# Backup & restore

Jarvis's irreplaceable data is the **database** (`memory/jarvis.db` — users, knowledge, chat history,
face embeddings, audit log) and the **vector store** (`memory/chroma_db`). Models and `config/` are
re-creatable, so backups don't include them. A backup archive holds **password/token hashes + face
embeddings** — treat it as sensitive (it's written `chmod 600`).

## Make a backup
- **Web UI:** Admin → **System → Backups → "Back up now"**, then **Download** to keep a copy off-box.
- **CLI / scheduled:** `bash src/scripts/backup.sh` (online + consistent; safe while running). Keeps the
  newest 14 by default (`KEEP=30 …` to change). For automatic daily backups, add a cron line or a
  systemd timer, e.g. cron:
  ```
  0 3 * * *  cd /srv/jarvis && bash src/scripts/backup.sh >> logs/backup.log 2>&1
  ```

A backup is a `.tar.gz` containing `jarvis.db` + `chroma_db/`.

## Restore (manual)
Restore overwrites live data, so it's deliberate/manual:

```bash
sudo systemctl stop jarvis-orchestrator
cd /srv/jarvis
tar -xzf backups/jarvis-backup-YYYYMMDD-HHMMSS.tar.gz -C /tmp/jrestore   # mkdir it first
cp /tmp/jrestore/jarvis.db memory/jarvis.db
rm -rf memory/chroma_db && cp -a /tmp/jrestore/chroma_db memory/chroma_db
chown -R jarvis:jarvis memory
sudo systemctl start jarvis-orchestrator
```

Then confirm: `curl --cacert tls/ca.crt https://127.0.0.1:5000/health` and log in.
