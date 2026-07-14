#!/bin/bash

# SSPL ERP — one-time production installer
#
# Automates the whole "SSPL ERP Production Deployment Guide" (the docx in
# this folder) plus the extra tooling in this repo, in a single run:
#
#   1. ERP stack     — frappe_docker + custom image from GHCR, .env,
#                      pdf-fix override, generated docker-compose.yml,
#                      site creation with all apps (one-time only)
#   2. Backup system — /opt/scripts/v2/ + cron jobs
#   3. Update/rollback scripts — /opt/sspl-erp/v2/
#   4. Web admin panel — /opt/sspl-admin/ (HTTPS, port 8090)
#
# All questions are asked UP FRONT; after that the install runs unattended
# (image pull + site creation take 10-20 minutes total).
#
# Usage — on a fresh Ubuntu 24.04 server with Docker installed:
#   git clone https://github.com/santhosh3279/sspl_installation.git
#   cd "sspl_installation/Production Installation"
#   ./install_sspl_erp.sh
#
# Safe to re-run: existing site, .env, certificates and cron jobs are
# detected and kept — nothing is destroyed on a second run.
#
# If ERPNext is already installed and running, this script refuses to run
# (see SSPL_FORCE below). If it does run and finds an OLDER backup cron job
# still active (outside /opt/scripts/v2/), it installs the v2 backup
# scripts but skips scheduling their cron jobs automatically, to avoid
# running backups twice.

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ERP_DIR="${SSPL_ERP_DIR:-/opt/sspl-erp}"   # overridable for testing
FD_DIR=$ERP_DIR/frappe_docker
COMPOSE_FILE=$ERP_DIR/docker-compose.yml
IMAGE=ghcr.io/santhosh3279/sspl-erpnext

err()  { echo "❌ $*" >&2; exit 1; }
step() { echo ""; echo "════════════════════════════════════════"; echo " $*"; echo "════════════════════════════════════════"; }

dc() { sudo docker compose -f "$COMPOSE_FILE" "$@"; }

# Cron lines pointing at /opt/scripts/ but not /opt/scripts/v2/ — an older
# backup setup (e.g. the "Santhosh additions" in the deployment guide)
# already running on this server.
legacy_backup_cron() {
    sudo crontab -l 2>/dev/null | grep -E '/opt/scripts/' | grep -v '/opt/scripts/v2/' || true
}

# ──────────────────────────────────────────────────────────── preflight checks
step "SSPL ERP One-Time Installer — preflight"

if [ "$(id -u)" -eq 0 ]; then
    err "Run as your normal user (with sudo rights), not as root."
fi
sudo -v || err "This installer needs sudo access."

command -v git >/dev/null    || err "git is not installed:  sudo apt install git -y"
command -v docker >/dev/null || err "Docker is not installed. Install it first (see section 2 of the guide)."
sudo docker info >/dev/null 2>&1 || err "Docker daemon is not running:  sudo systemctl start docker"

if ! sudo docker compose version >/dev/null 2>&1; then
    echo "→ docker compose plugin missing — installing..."
    sudo apt-get install -y docker-compose-plugin
fi
echo "✓ git, docker and docker compose are available"

# This installer is for FRESH servers. If ERPNext is already deployed here,
# say so and stop before touching anything. (SSPL_FORCE=1 bypasses this,
# e.g. to finish a half-completed install — re-running is safe: existing
# site, .env files and certificates are detected and kept.)
if [ -z "$SSPL_FORCE" ] && [ -f "$COMPOSE_FILE" ]; then
    RUNNING=$(sudo docker compose -f "$COMPOSE_FILE" ps --status running --services 2>/dev/null | grep -c . || true)
    echo ""
    if [ "$RUNNING" -gt 0 ]; then
        echo "✅ ERPNext is already installed and running here ($RUNNING services up)."
        echo ""
        echo "Nothing to do. Useful commands instead:"
        echo "  Update ERP to the latest release:  sudo /opt/sspl-erp/v2/sspl-erp-update-with-rollback.sh"
        echo "  Refresh this repo's tooling:       git pull && ./update_tooling.sh"
        echo ""
        echo "To force a re-run anyway:            SSPL_FORCE=1 ./install_sspl_erp.sh"
        exit 0
    fi
    echo "⚠ ERPNext is already installed here ($COMPOSE_FILE exists) but not running."
    echo ""
    echo "  Start it with:      sudo docker compose -f $COMPOSE_FILE up -d"
    echo "  Force a re-install: SSPL_FORCE=1 ./install_sspl_erp.sh"
    exit 1
fi

# ─────────────────────────────────────────────────────── ask everything upfront
step "Configuration — answer once, then the install runs unattended"

DETECTED_IP=$(hostname -I | awk '{print $1}')
read -p "Server LAN IP (this is also the ERP site name) [$DETECTED_IP]: " SERVER_IP
SERVER_IP=${SERVER_IP:-$DETECTED_IP}

read -p "HTTP port for the ERP frontend [80]: " HTTP_PORT
HTTP_PORT=${HTTP_PORT:-80}

while true; do
    read -sp "MariaDB root password (new DB, choose a strong one): " DB_PASSWORD; echo ""
    read -sp "Confirm MariaDB root password: " DB_PASSWORD2; echo ""
    [ -n "$DB_PASSWORD" ] && [ "$DB_PASSWORD" = "$DB_PASSWORD2" ] && break
    echo "Passwords are empty or do not match — try again."
done

while true; do
    read -sp "ERPNext Administrator password: " ADMIN_PASSWORD; echo ""
    read -sp "Confirm Administrator password: " ADMIN_PASSWORD2; echo ""
    [ -n "$ADMIN_PASSWORD" ] && [ "$ADMIN_PASSWORD" = "$ADMIN_PASSWORD2" ] && break
    echo "Passwords are empty or do not match — try again."
done

read -p "Install backup system (cron backups to /opt/backups/frappe)? [Y/n]: " WANT_BACKUPS
read -p "Install update/rollback scripts (/opt/sspl-erp/v2)? [Y/n]: "        WANT_UPDATES
read -p "Install web admin panel (https://$SERVER_IP:8090)? [Y/n]: "          WANT_PANEL
WANT_BACKUPS=${WANT_BACKUPS:-y}; WANT_UPDATES=${WANT_UPDATES:-y}; WANT_PANEL=${WANT_PANEL:-y}

PANEL_USER=""; PANEL_PW=""
if [[ "$WANT_PANEL" =~ ^[Yy] ]]; then
    if ! python3 -m venv --help >/dev/null 2>&1; then
        echo "→ python3-venv missing — installing..."
        sudo apt-get install -y python3-venv
    fi
    read -p "Admin panel username [admin]: " PANEL_USER
    PANEL_USER=${PANEL_USER:-admin}
    while true; do
        read -sp "Admin panel password: " PANEL_PW; echo ""
        read -sp "Confirm panel password: " PANEL_PW2; echo ""
        [ -n "$PANEL_PW" ] && [ "$PANEL_PW" = "$PANEL_PW2" ] && break
        echo "Passwords are empty or do not match — try again."
    done
fi

echo ""
echo "Ready to install:  site $SERVER_IP on port $HTTP_PORT"
read -p "Proceed? [Y/n]: " GO
[[ "${GO:-y}" =~ ^[Yy] ]] || err "Aborted by user."

# ───────────────────────────────────────────── 1. working directory + frappe_docker
step "1/7 Working directory and frappe_docker"

sudo mkdir -p "$ERP_DIR"
sudo chown "$USER:$USER" "$ERP_DIR"

if [ -d "$FD_DIR/.git" ]; then
    echo "✓ frappe_docker already cloned — keeping it"
else
    git clone https://github.com/frappe/frappe_docker "$FD_DIR"
fi

# Docker group for day-to-day use (takes effect after next login; the
# installer itself always uses sudo so this doesn't block anything)
if ! id -nG "$USER" | grep -qw docker; then
    sudo usermod -aG docker "$USER"
    echo "✓ Added $USER to the docker group (log out/in once to use docker without sudo)"
fi

# ─────────────────────────────────────────────────────────── 2. .env + override
step "2/7 Environment file and pdf-fix override"

ENV_FILE="$FD_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%Y%m%d_%H%M%S)"
    echo "✓ Existing .env backed up"
fi
cat > "$ENV_FILE" <<EOF
# Image
CUSTOM_IMAGE=$IMAGE
CUSTOM_TAG=latest
PULL_POLICY=always

# Site — the server's LAN IP
SITE_NAME=$SERVER_IP
FRAPPE_SITE_NAME_HEADER=$SERVER_IP

# Database root password
DB_PASSWORD=$DB_PASSWORD

# Workers
WORKER_REPLICAS=2

# External port: browsers use http://$SERVER_IP:$HTTP_PORT and socket.io
# must be told the same external port (not the internal 9000)
HTTP_PUBLISH_PORT=$HTTP_PORT
SOCKETIO_PORT=$HTTP_PORT
EOF
chmod 600 "$ENV_FILE"
echo "✓ Wrote $ENV_FILE"

# The update/rollback scripts read the root password from /opt/sspl-erp/.env
cat > "$ERP_DIR/.env" <<EOF
MARIADB_ROOT_PASSWORD=$DB_PASSWORD
EOF
chmod 600 "$ERP_DIR/.env"
echo "✓ Wrote $ERP_DIR/.env (used by the update/rollback scripts)"

# PDF-fix override: lets wkhtmltopdf inside the containers reach the site
# through host.docker.internal, and pins socketio_port to the external port
cat > "$FD_DIR/overrides/compose.pdf-fix.yaml" <<'EOF'
services:
  backend:
    extra_hosts:
      - "host.docker.internal:host-gateway"
  queue-short:
    extra_hosts:
      - "host.docker.internal:host-gateway"
  queue-long:
    extra_hosts:
      - "host.docker.internal:host-gateway"
  scheduler:
    extra_hosts:
      - "host.docker.internal:host-gateway"
  configurator:
    environment:
      FRAPPE_HOST_NAME: "http://host.docker.internal:${HTTP_PUBLISH_PORT:-8080}"
    command:
      - |
        ls -1 apps > sites/apps.txt
        bench set-config -g db_host $$DB_HOST
        bench set-config -gp db_port $$DB_PORT
        bench set-config -g redis_cache "redis://$$REDIS_CACHE"
        bench set-config -g redis_queue "redis://$$REDIS_QUEUE"
        bench set-config -g redis_socketio "redis://$$REDIS_QUEUE"
        bench set-config -gp socketio_port $$SOCKETIO_PORT
        bench set-config -g host_name "$$FRAPPE_HOST_NAME"
EOF
echo "✓ Wrote overrides/compose.pdf-fix.yaml"

# ─────────────────────────────────────────────── 3. generate docker-compose.yml
step "3/7 Generating $COMPOSE_FILE"

(cd "$FD_DIR" && sudo docker compose \
    -f compose.yaml \
    -f overrides/compose.mariadb.yaml \
    -f overrides/compose.redis.yaml \
    -f overrides/compose.noproxy.yaml \
    -f overrides/compose.pdf-fix.yaml \
    --env-file .env config) > "$COMPOSE_FILE"
echo "✓ Generated merged compose file"

if ! grep -Eq "published:? \"?$HTTP_PORT\"?" "$COMPOSE_FILE"; then
    echo "⚠ WARNING: port $HTTP_PORT not found in the generated compose file."
    echo "  Check the 'frontend' service ports in $COMPOSE_FILE before going live."
fi

# ─────────────────────────────────────────────────────── 4. pull + start stack
step "4/7 Pulling image and starting containers"

sudo docker pull "$IMAGE:latest"
dc up -d
dc ps

echo "→ Waiting for database to be ready..."
WAITED=0
until dc exec -T -e MYSQL_PWD="$DB_PASSWORD" db mariadb-admin -uroot ping --silent >/dev/null 2>&1; do
    WAITED=$((WAITED + 5))
    if [ "$WAITED" -ge 300 ]; then
        err "Database not ready after 300s — check: sudo docker compose -f $COMPOSE_FILE logs db"
    fi
    sleep 5
done
echo "→ Waiting for backend to be ready..."
until dc exec -T backend true >/dev/null 2>&1; do
    WAITED=$((WAITED + 5))
    if [ "$WAITED" -ge 300 ]; then
        err "Backend not ready after 300s — check: sudo docker compose -f $COMPOSE_FILE logs backend"
    fi
    sleep 5
done
echo "✓ Services are up"

# ──────────────────────────────────────────────────── 5. create the site (once)
step "5/7 ERPNext site $SERVER_IP"

if dc exec -T backend test -d "sites/$SERVER_IP" 2>/dev/null; then
    echo "✓ Site $SERVER_IP already exists — skipping creation (one-time step)"
else
    # Install every app shipped in the image (erpnext, india_compliance,
    # ssplbilling, printer_server_configuration, frappe_whatsapp, ...)
    APP_ARGS=""
    for app in $(dc exec -T backend ls apps); do
        [ "$app" = "frappe" ] && continue
        APP_ARGS="$APP_ARGS --install-app $app"
    done
    echo "→ Creating site with apps:$(echo "$APP_ARGS" | sed 's/--install-app//g')"
    echo "  (this takes 5-10 minutes — do not interrupt)"
    dc exec -T backend bench new-site "$SERVER_IP" \
        --mariadb-root-password "$DB_PASSWORD" \
        --admin-password "$ADMIN_PASSWORD" \
        $APP_ARGS
    echo "✓ Site created"
fi

dc exec -T backend bench use "$SERVER_IP"
echo "✓ $SERVER_IP set as default site"

# Re-create the site DB user with host '%' so grants survive container
# IP changes (same fix the update script applies after every update)
SITE_CONFIG=$(dc exec -T backend bash -c "cat sites/$SERVER_IP/site_config.json" 2>/dev/null || true)
DB_NAME=$(echo "$SITE_CONFIG" | grep -oP '"db_name":\s*"\K[^"]+' || true)
DB_PASS=$(echo "$SITE_CONFIG" | grep -oP '"db_password":\s*"\K[^"]+' || true)
if [ -n "$DB_NAME" ] && [ -n "$DB_PASS" ]; then
    dc exec -T -e MYSQL_PWD="$DB_PASSWORD" db mariadb -uroot -e "
        DROP USER IF EXISTS '${DB_NAME}'@'%';
        CREATE USER '${DB_NAME}'@'%' IDENTIFIED BY '${DB_PASS}';
        GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_NAME}'@'%';
        FLUSH PRIVILEGES;" 2>/dev/null \
        && echo "✓ Database grants fixed for '%' host" \
        || echo "⚠ Grant fix failed (may already be correct)"
fi

sudo systemctl enable docker >/dev/null 2>&1 || true
echo "✓ Docker auto-start on reboot enabled"

# ──────────────────────────────────────────────────────────── 6. extra tooling
step "6/7 Backup system, update/rollback scripts, admin panel"

BACKUP_CRON_SKIPPED=""
if [[ "$WANT_BACKUPS" =~ ^[Yy] ]]; then
    echo "→ Installing backup system (/opt/scripts/v2)..."
    CRON_FLAG=yes
    LEGACY_CRON=$(legacy_backup_cron)
    if [ -n "$LEGACY_CRON" ]; then
        echo "   ⚠ Found existing cron job(s) not under /opt/scripts/v2/ — this looks like an"
        echo "     older backup setup already running on this server:"
        echo "$LEGACY_CRON" | sed 's/^/       /'
        echo "   → Skipping automatic v2 cron install so backups don't run twice."
        echo "     Scripts are still installed at /opt/scripts/v2/ — switch over manually"
        echo "     when ready:  sudo crontab -e  (remove the old lines above, then add the"
        echo "     v2 lines from Backup/frappe_backup_system/frappe_backup.cron)"
        CRON_FLAG=no
        BACKUP_CRON_SKIPPED=1
    fi
    (cd "$REPO_DIR/Backup/frappe_backup_system" && \
        SSPL_SITE_NAME="$SERVER_IP" SSPL_INSTALL_CRON="$CRON_FLAG" SSPL_RUN_TEST=no \
        bash setup_frappe_backups.sh)
else
    echo "– Backup system skipped"
fi

if [[ "$WANT_UPDATES" =~ ^[Yy] ]]; then
    echo "→ Installing update/rollback scripts (/opt/sspl-erp/v2)..."
    sudo mkdir -p "$ERP_DIR/v2" "$ERP_DIR/image-backups"
    sudo cp "$REPO_DIR/Production Installation/update and rollback/"sspl-erp-*.sh "$ERP_DIR/v2/"
    sudo sed -i "s/^SITE_NAME=.*/SITE_NAME=\"$SERVER_IP\"/" "$ERP_DIR/v2/sspl-erp-common.sh"
    sudo chown root:root "$ERP_DIR/v2/"sspl-erp-*.sh
    sudo chmod 755 "$ERP_DIR/v2/"sspl-erp-*.sh
    echo "   ✓ Installed (site name set to $SERVER_IP)"
else
    echo "– Update/rollback scripts skipped"
fi

if [[ "$WANT_PANEL" =~ ^[Yy] ]]; then
    echo "→ Installing web admin panel (/opt/sspl-admin)..."
    (cd "$REPO_DIR/Admin Panel" && \
        SSPL_ADMIN_USER="$PANEL_USER" SSPL_ADMIN_PW="$PANEL_PW" \
        SSPL_PANEL_PORT=8090 SSPL_CERT_IP="$SERVER_IP" \
        bash setup_admin_panel.sh)
else
    echo "– Admin panel skipped"
fi

# ─────────────────────────────────── 7. terminal convenience (sudoers entries)
step "7/7 Terminal shortcuts (sudoers)"

# Lets this user clear RAM caches and trigger a manual backup from the
# terminal without a password (the "Santhosh additions" in the guide)
SUDOERS_FILE="/etc/sudoers.d/${USER}-sspl"
{
    echo "$USER ALL=(root) NOPASSWD: /usr/bin/tee /proc/sys/vm/drop_caches"
    echo "$USER ALL=(root) NOPASSWD: /opt/scripts/v2/frappe_backup.sh"
} | sudo tee "$SUDOERS_FILE" > /dev/null
sudo chmod 440 "$SUDOERS_FILE"
if sudo visudo -c -f "$SUDOERS_FILE" >/dev/null 2>&1; then
    echo "✓ Installed $SUDOERS_FILE"
else
    sudo rm -f "$SUDOERS_FILE"
    echo "⚠ sudoers entry invalid — removed (clear RAM / manual backup will ask for a password)"
fi

# ──────────────────────────────────────────────────────────────────── summary
step "Installation complete ✅"

ERP_URL="http://$SERVER_IP"
if [ "$HTTP_PORT" != 80 ]; then ERP_URL="$ERP_URL:$HTTP_PORT"; fi
echo ""
echo "ERP:            $ERP_URL"
echo "                login: Administrator / (the password you chose)"
if [[ "$WANT_PANEL" =~ ^[Yy] ]]; then
    echo "Admin panel:    https://$SERVER_IP:8090  (login: $PANEL_USER — self-signed"
    echo "                certificate, so the browser warns once: Advanced → Proceed)"
fi
if [[ "$WANT_BACKUPS" =~ ^[Yy] ]]; then
    if [ -n "$BACKUP_CRON_SKIPPED" ]; then
        echo "Backups:        scripts installed, cron NOT scheduled (old cron jobs found —"
        echo "                see the warning above) — /opt/backups/frappe/"
    else
        echo "Backups:        daily 02:00 full + 6-hourly DB — /opt/backups/frappe/"
    fi
fi
if [[ "$WANT_UPDATES" =~ ^[Yy] ]]; then
    echo "Updates:        sudo /opt/sspl-erp/v2/sspl-erp-update-with-rollback.sh"
fi
echo ""
echo "Later, refresh all installed tooling with:  git pull && ./update_tooling.sh"
echo "Full documentation: Install_and_Usage_Guide.pdf in the repo root."
