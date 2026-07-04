#!/bin/bash
# ADS Agent — one-time server setup script for Ubuntu 24
# Run as root: sudo bash setup.sh
set -e

APP_DIR="/var/www/adsagent"
APP_USER="adsagent"
REPO_URL="YOUR_GITHUB_REPO_URL"   # e.g. https://github.com/yourname/adsagent.git

echo "=== [1/8] System update and dependencies ==="
apt update && apt upgrade -y
apt install -y python3 python3-pip python3-venv git nginx certbot python3-certbot-nginx ufw sqlite3

echo "=== [2/8] Create dedicated system user ==="
if ! id "$APP_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"
fi

echo "=== [3/8] Clone repository ==="
if [ -d "$APP_DIR/.git" ]; then
    echo "Repo already exists — pulling latest..."
    git -C "$APP_DIR" pull
else
    git clone "$REPO_URL" "$APP_DIR"
fi
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

echo "=== [4/8] Python virtual environment + dependencies ==="
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "=== [5/8] Create .env file (edit this before continuing!) ==="
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    chown "$APP_USER":"$APP_USER" "$APP_DIR/.env"
    echo ""
    echo "  *** ACTION REQUIRED ***"
    echo "  Edit $APP_DIR/.env and fill in:"
    echo "    SECRET_KEY=<long random string>"
    echo "    ANTHROPIC_API_KEY=<your key>"
    echo "    COOKIE_SECURE=true"
    echo "    HSTS_ENABLED=true"
    echo "    DB_PATH=/var/www/adsagent/extractions.db"
    echo "    (and SMTP settings if you want email)"
    echo ""
    read -p "Press ENTER after you have saved .env to continue..."
fi

echo "=== [6/8] Install systemd service ==="
cp "$APP_DIR/deploy/adsagent.service" /etc/systemd/system/adsagent.service
systemctl daemon-reload
systemctl enable adsagent
systemctl restart adsagent
systemctl status adsagent --no-pager

echo "=== [7/8] Install Nginx site config ==="
cp "$APP_DIR/deploy/nginx.conf" /etc/nginx/sites-available/adsagent
ln -sf /etc/nginx/sites-available/adsagent /etc/nginx/sites-enabled/adsagent
rm -f /etc/nginx/sites-enabled/default    # remove default placeholder

echo ""
echo "  *** ACTION REQUIRED ***"
echo "  Edit /etc/nginx/sites-available/adsagent and replace"
echo "  YOUR_DOMAIN_OR_IP with your actual domain or IP."
echo "  Then run: sudo nginx -t && sudo systemctl reload nginx"
echo ""
read -p "Press ENTER after editing the Nginx config..."

nginx -t
systemctl reload nginx

echo "=== [8/8] Firewall ==="
ufw allow OpenSSH
ufw allow 'Nginx Full'   # ports 80 + 443
ufw --force enable
ufw status

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Point your domain DNS A record to this server's public IP."
echo "  2. Get a free SSL certificate:"
echo "       sudo certbot --nginx -d YOUR_DOMAIN"
echo "  3. Check the app is running:"
echo "       sudo systemctl status adsagent"
echo "       sudo journalctl -u adsagent -f"
echo "  4. On first boot the admin password is printed in the journal:"
echo "       sudo journalctl -u adsagent | grep 'Password'"
