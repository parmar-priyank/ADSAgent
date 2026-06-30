#!/bin/bash
# ADS Agent — pull latest code and restart
# Run as root: sudo bash update.sh
set -e

APP_DIR="/var/www/adsagent"

echo "=== Pulling latest code ==="
git -C "$APP_DIR" pull

echo "=== Installing/updating dependencies ==="
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "=== Restarting app ==="
systemctl restart adsagent
systemctl status adsagent --no-pager

echo "=== Done ==="
