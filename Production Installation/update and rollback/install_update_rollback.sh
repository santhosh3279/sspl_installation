#!/bin/bash

# Install the SSPL ERP update/rollback scripts to /opt/sspl-erp/v2/.
#
# Env-driven so both the terminal orchestrator and the web admin panel can
# run it. Copies the four sspl-erp-*.sh scripts sitting next to this file
# and sets the site name in the shared config.
#
# Required env:
#   SERVER_IP        the ERPNext site name (e.g. 192.168.225.135)
# Optional env:
#   SSPL_ERP_DIR     deployment directory (default /opt/sspl-erp)   [testing]

set -e

ERP_DIR="${SSPL_ERP_DIR:-/opt/sspl-erp}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

[ -n "$SERVER_IP" ] || { echo "❌ SERVER_IP is required" >&2; exit 1; }

sudo mkdir -p "$ERP_DIR/v2" "$ERP_DIR/image-backups"
sudo cp "$SRC_DIR/"sspl-erp-*.sh "$ERP_DIR/v2/"
sudo sed -i "s/^SITE_NAME=.*/SITE_NAME=\"$SERVER_IP\"/" "$ERP_DIR/v2/sspl-erp-common.sh"
sudo chown root:root "$ERP_DIR/v2/"sspl-erp-*.sh
sudo chmod 755 "$ERP_DIR/v2/"sspl-erp-*.sh
echo "✓ Update/rollback scripts installed to $ERP_DIR/v2/ (site name: $SERVER_IP)"
