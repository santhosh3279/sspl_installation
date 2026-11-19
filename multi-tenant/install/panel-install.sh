#!/bin/bash
# Non-interactive stack install, for the admin panel's installation suite.
# install.sh is the terminal version: it refuses to run as root and asks its
# questions on a terminal, neither of which a panel job has.
#
# Runs, as root:
#   1. install-stack.sh   the Docker stack, no site yet, and the host nginx
#                         that routes domains to sites (install-proxy.sh)
#   2. install-tools.sh   the tools to $SSPL_TOOLS_DIR (+ cron if asked)
# Sites are created afterwards, one per tenant (sites/new-site.sh, or the
# panel's Create site).
#
# Env (the panel sets these):
#   DB_PASSWORD         MariaDB root password (required)
#   HTTP_PORT           default 80
#   IMAGE               default ghcr.io/santhosh3279/sspl-erpnext
#   SSPL_CONF           default /opt/sspl-erp/sspl-mt.conf
#   SSPL_TOOLS_DIR      default /opt/sspl-erp/tools
#   SSPL_INSTALL_CRON   yes|no (default yes)
#
# SECURITY: never echoes DB_PASSWORD. Do not add 'set -x'.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
: "${DB_PASSWORD:?DB_PASSWORD is required}"
export SSPL_CONF="${SSPL_CONF:-/opt/sspl-erp/sspl-mt.conf}"
export SSPL_TOOLS_DIR="${SSPL_TOOLS_DIR:-/opt/sspl-erp/tools}"
export HTTP_PORT="${HTTP_PORT:-80}"
CRON="--cron"
[ "${SSPL_INSTALL_CRON:-yes}" = yes ] || CRON="--no-cron"

err() { echo "❌ $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || err "Run as root."
command -v docker >/dev/null || err "Docker missing — install it first."
docker info >/dev/null 2>&1 || err "Docker daemon not running."
docker compose version >/dev/null 2>&1 || apt-get install -y docker-compose-plugin
command -v git >/dev/null || err "git missing: apt install git -y"
if [ -f /opt/sspl-erp/docker-compose.yml ]; then
    err "/opt/sspl-erp/docker-compose.yml exists — this server already has a stack."
fi

echo "════ SSPL ERP multi-tenant stack install — $(date) ════"
echo ""
echo "══ 1/2 Docker stack ══"
bash "$HERE/install-stack.sh"
systemctl enable docker >/dev/null 2>&1 || true

echo ""
echo "══ 2/2 Tools ══"
bash "$HERE/install-tools.sh" "$CRON"

echo ""
echo "✅ Stack and tools installed. Create the first site from the panel"
echo "   dashboard (Create site), or: sudo $SSPL_TOOLS_DIR/sites/new-site.sh <host>"
