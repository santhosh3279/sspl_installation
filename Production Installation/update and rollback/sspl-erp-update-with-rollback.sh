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

# Whatever ends this script — a failure under 'set -e', Ctrl-C, a kill — the
# site must not be left in the migration's offline window. Idempotent, and a
# no-op if the window was never opened, so the success path closing it inline
# makes this do nothing. INT/TERM exit so EXIT fires.
trap 'migrate_window_close' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

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
# Compose prints image references directly. Filtering empty lines also avoids
# treating an unset `image:` field as an image name to pull.
if ! IMAGES=$(docker compose -f "$COMPOSE_FILE" config --images); then
    echo "❌ Could not read Docker Compose images." >&2
    exit 1
fi
IMAGES=$(printf '%s\n' "$IMAGES" | awk 'NF' | sort -u)
[ -n "$IMAGES" ] || { echo "❌ No Docker Compose images found." >&2; exit 1; }

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

# The prune and the pull both happen with the site still up, so the download —
# by far the longest step — costs no downtime. Order inside this block matters:
#
#   - the snapshot above must come first. IMAGES are tags, and the rollback
#     does a bare 'docker load', so it depends on the tar carrying the old
#     images' RepoTags. Pull first and ':latest' already points at the new
#     image, so the snapshot would save the new images and the rollback would
#     be a no-op. (Nothing is lost by that ordering: the frappe backup and the
#     snapshot already run with the site up.)
#   - the prune must come before the pull, as it always has. It only reclaims
#     dangling images, which are the *previous* update's leftovers — the pull
#     is what orphans the current ones. Pruning after the pull would make each
#     update hold two generations of images plus the tar at once.
echo "→ Cleaning up unused Docker resources..."
# Cleanup, not a precondition — and it now runs while the site is up, so
# failing it here would abort a still-healthy system into the ERR trap's
# "partial state" message.
docker system prune -f || echo "   ⚠ Prune failed, continuing"

# Guarded rather than left to the ERR trap: until 'down' runs below, nothing
# has changed, so a registry failure here is not the partial state the trap
# describes.
STAGE_FILE="$BACKUP_DIR/staged-images.tsv"
USE_STAGED=no
if [ -s "$STAGE_FILE" ]; then
    USE_STAGED=yes
    # Every compose image must have a matching staged copy, and its original
    # tag must still point to the image that was current at download time.
    while IFS=$'\t' read -r image old_id stage new_id; do
        [ -n "$image" ] || { USE_STAGED=no; break; }
        [ "$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null)" = "$old_id" ] || USE_STAGED=no
        [ "$(docker image inspect --format '{{.Id}}' "$stage" 2>/dev/null)" = "$new_id" ] || USE_STAGED=no
    done < "$STAGE_FILE"
    diff -q <(printf '%s\n' "$IMAGES" | sort -u) \
        <(cut -f1 "$STAGE_FILE" | sort -u) >/dev/null || USE_STAGED=no
    [ "$(wc -l < "$STAGE_FILE")" -eq "$(printf '%s\n' "$IMAGES" | wc -l)" ] || USE_STAGED=no
fi
if [ "$USE_STAGED" = yes ]; then
    echo "→ Using previously downloaded images..."
    while IFS=$'\t' read -r image old_id stage new_id; do
        docker tag "$stage" "$image"
    done < "$STAGE_FILE"
    rm -f "$STAGE_FILE"
else
    [ ! -e "$STAGE_FILE" ] || echo "   Staged images are stale or incomplete; downloading fresh images."
    echo "→ Pulling latest images (services stay up during the download)..."
    PULL_FAILED=no
    while IFS= read -r image; do
        echo "→ $image"
        if ! python3 "$(dirname "$0")/sspl-erp-pull-progress.py" "$image"; then
            PULL_FAILED=yes
            break
        fi
    done <<< "$IMAGES"
    if [ "$PULL_FAILED" = yes ]; then
        echo "❌ Update stopped: the image pull failed."
        echo "   Nothing was changed — the site is still up on the current images."
        echo "   Check the network/registry and run the update again."
        exit 1
    fi

fi

echo "→ Stopping all services..."
docker compose -f "$COMPOSE_FILE" down

echo "→ Starting all services..."
docker compose -f "$COMPOSE_FILE" up -d

wait_for_services
fix_db_grants

# 'up -d' above started everything, including the frontend — so users are
# already on a site whose code is new and whose database is not yet migrated.
# Close that door before the schema starts moving.
migrate_window_open

if run_migrate; then
    # After the migration, so a new app installs against the schema the rest
    # of the stack has just been brought up to; before the cache clear, so
    # that covers the new app too.
    install_new_apps

    echo "→ Clearing cache..."
    docker compose -f "$COMPOSE_FILE" exec -T backend \
      bench --site "$SITE_NAME" clear-cache

    migrate_window_close
else
    # The site is serving new code against a part-migrated database. Do not
    # install apps or clear caches on top of that — put the site back up so
    # it is not dark, say plainly what state it is in, and stop.
    migrate_window_close
    echo ""
    echo "❌ Update stopped: the migration failed."
    echo "   The site is back online, running the NEW images against a"
    echo "   database whose patches did not finish. Expect errors."
    echo ""
    echo "   Two ways out:"
    echo "   1. Fix the app that failed (the traceback above names it), then"
    echo "      re-run only the migration — the images stay as they are:"
    echo "      sudo $MIGRATE_SCRIPT $SITE_NAME"
    echo "   2. Roll the images back:"
    echo "      /opt/sspl-erp/v2/sspl-erp-rollback.sh"
    echo "      Note this is NOT a clean undo. The schema sync ran before the"
    echo "      patch that failed, so the tables have already changed and the"
    echo "      rollback restores images only. If the old code cannot cope,"
    echo "      restore the data backup this update took at the start."
    echo ""
    echo "   New apps were NOT installed and the cache was NOT cleared."
    exit 1
fi

echo "✅ Update complete!"
docker compose -f "$COMPOSE_FILE" exec -T backend bench version

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

# Retention deletes Google Drive snapshots permanently; Mega does so when
# MEGA_HARD_DELETE=yes. Other backends keep their native delete behavior.
# stdin is closed: a password-protected rclone config would prompt for it.
prune_flag() {
    case "$1" in
        :*) return 0 ;;   # connection string, no named remote
        *:*) ;;
        *) return 0 ;;
    esac
    case "$(rclone config show "${1%%:*}" </dev/null 2>/dev/null \
            | grep -oP '^\s*type\s*=\s*\K\S+' | head -1)" in
        drive) echo "--drive-use-trash=false" ;;
        mega) [ "$MEGA_HARD_DELETE" = "yes" ] && echo "--mega-hard-delete" ;;
    esac
    return 0
}

RCLONE_TARGETS=()
declare -A PRUNE_FLAG_FOR=()
if [ -n "$RCLONE_REMOTE" ]; then
    rclone_expand_targets "$RCLONE_REMOTE"
    # Worked out once per remote, not once per deleted snapshot below.
    for REMOTE in "${RCLONE_TARGETS[@]}"; do
        PRUNE_FLAG_FOR["$REMOTE"]=$(prune_flag "$REMOTE")
        if [ -n "${PRUNE_FLAG_FOR[$REMOTE]}" ]; then
            echo "   Remote $REMOTE: pruned snapshots are deleted permanently"
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
        # hit a full remote. Old Drive trash from earlier runs may still
        # consume quota; reclaim just enough for this upload. Never fatal:
        # the update has already succeeded by this point.
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
        # Unquoted on purpose: the flag is empty or one rclone option.
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
