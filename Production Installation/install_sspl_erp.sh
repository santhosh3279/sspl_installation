#!/bin/bash

# SSPL ERP — one-time production installer
#
# Automates the whole "SSPL ERP Production Deployment Guide" (the docx in
# this folder) plus the extra tooling in this repo, in a single run:
#
#   1. ERP stack     — via install_erp_stack.sh (frappe_docker, .env,
#                      compose, image pull, start, site creation)
#   2. Backup system — /opt/scripts/v2/ + cron jobs
#      Update/rollback scripts — via update and rollback/install_update_rollback.sh
#      Web admin panel — /opt/sspl-admin/ (HTTPS, port 8090)
#   3. Terminal shortcuts (sudoers) for clear-RAM and manual backup
#
# The ERP-stack and update/rollback steps are delegated to standalone,
# env-driven scripts so the web admin panel can run the exact same logic
# from its "Install" switches. This file is the terminal fresh-server flow.
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
HERE="$(cd "$(dirname "$0")" && pwd)"
ERP_DIR="${SSPL_ERP_DIR:-/opt/sspl-erp}"   # overridable for testing
COMPOSE_FILE=$ERP_DIR/docker-compose.yml

err()  { echo "❌ $*" >&2; exit 1; }
step() { echo ""; echo "════════════════════════════════════════"; echo " $*"; echo "════════════════════════════════════════"; }

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

# ───────────────────────────────────────────────── 1. ERPNext Docker stack
step "1/3 ERPNext Docker stack and site"

# Docker group for day-to-day use (terminal convenience; the installer
# itself always uses sudo so this doesn't block anything). The panel path
# doesn't need this, so it lives in the orchestrator, not the stack script.
if ! id -nG "$USER" | grep -qw docker; then
    sudo usermod -aG docker "$USER"
    echo "✓ Added $USER to the docker group (log out/in once to use docker without sudo)"
fi

# All the heavy lifting (frappe_docker, .env, compose, pull, start, site
# creation, grants) lives in install_erp_stack.sh, shared with the panel.
SERVER_IP="$SERVER_IP" HTTP_PORT="$HTTP_PORT" \
    DB_PASSWORD="$DB_PASSWORD" ADMIN_PASSWORD="$ADMIN_PASSWORD" \
    SSPL_ERP_DIR="$ERP_DIR" \
    bash "$HERE/install_erp_stack.sh"

# ──────────────────────────────────────────────────────────── 2. extra tooling
step "2/3 Backup system and update/rollback scripts"

if [[ "$WANT_BACKUPS" =~ ^[Yy] ]]; then
    echo "→ Installing backup system (/opt/scripts/v2)..."
    # setup_frappe_backups.sh itself skips cron scheduling if it detects an
    # older backup cron job (so backups can't run twice) — that check is
    # shared by this path and the web panel's "Install backups" switch.
    (cd "$REPO_DIR/Backup/frappe_backup_system" && \
        SSPL_SITE_NAME="$SERVER_IP" SSPL_INSTALL_CRON=yes SSPL_RUN_TEST=no \
        bash setup_frappe_backups.sh)
else
    echo "– Backup system skipped"
fi

if [[ "$WANT_UPDATES" =~ ^[Yy] ]]; then
    echo "→ Installing update/rollback scripts (/opt/sspl-erp/v2)..."
    SERVER_IP="$SERVER_IP" SSPL_ERP_DIR="$ERP_DIR" \
        bash "$HERE/update and rollback/install_update_rollback.sh"
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

# ─────────────────────────────────── 3. terminal convenience (sudoers entries)
step "3/3 Terminal shortcuts (sudoers)"

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
    echo "Backups:        installed — /opt/backups/frappe/ (see the backup step above"
    echo "                for whether cron was scheduled or skipped)"
fi
if [[ "$WANT_UPDATES" =~ ^[Yy] ]]; then
    echo "Updates:        sudo /opt/sspl-erp/v2/sspl-erp-update-with-rollback.sh"
fi
echo ""
echo "Later, refresh all installed tooling with:  git pull && ./update_tooling.sh"
echo "Full documentation: Install_and_Usage_Guide.pdf in the repo root."
