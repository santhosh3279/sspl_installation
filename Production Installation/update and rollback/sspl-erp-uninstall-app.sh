#!/bin/bash
# Uninstall one app from the site, on the images running now.
#
# Usage: sspl-erp-uninstall-app.sh <app>
#
# Run this BEFORE updating to an image that no longer ships the app:
# 'uninstall-app' needs the app's code, which only the current image has, and
# an update to an image without it refuses to start while it is installed.
#
# Uninstalling DELETES the app's DocTypes and all their data. A full backup is
# taken first and must succeed. Non-interactive — the Admin Panel runs it.
set -e

cd /opt/sspl-erp
source "$(dirname "$0")/sspl-erp-common.sh"

APP="$1"
if [ -z "$APP" ] || [ "$#" -ne 1 ]; then
    echo "Usage: $0 <app>" >&2
    exit 2
fi
if ! [[ "$APP" =~ ^[a-z][a-z0-9_]*$ ]]; then
    echo "❌ '$APP' is not a valid app name." >&2
    exit 2
fi
if [ "$APP" = "frappe" ]; then
    echo "❌ frappe is the framework itself and cannot be uninstalled." >&2
    exit 2
fi

# Whatever ends this script, the site must not be left offline.
trap 'migrate_window_close' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "=============================="
echo " SSPL ERP Uninstall $APP - $(date)"
echo "=============================="

if ! INSTALLED=$(site_installed_apps) || [ -z "$INSTALLED" ]; then
    echo "❌ Could not list the apps installed on $SITE_NAME. Nothing was changed."
    exit 1
fi
if ! printf '%s\n' "$INSTALLED" | grep -qx -- "$APP"; then
    echo "❌ $APP is not installed on $SITE_NAME. Nothing was changed."
    exit 1
fi

# No 'continue anyway?' here, unlike the update: this deletes data, and the
# backup is the only way back.
echo "→ Running Frappe backup..."
if ! sudo /opt/scripts/v2/frappe_backup.sh; then
    echo "❌ Backup failed — $APP was NOT uninstalled. Nothing was changed."
    exit 1
fi
echo "   ✓ Frappe backup completed successfully"

# Nothing may run the app's jobs or write its records while its tables go.
migrate_window_open

echo "→ Uninstalling $APP from $SITE_NAME (deletes its data)..."
if ! docker compose -f "$COMPOSE_FILE" exec -T backend \
        bench --site "$SITE_NAME" uninstall-app "$APP" --yes; then
    echo ""
    echo "❌ Uninstalling $APP failed."
    echo "   The database may have been changed: the app can be half removed."
    echo "   Read the traceback above, then either fix the cause and run this"
    echo "   again, or restore the backup this run took at the start:"
    echo "   sudo /opt/scripts/v2/frappe_restore.sh <backup-folder>"
    exit 1
fi
echo "   ✓ $APP uninstalled"

# Until the image drops it, an update would otherwise reinstall it as a new
# app — see install_new_apps. The update forgets it once the image catches up.
mkdir -p "$BACKUP_DIR"
if ! grep -qx -- "$APP" "$UNINSTALLED_APPS_FILE" 2>/dev/null; then
    echo "$APP" >> "$UNINSTALLED_APPS_FILE" \
        || echo "   ⚠ Could not record $APP in $UNINSTALLED_APPS_FILE — an update"\
                "before the image drops $APP will install it again"
fi

echo "→ Clearing cache..."
docker compose -f "$COMPOSE_FILE" exec -T backend \
    bench --site "$SITE_NAME" clear-cache || echo "   ⚠ Cache clear failed"

migrate_window_close

echo ""
echo "✅ $APP uninstalled."
echo "   Next: publish the image without $APP, then run the update."
