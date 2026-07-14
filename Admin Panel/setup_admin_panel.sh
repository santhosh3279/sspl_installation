#!/bin/bash

# SSPL ERP Admin Panel installer
# Installs the web admin panel to /opt/sspl-admin and runs it as a
# systemd service. Run from the directory containing app.py.

set -e

INSTALL_DIR=/opt/sspl-admin

# The repo checkout root (this script lives in "<repo>/Admin Panel/"). The
# panel's Install switches run the installers from here, so this path must
# keep existing on the server after setup.
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVER_IP="$(hostname -I | awk '{print $1}')"

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
# (SSPL_ADMIN_USER, SSPL_ADMIN_PW, SSPL_PANEL_PORT and SSPL_CERT_IP can be
# set by a parent installer to answer the prompts non-interactively)
echo ""
ADMIN_USER="${SSPL_ADMIN_USER:-}"
if [ -z "$ADMIN_USER" ]; then
    read -p "Admin username [admin]: " ADMIN_USER
    ADMIN_USER=${ADMIN_USER:-admin}
fi
PW1="${SSPL_ADMIN_PW:-}"
while [ -z "$PW1" ]; do
    read -sp "Admin password: " PW1; echo ""
    read -sp "Confirm password: " PW2; echo ""
    if [ -n "$PW1" ] && [ "$PW1" = "$PW2" ]; then break; fi
    echo "Passwords are empty or do not match — try again."
    PW1=""
done
PORT="${SSPL_PANEL_PORT:-}"
if [ -z "$PORT" ]; then
    read -p "Port for the panel [8090]: " PORT
    PORT=${PORT:-8090}
fi

# 5. HTTPS certificate (self-signed, valid 10 years; kept on re-install)
CERT_DIR="$INSTALL_DIR/certs"
CERT="$CERT_DIR/sspl-admin.crt"
KEY="$CERT_DIR/sspl-admin.key"
if [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
    CERT_IP="${SSPL_CERT_IP:-}"
    if [ -z "$CERT_IP" ]; then
        DETECTED_IP=$(hostname -I | awk '{print $1}')
        read -p "Server IP for the certificate [$DETECTED_IP]: " CERT_IP
        CERT_IP=${CERT_IP:-$DETECTED_IP}
    fi
    echo "Generating self-signed HTTPS certificate for $CERT_IP..."
    sudo mkdir -p "$CERT_DIR"
    sudo openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$KEY" -out "$CERT" \
        -subj "/CN=sspl-admin" \
        -addext "subjectAltName=IP:$CERT_IP,IP:127.0.0.1,DNS:$(hostname)"
    sudo chmod 600 "$KEY"
else
    echo "Existing HTTPS certificate found — keeping it."
fi

ADMIN_USER="$ADMIN_USER" ADMIN_PW="$PW1" PORT="$PORT" CERT="$CERT" KEY="$KEY" \
    REPO_DIR="$REPO_DIR" SERVER_IP="$SERVER_IP" \
    "$INSTALL_DIR/venv/bin/python" - <<'EOF' | sudo tee "$INSTALL_DIR/config.json" > /dev/null
import json, os, secrets
from werkzeug.security import generate_password_hash
print(json.dumps({
    "username": os.environ["ADMIN_USER"],
    "password_hash": generate_password_hash(os.environ["ADMIN_PW"]),
    "secret_key": secrets.token_hex(32),
    "port": int(os.environ["PORT"]),
    "tls_cert": os.environ["CERT"],
    "tls_key": os.environ["KEY"],
    "repo_dir": os.environ["REPO_DIR"],
    "server_ip": os.environ["SERVER_IP"],
}, indent=2))
EOF
sudo chown root:root "$INSTALL_DIR/config.json"
sudo chmod 600 "$INSTALL_DIR/config.json"

# 6. Systemd service
sudo cp sspl-admin.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sspl-admin
sleep 2
sudo systemctl --no-pager --lines=0 status sspl-admin || true

IP=$(hostname -I | awk '{print $1}')
echo ""
echo "=== Installation Complete ==="
echo ""
echo "Admin panel:   https://$IP:$PORT"
echo "Username:      $ADMIN_USER"
echo ""
echo "NOTE: the certificate is self-signed, so the browser shows a security"
echo "warning the first time — click Advanced -> Proceed. The connection is"
echo "still fully encrypted."
echo "Service:       sudo systemctl status sspl-admin"
echo "Logs:          sudo journalctl -u sspl-admin -f"
echo ""
echo "To change the password later, re-run this installer."
