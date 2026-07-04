#!/bin/bash
# deploy/backup_db.sh — weekly SQLite backup for ADS Agent.
#
# Uses `sqlite3 <db> ".backup <dest>"` (not `cp`) — this is SQLite's own
# backup API, safe to run against a live database in WAL mode (unlike a
# plain file copy, which can grab an inconsistent snapshot mid-write).
#
# Installed/run by: deploy/adsagent-backup.service + .timer (weekly).
set -euo pipefail

APP_DIR="/var/www/adsagent"
DB_PATH="${APP_DIR}/extractions.db"
BACKUP_DIR="${APP_DIR}/backups"
KEEP=8   # ~2 months of weekly backups

mkdir -p "$BACKUP_DIR"

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
DEST="${BACKUP_DIR}/extractions_${TIMESTAMP}.db"

sqlite3 "$DB_PATH" ".backup '${DEST}'"

# Prune: keep only the $KEEP most recent backups
cd "$BACKUP_DIR"
ls -1t extractions_*.db 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

echo "Backup written to ${DEST}"
