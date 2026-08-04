#!/bin/bash
set -e

cd /opt/sspl-erp
source "$(dirname "$0")/sspl-erp-common.sh"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/backup_$TIMESTAMP.tar"
RCLONE_REMOTE=""  # Optional cloud destinations — empty skips the upload entirely.
                  # One:  "gdrive:frappe-backups"
                  # Many: "gdrive:frappe-backups mega:frappe-backups"
                  # All:  "*:frappe-backups"  (every remote rclone knows)
CLEAR_CLOUD_TRASH=yes # When the remote is too full for the snapshot, permanently
                      # delete old backups out of its trash to make room
MEGA_HARD_DELETE=yes  # On Mega only: prune permanently instead of binning.
                      # Set to 'no' to keep Mega's rubbish bin as an undo path,
                      # at the cost of it holding every pruned snapshot forever
# Installed by the backup setup, not by this one — hence the absolute path.
TRASH_CLEANUP="/opt/scripts/v2/rclone_trash_cleanup.sh"

trap 'echo ""; echo "❌ Update failed!"; echo "   Services may be in a partial state."; echo "   To roll back images: /opt/sspl-erp/v2/sspl-erp-rollback.sh"; echo "   To restore data:      sudo /opt/scripts/v2/frappe_restore.sh <backup-folder>"' ERR

echo "=============================="
echo " SSPL ERP Update - $(date)"
echo "=============================="

# Run Frappe backup first
echo "→ Running Frappe backup..."
if sudo /opt/scripts/v2/frappe_backup.sh; then
    echo "   ✓ Frappe backup completed successfully"
else
    echo "   ⚠ Frappe backup failed!"
    read -p "   Continue with update anyway? (yes/no): " CONTINUE
    if [ "$CONTINUE" != "yes" ]; then
        echo "Update cancelled."
        exit 1
    fi
fi

# Create backup directory if it doesn't exist
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

echo "→ Backing up current Docker images..."
# Get list of images used by the compose file
IMAGES=$(docker compose -f "$COMPOSE_FILE" config | grep 'image:' | awk '{print $2}' | sort -u)

# Save current images to tar file
if [ -n "$IMAGES" ]; then
    echo "   Images to backup:"
    echo "$IMAGES" | while read img; do echo "   - $img"; done

    docker save -o "$BACKUP_FILE" $IMAGES

    if [ -f "$BACKUP_FILE" ]; then
        BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
        echo "   ✓ Backup created: $BACKUP_FILE ($BACKUP_SIZE)"
        echo "$BACKUP_FILE" > "$BACKUP_DIR/latest_backup.txt"
    else
        echo "   ⚠ Backup failed, continuing anyway..."
    fi
else
    echo "   ⚠ No images found to backup"
fi

echo "→ Stopping all services..."
docker compose -f "$COMPOSE_FILE" down

echo "→ Cleaning up unused Docker resources..."
docker system prune -f

echo "→ Pulling latest image..."
docker compose -f "$COMPOSE_FILE" pull

echo "→ Starting all services..."
docker compose -f "$COMPOSE_FILE" up -d

wait_for_services
fix_db_grants

echo "→ Running migrations..."
docker compose -f "$COMPOSE_FILE" exec backend \
  bench --site "$SITE_NAME" migrate

echo "→ Clearing cache..."
docker compose -f "$COMPOSE_FILE" exec backend \
  bench --site "$SITE_NAME" clear-cache

echo "✅ Update complete!"
docker compose -f "$COMPOSE_FILE" exec backend bench version

echo ""
echo "📦 Backup Information:"
echo "   Backup file: $BACKUP_FILE"

# Expand RCLONE_REMOTE into the destinations to upload to, in RCLONE_TARGETS:
#
#   ""                            no upload
#   "gdrive:frappe-backups"       that one
#   "gdrive:backups mega:backups" both, independently
#   "*:backups"                   every remote 'rclone listremotes' reports
#
# The list is space-separated, so a folder name may not contain spaces.
rclone_expand_targets() {
    local spec="$1" folder r
    RCLONE_TARGETS=()
    case "$spec" in
        "") return 0 ;;
        '*:'*)
            folder="${spec#'*:'}"
            while IFS= read -r r; do
                # 'rclone listremotes' prints "name:" per line; anything else
                # is noise and must not become an upload destination.
                case "$r" in *:) ;; *) continue ;; esac
                RCLONE_TARGETS+=("${r}${folder}")
            done < <(rclone listremotes </dev/null 2>/dev/null)
            ;;
        *)
            # Deliberate word split, with globbing off so an unexpanded '*'
            # cannot turn local filenames into upload destinations.
            set -f
            RCLONE_TARGETS=($spec)
            set +f
            ;;
    esac
}

# The prune flag one remote needs, printed on stdout ('' for most backends).
#
# Mega bins its deletes and rclone can only empty that bin wholesale
# ('cleanup'), never part of it — so on Mega snapshots are deleted permanently
# and the bin never fills. Drive keeps its trash, where rclone_trash_cleanup.sh
# can reclaim just what an upload needs. Snapshots are the biggest thing this
# stack uploads, so a bin full of them is the fastest way to a wedged remote.
#
# stdin is closed: a password-protected rclone config would prompt for it.
mega_prune_flag() {
    case "$1" in
        *:*) ;;
        *) return 0 ;;    # not a remote
    esac
    case "$1" in
        :*) return 0 ;;   # ':backend:...' connection string, no named remote
    esac
    [ "$MEGA_HARD_DELETE" = "yes" ] || return 0
    if [ "$(rclone config show "${1%%:*}" </dev/null 2>/dev/null \
            | grep -oP '^\s*type\s*=\s*\K\S+' | head -1)" = "mega" ]; then
        echo "--mega-hard-delete"
    fi
}

RCLONE_TARGETS=()
declare -A PRUNE_FLAG_FOR=()
if [ -n "$RCLONE_REMOTE" ]; then
    rclone_expand_targets "$RCLONE_REMOTE"
    # Worked out once per remote, not once per deleted snapshot below.
    for REMOTE in "${RCLONE_TARGETS[@]}"; do
        PRUNE_FLAG_FOR["$REMOTE"]=$(mega_prune_flag "$REMOTE")
        if [ -n "${PRUNE_FLAG_FOR[$REMOTE]}" ]; then
            echo "   Mega remote $REMOTE: pruned snapshots are deleted permanently, not binned"
        fi
    done
fi

# Optional: copy the image snapshot to cloud storage via rclone. Done after
# the update, not before it, so a multi-gigabyte upload never extends the
# downtime window. Same semantics as the backup scripts: a failed upload is
# a warning, not a failure, and each destination stands on its own.
if [ ${#RCLONE_TARGETS[@]} -gt 0 ] && [ -f "$BACKUP_FILE" ]; then
    echo ""
    echo "→ Cloud destinations: ${RCLONE_TARGETS[*]}"
    for REMOTE in "${RCLONE_TARGETS[@]}"; do
        # Snapshots are multi-gigabyte, so this is the upload most likely to
        # hit a full remote. The retention below deletes with 'rclone
        # deletefile', which on Google Drive only moves old snapshots to the
        # account's trash, where they keep consuming quota — reclaim just
        # enough of it for this upload. Never fatal: the update has already
        # succeeded by this point.
        if [ "$CLEAR_CLOUD_TRASH" = "yes" ] && [ -x "$TRASH_CLEANUP" ]; then
            echo "→ Checking free space on $REMOTE..."
            "$TRASH_CLEANUP" --remote "$REMOTE" --need-path "$BACKUP_FILE" \
                || echo "   Proceeding with the upload anyway"
        fi

        echo "→ Uploading image snapshot to $REMOTE/image-snapshots..."
        if rclone copy "$BACKUP_FILE" "$REMOTE/image-snapshots"; then
            echo "   ✓ Cloud upload to $REMOTE completed"
        else
            echo "   ⚠ Cloud upload to $REMOTE failed"
        fi
    done
fi

# Automatically keep only the last 3 backups, on the remote too — snapshots
# are multi-gigabyte, so the cloud copy follows the same retention instead
# of growing without bound.
echo ""
echo "→ Cleaning old backups (keeping last 3)..."
OLD_BACKUPS=$(ls -t "$BACKUP_DIR"/backup_*.tar 2>/dev/null | tail -n +4)
if [ -n "$OLD_BACKUPS" ]; then
    echo "$OLD_BACKUPS" | while read backup; do
        rm -f "$backup"
        # Unquoted on purpose: the flag is either empty (contributing no
        # argument) or the single token --mega-hard-delete.
        for REMOTE in "${RCLONE_TARGETS[@]}"; do
            rclone deletefile ${PRUNE_FLAG_FOR[$REMOTE]} \
                "$REMOTE/image-snapshots/$(basename "$backup")" 2>/dev/null || true
        done
        echo "   ✓ Deleted: $(basename "$backup")"
    done
    echo "   ✓ Cleanup complete - 3 most recent backups retained"
else
    echo "   ✓ No old backups to clean"
fi

echo ""
echo "   To rollback, run: /opt/sspl-erp/v2/sspl-erp-rollback.sh"
