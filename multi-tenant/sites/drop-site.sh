#!/bin/bash
# Remove a tenant site: full backup first, then drop its database and move
# its folder to the bench's archived_sites/.
#
# Usage: drop-site.sh <host> [--yes]
#
# Without --yes you must type the site name to confirm. The backup must
# succeed or nothing is dropped. The site's backups stay on disk (and in the
# cloud) after it is gone — the --all backup jobs only prune sites that exist,
# so delete $BACKUP_ROOT/sites/<host>/ by hand once you no longer need them.
set -euo pipefail
source "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"

SITE="${1:-}"
[ -n "$SITE" ] || { echo "Usage: $0 <host> [--yes]" >&2; exit 2; }
YES=no
[ "${2:-}" = "--yes" ] && YES=yes

require_site "$SITE"
take_lock

echo "=============================="
echo " Drop site $SITE - $(date)"
echo "=============================="

if [ "$YES" != yes ]; then
    [ -t 0 ] || err "No terminal to confirm on — pass --yes"
    echo "This DELETES the database of $SITE. Users of $SITE lose access."
    read -rp "Type the site name to confirm: " CONFIRM
    [ "$CONFIRM" = "$SITE" ] || { echo "Cancelled."; exit 0; }
fi

echo "→ Full backup of $SITE first..."
"$TOOLS_DIR/backup/backup.sh" --site "$SITE" || err "Backup failed — $SITE was NOT dropped."

ROOT_PW=$(db_root_password)
echo "→ Dropping $SITE..."
dc exec -T backend bench drop-site "$SITE" --root-password "$ROOT_PW" --no-backup --force
ok "$SITE dropped (folder moved to sites/archived_sites/)"

rm -f "$(skip_apps_file "$SITE")"

if ! is_ip "$SITE"; then
    echo "→ Removing $SITE from the containers' host list..."
    apply_tenant_hosts || warn "Host list not refreshed; harmless — re-run $TOOLS_DIR/sites/apply-hosts.sh"
fi

echo ""
echo "✅ $SITE removed. Its backups remain in $(site_backup_dir "$SITE")/"
echo "   Remove its DNS entry too."
