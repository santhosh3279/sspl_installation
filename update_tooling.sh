#!/bin/bash

# SSPL tooling updater
#
# Updates everything this repo installs on the server, in one go:
#   1. Backup scripts        -> /opt/scripts/v2/
#   2. Update/rollback scripts -> /opt/sspl-erp/v2/
#   3. Web admin panel       -> /opt/sspl-admin/ (app + service, then restart)
#
# It only refreshes the CODE. All of your configuration is preserved:
# site name, rclone remote, retention days, alert email (re-applied to the
# new scripts from the installed copies), and the panel's config.json,
# HTTPS certificates, and job logs (never touched).
#
# Components that were never installed are skipped with a pointer to the
# right installer. This script does not create first-time installations.
#
# Usage (on the server):
#   cd sspl_installation
#   git pull
#   ./update_tooling.sh

set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKUP_SRC="$REPO_DIR/Backup/frappe_backup_system"
UPDATE_SRC="$REPO_DIR/Production Installation/update and rollback"
PANEL_SRC="$REPO_DIR/Admin Panel"

# Overridable for testing
BACKUP_DST="${SSPL_BACKUP_DST:-/opt/scripts/v2}"
UPDATE_DST="${SSPL_UPDATE_DST:-/opt/sspl-erp/v2}"
PANEL_DST="${SSPL_PANEL_DST:-/opt/sspl-admin}"

# Copy the configuration line for $3 from the installed script $1 into the
# staged new script $2, so user settings survive the update.
preserve_line() {
    local installed="$1" staged="$2" var="$3" old esc
    old=$(sudo grep -m1 "^${var}=" "$installed" 2>/dev/null || true)
    [ -z "$old" ] && return 0
    esc=$(printf '%s' "$old" | sed -e 's/\\/\\\\/g' -e 's/[\/&]/\\&/g')
    sed -i "s/^${var}=.*/${esc}/" "$staged"
}

# Stage $1/$3 with settings preserved from $2/$3, then install it.
update_script() {
    local src_dir="$1" dst_dir="$2" file="$3" staged var
    shift 3
    staged=$(mktemp)
    cp "$src_dir/$file" "$staged"
    for var in "$@"; do
        preserve_line "$dst_dir/$file" "$staged" "$var"
    done
    sudo cp "$staged" "$dst_dir/$file"
    sudo chown root:root "$dst_dir/$file"
    sudo chmod 755 "$dst_dir/$file"
    rm -f "$staged"
    echo "   ✓ $file"
}

UPDATED=""
SKIPPED=""

echo "==============================="
echo " SSPL Tooling Update - $(date)"
echo "==============================="

# ---------------------------------------------------------- 1. backup scripts
echo ""
echo "→ 1/3 Backup scripts ($BACKUP_DST)"
if [ -f "$BACKUP_DST/frappe_backup.sh" ]; then
    update_script "$BACKUP_SRC" "$BACKUP_DST" frappe_backup.sh        SITE_NAME BACKUP_DIR RETENTION_DAYS RCLONE_REMOTE
    update_script "$BACKUP_SRC" "$BACKUP_DST" frappe_db_backup.sh     SITE_NAME BACKUP_DIR RETENTION_DAYS RCLONE_REMOTE
    update_script "$BACKUP_SRC" "$BACKUP_DST" frappe_restore.sh       SITE_NAME
    update_script "$BACKUP_SRC" "$BACKUP_DST" frappe_backup_verify.sh BACKUP_DIR ALERT_EMAIL MAX_AGE_HOURS
    update_script "$BACKUP_SRC" "$BACKUP_DST" restore_with_backup.sh
    UPDATED="$UPDATED backup-scripts"
else
    echo "   – not installed, skipping (install with: Backup/frappe_backup_system/setup_frappe_backups.sh)"
    SKIPPED="$SKIPPED backup-scripts"
fi

# ------------------------------------------------- 2. update/rollback scripts
echo ""
echo "→ 2/3 Update & rollback scripts ($UPDATE_DST)"
if [ -f "$UPDATE_DST/sspl-erp-common.sh" ]; then
    update_script "$UPDATE_SRC" "$UPDATE_DST" sspl-erp-common.sh SITE_NAME SERVICE_WAIT_TIMEOUT
    update_script "$UPDATE_SRC" "$UPDATE_DST" sspl-erp-update-with-rollback.sh RCLONE_REMOTE
    update_script "$UPDATE_SRC" "$UPDATE_DST" sspl-erp-rollback.sh
    update_script "$UPDATE_SRC" "$UPDATE_DST" sspl-erp-backup-manager.sh
    UPDATED="$UPDATED update-scripts"
else
    echo "   – not installed, skipping (see 'Production Installation/update and rollback/README.md')"
    SKIPPED="$SKIPPED update-scripts"
fi

# ------------------------------------------------------------- 3. admin panel
echo ""
echo "→ 3/3 Web admin panel ($PANEL_DST)"
if [ -f "$PANEL_DST/config.json" ]; then
    sudo cp "$PANEL_SRC/app.py" "$PANEL_DST/"
    PANEL_VER=$(grep -m1 '^PANEL_VERSION' "$PANEL_SRC/app.py" | cut -d'"' -f2)
    echo "   ✓ app.py (v$PANEL_VER)"
    # Copying app.py is NOT enough: the running process holds the old code in
    # memory, so without a restart the browser keeps showing the old panel.
    # Never skip this silently — say so loudly if it can't be done.
    if [ -f /etc/systemd/system/sspl-admin.service ]; then
        sudo cp "$PANEL_SRC/sspl-admin.service" /etc/systemd/system/
        sudo systemctl daemon-reload
        echo "   → Restarting sspl-admin service..."
        if sudo systemctl restart sspl-admin; then
            sleep 2
            if sudo systemctl is-active --quiet sspl-admin; then
                echo "   ✓ sspl-admin is running v$PANEL_VER"
                echo "     (browser still showing the old page? hard-refresh: Ctrl+Shift+R)"
            else
                echo "   ❌ sspl-admin failed to start — check: sudo journalctl -u sspl-admin -n 30"
                exit 1
            fi
        else
            echo "   ❌ could not restart sspl-admin — check: sudo journalctl -u sspl-admin -n 30"
            exit 1
        fi
    else
        echo "   ⚠ /etc/systemd/system/sspl-admin.service not found, so the panel was"
        echo "     NOT restarted — it is still running the OLD code. The new app.py is"
        echo "     in place; restart the panel yourself, however you started it."
        SKIPPED="$SKIPPED admin-panel-restart"
    fi
    UPDATED="$UPDATED admin-panel"
else
    echo "   – not installed, skipping (install with: 'Admin Panel/setup_admin_panel.sh')"
    SKIPPED="$SKIPPED admin-panel"
fi

# ---------------------------------------------------------------------- done
echo ""
echo "==============================="
echo "✅ Update complete"
if [ -n "$UPDATED" ]; then echo "   Updated:$UPDATED"; fi
if [ -n "$SKIPPED" ]; then echo "   Skipped:$SKIPPED"; fi
echo ""
echo "Configuration was preserved (site name, rclone remote, retention,"
echo "panel credentials/certificates). Cron jobs were not changed."
