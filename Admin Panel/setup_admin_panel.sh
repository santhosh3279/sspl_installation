#!/bin/bash

# SSPL ERP Admin Panel installer
# Installs the web admin panel to /opt/sspl-admin and runs it as a
# systemd service. Run from the directory containing app.py.

set -e

INSTALL_DIR=/opt/sspl-admin

echo "=== SSPL ERP Admin Panel Setup ==="

if [ ! -f app.py ] || [ ! -f sspl-admin.service ]; then
    echo "ERROR: run this script from the 'Admin Panel' directory (app.py not found)"
    exit 1
fi

# 1. Directories
sudo mkdir -p "$INSTALL_DIR" "$INSTALL_DIR/jobs" /opt/backups/frappe/uploads

# 2. Python virtualenv with Flask
if [ ! -x "$INSTALL_DIR/venv/bin/python" ]; then
    echo "Creating Python virtualenv..."
    sudo python3 -m venv "$INSTALL_DIR/venv"
fi
echo "Installing Flask..."
sudo "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip flask

# 3. App
sudo cp app.py "$INSTALL_DIR/"

# 4. Credentials and config
echo ""
read -p "Admin username [admin]: " ADMIN_USER
ADMIN_USER=${ADMIN_USER:-admin}
while true; do
    read -sp "Admin password: " PW1; echo ""
    read -sp "Confirm password: " PW2; echo ""
    if [ -n "$PW1" ] && [ "$PW1" = "$PW2" ]; then break; fi
    echo "Passwords are empty or do not match — try again."
done
read -p "Port for the panel [8090]: " PORT
PORT=${PORT:-8090}

ADMIN_USER="$ADMIN_USER" ADMIN_PW="$PW1" PORT="$PORT" \
    "$INSTALL_DIR/venv/bin/python" - <<'EOF' | sudo tee "$INSTALL_DIR/config.json" > /dev/null
import json, os, secrets
from werkzeug.security import generate_password_hash
print(json.dumps({
    "username": os.environ["ADMIN_USER"],
    "password_hash": generate_password_hash(os.environ["ADMIN_PW"]),
    "secret_key": secrets.token_hex(32),
    "port": int(os.environ["PORT"]),
}, indent=2))
EOF
sudo chown root:root "$INSTALL_DIR/config.json"
sudo chmod 600 "$INSTALL_DIR/config.json"

# 5. Systemd service
sudo cp sspl-admin.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sspl-admin
sleep 2
sudo systemctl --no-pager --lines=0 status sspl-admin || true

IP=$(hostname -I | awk '{print $1}')
echo ""
echo "=== Installation Complete ==="
echo ""
echo "Admin panel:   http://$IP:$PORT"
echo "Username:      $ADMIN_USER"
echo "Service:       sudo systemctl status sspl-admin"
echo "Logs:          sudo journalctl -u sspl-admin -f"
echo ""
echo "To change the password later, re-run this installer."
