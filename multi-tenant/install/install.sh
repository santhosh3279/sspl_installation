#!/bin/bash
# SSPL ERP multi-tenant — one-time installer for a FRESH server.
#
#   git clone -b multi-tenant https://github.com/santhosh3279/sspl_installation.git
#   cd sspl_installation/multi-tenant/install
#   ./install.sh
#
# Asks everything up front, then runs unattended:
#   1. the Docker stack (no site yet)       — install-stack.sh
#   2. the tools + backup cron              — install-tools.sh
#   3. the first tenant site                — sites/new-site.sh
#
# Not for a server already running the single-site setup (main branch).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TOOLS_DEST=/opt/sspl-erp/tools
export SSPL_CONF=/opt/sspl-erp/sspl-mt.conf

err() { echo "❌ $*" >&2; exit 1; }
[ "$(id -u)" -ne 0 ] || err "Run as your normal user (with sudo rights), not root."
sudo -v || err "Needs sudo."
command -v git >/dev/null || err "git missing: sudo apt install git -y"
command -v docker >/dev/null || err "Docker missing — install it first."
sudo docker info >/dev/null 2>&1 || err "Docker daemon not running."
sudo docker compose version >/dev/null 2>&1 || sudo apt-get install -y docker-compose-plugin

if sudo test -f /opt/sspl-erp/docker-compose.yml; then
    err "/opt/sspl-erp/docker-compose.yml exists — this server already has a stack. Add tenants with $TOOLS_DEST/sites/new-site.sh"
fi

echo "════ SSPL ERP multi-tenant installer ════"
read -rp "HTTP port [80]: " HTTP_PORT; HTTP_PORT=${HTTP_PORT:-80}
while true; do
    read -rsp "MariaDB root password (new): " DB_PASSWORD; echo ""
    read -rsp "Confirm: " P2; echo ""
    [ -n "$DB_PASSWORD" ] && [ "$DB_PASSWORD" = "$P2" ] && break
    echo "Empty or not matching."
done
read -rp "First tenant hostname (e.g. acme.erp.lan; blank = none yet): " FIRST_SITE
if [ -n "$FIRST_SITE" ]; then
    while true; do
        read -rsp "Administrator password for $FIRST_SITE: " ADMIN_PASSWORD; echo ""
        read -rsp "Confirm: " P2; echo ""
        [ -n "$ADMIN_PASSWORD" ] && [ "$ADMIN_PASSWORD" = "$P2" ] && break
        echo "Empty or not matching."
    done
fi
read -rp "Schedule automatic backups (cron)? [Y/n]: " WANT_CRON
[[ "${WANT_CRON:-y}" =~ ^[Yy] ]] && CRON=--cron || CRON=--no-cron
read -rp "Proceed? [Y/n]: " GO
[[ "${GO:-y}" =~ ^[Yy] ]] || err "Aborted."

echo ""
echo "══ 1/3 Docker stack ══"
sudo env SSPL_CONF="$SSPL_CONF" HTTP_PORT="$HTTP_PORT" DB_PASSWORD="$DB_PASSWORD" \
    bash "$HERE/install-stack.sh"
sudo systemctl enable docker >/dev/null 2>&1 || true

echo ""
echo "══ 2/3 Tools ══"
sudo env SSPL_CONF="$SSPL_CONF" SSPL_TOOLS_DIR="$TOOLS_DEST" bash "$HERE/install-tools.sh" "$CRON"

echo ""
echo "══ 3/3 First tenant ══"
if [ -n "$FIRST_SITE" ]; then
    sudo env SSPL_CONF="$SSPL_CONF" ADMIN_PASSWORD="$ADMIN_PASSWORD" \
        "$TOOLS_DEST/sites/new-site.sh" "$FIRST_SITE" --yes
else
    echo "Skipped. Later: sudo $TOOLS_DEST/sites/new-site.sh <host>"
fi

cat <<EOF

✅ Done.
   Add a tenant:      sudo $TOOLS_DEST/sites/new-site.sh <host>
   List tenants:      sudo $TOOLS_DEST/sites/list-sites.sh --apps
   Backup now:        sudo $TOOLS_DEST/backup/backup.sh --all
   Update ERP:        sudo $TOOLS_DEST/update/update.sh
   Config:            $SSPL_CONF
   Refresh tools:     git pull && sudo $HERE/install-tools.sh
EOF
