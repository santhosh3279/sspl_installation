#!/bin/bash
# Uninstall one app from ONE site. Deletes that app's data on that site.
#
# Usage: uninstall-app.sh --site <host> <app>
#
# A full backup of the site is taken first and must succeed. The app is then
# recorded for the site so later updates do not install it again. Run this
# before updating to an image that no longer ships the app — for EVERY site
# that has it (the update lists them).
set -euo pipefail
source "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"

parse_site_args "$@"
[ ${#SITES[@]} -eq 1 ] && [ ${#REST[@]} -eq 1 ] || err "Usage: $0 --site <host> <app>"
SITE="${SITES[0]}"; APP="${REST[0]}"
[[ "$APP" =~ ^[a-z][a-z0-9_]*$ ]] || err "'$APP' is not a valid app name"
[ "$APP" != frappe ] || err "frappe is the framework itself and cannot be uninstalled"
require_site "$SITE"
take_lock

echo "=== Uninstall $APP from $SITE — $(date) ==="
INSTALLED=$(site_installed_apps "$SITE") || err "Could not list apps on $SITE. Nothing changed."
printf '%s\n' "$INSTALLED" | grep -qx -- "$APP" || err "$APP is not installed on $SITE. Nothing changed."

echo "→ Full backup of $SITE..."
"$TOOLS_DIR/backup/backup.sh" --site "$SITE" || err "Backup failed — $APP NOT uninstalled. Nothing changed."

SITE_OFF=""
finish() {
    jobs_start
    if [ -n "$SITE_OFF" ]; then
        site_online "$SITE" && SITE_OFF="" || echo "❌ $SITE is still in maintenance mode — turn it off by hand."
    fi
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

site_offline "$SITE"; SITE_OFF=yes
jobs_stop

echo "→ Uninstalling $APP from $SITE (deletes its data)..."
if ! dc exec -T backend bench --site "$SITE" uninstall-app "$APP" --yes --no-backup; then
    echo "❌ Uninstall failed — the app may be half removed. Fix and re-run, or restore"
    echo "   the backup just taken: $TOOLS_DIR/backup/restore.sh --site $SITE <folder>"
    exit 1
fi
skip_app "$SITE" "$APP"
dc exec -T backend bench --site "$SITE" clear-cache >/dev/null 2>&1 || warn "cache clear failed"
ok "$APP uninstalled from $SITE (updates will not reinstall it there)"
