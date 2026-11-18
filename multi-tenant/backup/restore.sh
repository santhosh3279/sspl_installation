#!/bin/bash
# Restore ONE site from a full backup folder. Other tenants stay online.
#
# Usage: restore.sh --site <host> <backup-folder> [--yes] [--allow-other-site]
#
#   --yes                 skip the "type the site name" confirmation
#   --allow-other-site    restore a backup taken from a DIFFERENT site into
#                         <host> (moving a tenant). The backup's encryption key
#                         is copied into <host>'s site_config.json too, or
#                         encrypted fields (passwords, API keys) are unreadable.
#
# Takes no safety backup itself — restore-with-backup.sh does that and then
# runs this. The MariaDB root password is read from the stack .env.
#
# While it runs:
#   - <host> is in maintenance mode with its scheduler paused
#   - the shared job workers + scheduler are stopped (queued jobs of other
#     tenants wait in Redis and run afterwards); every tenant's web stays up
set -euo pipefail
source "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"

YES=no
OTHER=no
ARGS=()
for a in "$@"; do
    case "$a" in
        --yes) YES=yes ;;
        --allow-other-site) OTHER=yes ;;
        *) ARGS+=("$a") ;;
    esac
done
parse_site_args "${ARGS[@]+"${ARGS[@]}"}"
[ ${#SITES[@]} -eq 1 ] || err "restore takes exactly one --site"
SITE="${SITES[0]}"
[ ${#REST[@]} -eq 1 ] || err "Usage: $0 --site <host> <backup-folder> [--yes] [--allow-other-site]"
SRC="${REST[0]}"

take_lock

echo "=== Restore $SITE from $SRC — $(date) ==="

# ───────────────────────────── everything checkable, while the site is still up
require_site "$SITE"
[ -d "$SRC" ] || err "Backup folder not found: $SRC"

DB_BACKUP=$(find "$SRC" -maxdepth 1 -name '*-database.sql.gz' -type f | sort | tail -1)
[ -n "$DB_BACKUP" ] || err "No *-database.sql.gz in $SRC"
PREFIX="${DB_BACKUP%-database.sql.gz}"

# A database without its file archives leaves every attachment link dead.
matching_archive() {
    local a
    for a in "$PREFIX-$1.tgz" "$PREFIX-$1.tar"; do
        [ -s "$a" ] && { printf '%s\n' "$a"; return 0; }
    done
    return 1
}
FILES_BACKUP=$(matching_archive files) \
    || err "Missing the public files archive that belongs to $(basename "$DB_BACKUP")"
PRIVATE_BACKUP=$(matching_archive private-files) \
    || err "Missing the private files archive that belongs to $(basename "$DB_BACKUP")"
for a in "$FILES_BACKUP" "$PRIVATE_BACKUP"; do
    tar -tf "$a" >/dev/null 2>&1 || err "Damaged archive: $a"
done

# Which site was this backup taken from? The manifest says, and Frappe puts
# the site name (dots as underscores) in every backup file name.
FROM_SITE=""
if [ -f "$SRC/backup_manifest.txt" ]; then
    FROM_SITE=$(sed -n 's/^Site: //p' "$SRC/backup_manifest.txt" | head -1)
fi
STEM_OK=no
case "$(basename "$DB_BACKUP")" in
    *-"$(site_file_stem "$SITE")"-database.sql.gz) STEM_OK=yes ;;
esac
if { [ -n "$FROM_SITE" ] && [ "$FROM_SITE" != "$SITE" ]; } || { [ -z "$FROM_SITE" ] && [ "$STEM_OK" = no ]; }; then
    if [ "$OTHER" != yes ]; then
        echo "❌ This backup was taken from ${FROM_SITE:-another site} ($(basename "$DB_BACKUP")), not $SITE." >&2
        echo "   Restoring it here gives $SITE that tenant's data. If that is the point" >&2
        echo "   (moving a tenant), re-run with --allow-other-site." >&2
        exit 1
    fi
    echo "⚠ Restoring a backup of ${FROM_SITE:-another site} into $SITE (--allow-other-site)."
    [ -f "$SRC/site_config.json" ] || err "--allow-other-site needs the backup's site_config.json (for its encryption key)"
fi

ROOT_PW=$(db_root_password)

echo "Database:      $(basename "$DB_BACKUP")"
echo "Public files:  $(basename "$FILES_BACKUP")"
echo "Private files: $(basename "$PRIVATE_BACKUP")"
echo ""
echo "WARNING: this replaces ALL data of $SITE. Other sites are not touched."
if [ "$YES" != yes ]; then
    [ -t 0 ] || err "No terminal to confirm on — pass --yes"
    read -rp "Type the site name to continue: " CONFIRM
    [ "$CONFIRM" = "$SITE" ] || { echo "Restore cancelled."; exit 0; }
fi

# ─────────────────────────────────────────────────────── take the site offline
# Three outcomes, three different next steps for the operator:
#   neither set    died at or before 'bench restore' — the database may be
#                  partial; the safety backup is the way back
#   DB_RESTORED    data is in; only the migration failed — re-run the
#                  migration, do NOT restore again
#   RESTORE_DONE   clean
SITE_OFF=""; DB_RESTORED=""; RESTORE_DONE=""
CTMP="/tmp/sspl-restore-$$"

finish() {
    dc exec -T backend rm -rf "$CTMP" >/dev/null 2>&1 || true
    jobs_start
    [ -n "$SITE_OFF" ] || return 0
    echo ""
    if [ -n "$RESTORE_DONE" ]; then
        echo "Bringing $SITE back online..."
    elif [ -n "$DB_RESTORED" ]; then
        echo "⚠ The data was restored but the migration did NOT finish. Do not restore"
        echo "  again — fix the app, then re-run only the migration:"
        echo "  sudo $TOOLS_DIR/backup/migrate.sh --site $SITE"
        echo "  Bringing $SITE online anyway; expect errors until then."
    else
        echo "⚠ The restore did NOT finish. Bringing $SITE online anyway, but its database"
        echo "  may be INCOMPLETE. If it is broken, restore the safety backup in"
        echo "  $(site_backup_dir "$SITE")/full/ (the newest one, taken just before this)."
    fi
    if site_online "$SITE"; then SITE_OFF=""; else
        echo "❌ Could not take $SITE out of maintenance mode. By hand:"
        echo "   docker compose -f $COMPOSE_FILE exec backend bench --site $SITE set-maintenance-mode off"
        echo "   docker compose -f $COMPOSE_FILE exec backend bench --site $SITE set-config -p pause_scheduler 0"
    fi
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "→ Taking $SITE offline (maintenance mode, scheduler paused)..."
site_offline "$SITE"
SITE_OFF=yes
jobs_stop

# ─────────────────────────────────────────────────────────────── restore
echo "→ Copying the backup into the container..."
dc exec -T backend mkdir -p "$CTMP"
for f in "$DB_BACKUP" "$FILES_BACKUP" "$PRIVATE_BACKUP"; do
    dc cp "$f" "backend:$CTMP/" >/dev/null
done

if [ "$OTHER" = yes ]; then
    KEY=$(grep -oP '"encryption_key":\s*"\K[^"]+' "$SRC/site_config.json" || true)
    if [ -n "$KEY" ]; then
        dc exec -T backend bench --site "$SITE" set-config encryption_key "$KEY" >/dev/null
        echo "→ Encryption key taken over from the backup's site"
    else
        warn "The backup's site_config.json has no encryption_key — encrypted fields may be unreadable"
    fi
fi

echo "→ Restoring $SITE..."
dc exec -T backend bench --site "$SITE" restore \
    --db-root-password "$ROOT_PW" \
    --with-public-files "$CTMP/$(basename "$FILES_BACKUP")" \
    --with-private-files "$CTMP/$(basename "$PRIVATE_BACKUP")" \
    "$CTMP/$(basename "$DB_BACKUP")"
DB_RESTORED=yes

fix_db_grants "$SITE"

# Not under set -e: a failed patch must still reach the cleanup below.
if "$TOOLS_DIR/backup/migrate.sh" --site "$SITE"; then
    RESTORE_DONE=yes
fi
dc exec -T backend bench --site "$SITE" clear-cache >/dev/null 2>&1 || true

finish
trap - EXIT

if [ -z "$RESTORE_DONE" ]; then
    echo ""
    echo "=== Restore of $SITE finished with a FAILED migration ==="
    exit 1
fi
echo ""
echo "=== Restore of $SITE completed ==="
