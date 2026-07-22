#!/bin/bash
# deploy/vacuum_db.sh — monthly SQLite VACUUM for ADS Agent.
#
# Reclaims disk space that deleted rows leave behind: SQLite marks freed pages
# for reuse but never shrinks the file on its own. VACUUM rewrites the database
# compactly, returning the space to the OS. Safe to run on a WAL database; it
# briefly takes a write lock, so this runs in a low-traffic window.
#
# Installed/run by: deploy/adsagent-vacuum.service + .timer (monthly).
set -euo pipefail

APP_DIR="/var/www/adsagent"
DB_PATH="${APP_DIR}/extractions.db"

if [ ! -f "$DB_PATH" ]; then
  echo "No database at ${DB_PATH} — nothing to vacuum."
  exit 0
fi

BEFORE="$(stat -c%s "$DB_PATH" 2>/dev/null || echo 0)"

# Checkpoint the WAL back into the main file first, then compact.
sqlite3 "$DB_PATH" "PRAGMA wal_checkpoint(TRUNCATE); VACUUM;"

AFTER="$(stat -c%s "$DB_PATH" 2>/dev/null || echo 0)"

echo "VACUUM complete. Size before: ${BEFORE} bytes, after: ${AFTER} bytes."
