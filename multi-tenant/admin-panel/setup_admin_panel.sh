#!/bin/bash
# SSPL ERP Admin Panel installer — multi-tenant edition.
#
# Installs the web admin panel to /opt/sspl-admin and runs it as the
# sspl-admin systemd service. Run it from this folder, on a server that will
# run (or already runs) the multi-tenant stack:
#
#   git clone -b Multi-Site https://github.com/santhosh3279/sspl_installation.git
#   cd sspl_installation/multi-tenant/admin-panel
#   ./setup_admin_panel.sh
#
# Then install the stack and create sites from the browser. Replaces a
# single-site panel if one is installed (same path and service name): a
# server runs one or the other.
#
# Answers can come from the environment (SSPL_ADMIN_USER, SSPL_ADMIN_PW,
# SSPL_PANEL_PORT, SSPL_SERVER_IP) for a non-interactive install.
set -e

INSTALL_DIR=/opt/sspl-admin
HERE="$(cd "$(dirname "$0")" && pwd)"
# The checkout root: the install suite runs the installers from here, so this
# path must keep existing on the server.
REPO_DIR="$(cd "$HERE/../.." && pwd)"
cd "$HERE"

if [ ! -f app.py ] || [ ! -f sspl-admin.service ]; then
    echo "ERROR: app.py / sspl-admin.service not found in $HERE"
    exit 1
fi

echo "=== SSPL ERP Admin Panel Setup (multi-tenant) ==="

if sudo test -f "$INSTALL_DIR/config.json" && ! sudo grep -q '"mt_conf"' "$INSTALL_DIR/config.json"; then
    echo ""
    echo "⚠ $INSTALL_DIR already holds the SINGLE-SITE panel. This replaces it with the"
    echo "  multi-tenant panel (job logs and the HTTPS certificate are kept)."
    read -rp "  Continue? [y/N]: " GO
    [[ "$GO" =~ ^[Yy] ]] || { echo "Cancelled."; exit 0; }
fi

SERVER_IP="${SSPL_SERVER_IP:-}"
if [ -z "$SERVER_IP" ]; then
    DEF=$(hostname -I 2>/dev/null | awk '{print $1}')
    read -rp "This server's IP address [$DEF]: " SERVER_IP
    SERVER_IP=${SERVER_IP:-$DEF}
fi
[ -n "$SERVER_IP" ] || { echo "ERROR: server IP is required"; exit 1; }

# 1. Directories
sudo mkdir -p "$INSTALL_DIR" "$INSTALL_DIR/jobs" /opt/backups/frappe/uploads

# 2. Python virtualenv with Flask
if [ ! -x "$INSTALL_DIR/venv/bin/python" ] || [ ! -x "$INSTALL_DIR/venv/bin/pip" ]; then
    echo "Creating Python virtualenv..."
    sudo rm -rf "$INSTALL_DIR/venv"
    sudo python3 -m venv "$INSTALL_DIR/venv"
fi
echo "Installing Flask..."
sudo "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip flask

# 3. App
sudo cp app.py "$INSTALL_DIR/"

# 4. Credentials and config
echo ""
ADMIN_USER="${SSPL_ADMIN_USER:-}"
if [ -z "$ADMIN_USER" ]; then
    read -rp "Admin username [admin]: " ADMIN_USER
    ADMIN_USER=${ADMIN_USER:-admin}
fi
PW1="${SSPL_ADMIN_PW:-}"
while [ -z "$PW1" ]; do
    read -rsp "Admin password: " PW1; echo ""
    read -rsp "Confirm password: " PW2; echo ""
    if [ -n "$PW1" ] && [ "$PW1" = "$PW2" ]; then break; fi
    echo "Passwords are empty or do not match — try again."
    PW1=""
done
PORT="${SSPL_PANEL_PORT:-}"
if [ -z "$PORT" ]; then
    read -rp "Port for the panel [8090]: " PORT
    PORT=${PORT:-8090}
fi

# 5. HTTPS certificate (self-signed, valid 10 years; kept on re-install)
CERT_DIR="$INSTALL_DIR/certs"
CERT="$CERT_DIR/sspl-admin.crt"
KEY="$CERT_DIR/sspl-admin.key"
if ! sudo test -f "$CERT" || ! sudo test -f "$KEY"; then
    echo "Generating self-signed HTTPS certificate for $SERVER_IP..."
    sudo mkdir -p "$CERT_DIR"
    sudo openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$KEY" -out "$CERT" \
        -subj "/CN=sspl-admin" \
        -addext "subjectAltName=IP:$SERVER_IP,IP:127.0.0.1,DNS:$(hostname)"
    sudo chmod 600 "$KEY"
else
    echo "Existing HTTPS certificate found — keeping it."
fi

ADMIN_USER="$ADMIN_USER" ADMIN_PW="$PW1" PORT="$PORT" CERT="$CERT" KEY="$KEY" \
    REPO_DIR="$REPO_DIR" SERVER_IP="$SERVER_IP" \
    "$INSTALL_DIR/venv/bin/python" - <<'EOF' | sudo tee "$INSTALL_DIR/config.json.new" > /dev/null
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
    "mt_conf": "/opt/sspl-erp/sspl-mt.conf",
    "tools_dir": "/opt/sspl-erp/tools",
}, indent=2))
EOF
sudo chown root:root "$INSTALL_DIR/config.json.new"
sudo chmod 600 "$INSTALL_DIR/config.json.new"
sudo mv -f "$INSTALL_DIR/config.json.new" "$INSTALL_DIR/config.json"

# 6. Systemd service
sudo cp sspl-admin.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable sspl-admin >/dev/null 2>&1
sudo systemctl restart sspl-admin
sleep 2
sudo systemctl --no-pager --lines=0 status sspl-admin || true

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Admin panel:   https://$SERVER_IP:$PORT"
echo "Username:      $ADMIN_USER"
echo ""
echo "Next: open the panel → ERP Next Installation suite → Install stack,"
echo "then Create site on the dashboard for each tenant."
echo ""
echo "The certificate is self-signed: the browser warns the first time —"
echo "click Advanced -> Proceed. The connection is still encrypted."
echo "Service:       sudo systemctl status sspl-admin"
echo "Logs:          sudo journalctl -u sspl-admin -f"
echo "To change the password later, re-run this installer."
