#!/bin/bash

# Direct MariaDB Backup Script
# Useful for quick database-only backups

set -e
set -o pipefail

BACKUP_DIR="/opt/backups/frappe/db-only"
SITE_NAME="your-site-name"  # Change this to your site name
COMPOSE_FILE="/opt/sspl-erp/docker-compose.yml"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RETENTION_DAYS=14
LOCAL_KEEP_MIN=20 # Newest N on this server survive the cleanup however old they are
RCLONE_REMOTE=""  # Optional cloud destinations — empty skips the upload entirely.
                  # One:  "gdrive:frappe-backups"
                  # Many: "gdrive:frappe-backups mega:frappe-backups"
                  # All:  "*:frappe-backups"  (every remote rclone knows, now
                  #       and in future — each one receives the whole database)
CLOUD_KEEP=10     # How many dumps to keep on each remote (local keeps RETENTION_DAYS)
CLEAR_CLOUD_TRASH=yes # When the remote is too full for the upload, permanently
                      # delete old backups out of its trash to make room
MEGA_HARD_DELETE=yes  # On Mega only: prune permanently instead of binning.
                      # Set to 'no' to keep Mega's rubbish bin as an undo path,
                      # at the cost of it holding every pruned dump forever

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

echo "=== Starting Database Backup at $(date) ==="

# Get the site's database credentials from site_config.json
SITE_CONFIG=$(docker compose -f "$COMPOSE_FILE" exec -T backend \
    bash -c "cat ~/frappe-bench/sites/${SITE_NAME}/site_config.json")

DB_NAME=$(echo "$SITE_CONFIG" | grep -oP '"db_name":\s*"\K[^"]+')
DB_PASSWORD=$(echo "$SITE_CONFIG" | grep -oP '"db_password":\s*"\K[^"]+')

if [ -z "$DB_NAME" ] || [ -z "$DB_PASSWORD" ]; then
    echo "ERROR: Could not read db_name/db_password from site_config.json for site $SITE_NAME"
    exit 1
fi

BACKUP_FILE="$BACKUP_DIR/${TIMESTAMP}_${DB_NAME}.sql.gz"

# Dump database as the site's own DB user; password via env, not command line
docker compose -f "$COMPOSE_FILE" exec -T -e MYSQL_PWD="$DB_PASSWORD" db \
    mariadb-dump -u "$DB_NAME" "$DB_NAME" \
    --single-transaction \
    --quick | gzip > "$BACKUP_FILE"

# Sanity check: a real ERPNext dump is never this small
if [ "$(stat -c %s "$BACKUP_FILE")" -lt 10240 ]; then
    echo "ERROR: Dump looks too small ($(du -h "$BACKUP_FILE" | cut -f1)) — treating as failed"
    exit 1
fi

# Clean old dumps: older than RETENTION_DAYS *and* outside the newest
# LOCAL_KEEP_MIN. Without the floor, a cron that stalled for longer than the
# retention window wipes the folder on its next successful run.
echo "Cleaning up old dumps (keeping newest $LOCAL_KEEP_MIN, then $RETENTION_DAYS days)..."
# Timestamped dumps only, same pattern the cloud prune uses: a hand-made
# manual.sql.gz would otherwise sort to the front and eat a protected slot.
OLD_LOCAL=$(find "$BACKUP_DIR" -maxdepth 1 -type f \
        -regextype posix-extended -regex '.*/[0-9]{8}_[0-9]{6}_.*\.sql\.gz' -printf '%f\n' 2>/dev/null \
    | sort -r \
    | tail -n +$((LOCAL_KEEP_MIN + 1))) || true
if [ -n "$OLD_LOCAL" ]; then
    echo "$OLD_LOCAL" | while read -r dump; do
        [ -n "$dump" ] || continue
        [ -n "$(find "$BACKUP_DIR/$dump" -maxdepth 0 -mtime +$RETENTION_DAYS 2>/dev/null)" ] || continue
        if rm -f "${BACKUP_DIR:?}/$dump"; then
            echo "  Removed: $dump"
        else
            echo "  WARNING: could not remove: $dump"
        fi
    done
fi

# Optional: upload to cloud storage via rclone, into a db-only/ folder so the
# dumps don't mix with the full backups' timestamped directories. Same
# semantics as the full backup: a failed upload is a warning, not a failure.
# Expand RCLONE_REMOTE into the destinations to upload to, in RCLONE_TARGETS:
#
#   ""                            no upload
#   "gdrive:frappe-backups"       that one
#   "gdrive:backups mega:backups" both, independently
#   "*:backups"                   every remote 'rclone listremotes' reports
#
# The list is space-separated, so a folder name may not contain spaces.
#
# '*:' picks up remotes added later, automatically and silently. Dumps are the
# whole database, so every remote in root's rclone config receives it. Naming
# the destinations explicitly is the form that cannot surprise you.
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

# Upload this dump to one remote and apply that remote's cloud retention.
# Destinations are independent: a failure is a warning, and a remote is pruned
# only when its own upload succeeded.
upload_and_prune() {
    local REMOTE="$1"
    local PRUNE_FLAGS=() OLD_REMOTE

    # How this remote's prune deletes. Mega bins its deletes and rclone can
    # only empty that bin wholesale ('cleanup'), never part of it — so on Mega
    # the prune deletes permanently and the bin never fills. Drive keeps its
    # trash, where rclone_trash_cleanup.sh can reclaim just what an upload
    # needs. See BACKUP_GUIDE.md, "Reclaiming trashed space".
    #
    # stdin is closed: a password-protected rclone config would prompt for it.
    case "$REMOTE" in
        :*) ;;   # ':backend:...' connection string — no named remote to look up
        *:*)
            if [ "$MEGA_HARD_DELETE" = "yes" ] && [ "$(rclone config show \
                    "${REMOTE%%:*}" </dev/null 2>/dev/null \
                    | grep -oP '^\s*type\s*=\s*\K\S+' | head -1)" = "mega" ]; then
                PRUNE_FLAGS=(--mega-hard-delete)
                echo "  Mega remote: pruned dumps are deleted permanently, not binned"
            fi
            ;;
    esac

    # Make room first if this remote is short: the prune below deletes with
    # 'rclone deletefile', which on Google Drive only moves the old dumps to
    # the account's trash, where they keep consuming quota. Never fatal — if
    # the space cannot be freed, the upload still gets its attempt.
    local TRASH_CLEANUP="$(dirname "$0")/rclone_trash_cleanup.sh"
    if [ "$CLEAR_CLOUD_TRASH" = "yes" ] && [ -x "$TRASH_CLEANUP" ]; then
        echo "  Checking free space on $REMOTE..."
        "$TRASH_CLEANUP" --remote "$REMOTE" --need-path "$BACKUP_FILE" \
            || echo "  Proceeding with the upload anyway"
    fi

    echo "  Uploading dump to $REMOTE/db-only..."
    if ! rclone copy "$BACKUP_FILE" "$REMOTE/db-only"; then
        echo "  WARNING: upload to $REMOTE failed — not pruning it"
        return 1
    fi
    echo "  Cloud upload to $REMOTE completed"

    # Keep only the newest CLOUD_KEEP dumps on this remote: the local
    # find -mtime retention above does not touch the cloud. The remote is
    # listed directly, so a dump removed locally by any other means cannot
    # orphan its cloud copy. Names start with the timestamp, so a reverse
    # lexicographic sort is newest-first.
    echo "  Cleaning old cloud dumps on $REMOTE (keeping newest $CLOUD_KEEP)..."
    OLD_REMOTE=$(rclone lsf --files-only "$REMOTE/db-only" 2>/dev/null \
        | grep -E '^[0-9]{8}_[0-9]{6}_.*\.sql\.gz$' \
        | sort -r \
        | tail -n +$((CLOUD_KEEP + 1))) || true
    if [ -n "$OLD_REMOTE" ]; then
        echo "$OLD_REMOTE" | while read -r dump; do
            [ -n "$dump" ] || continue
            if rclone deletefile "${PRUNE_FLAGS[@]}" "$REMOTE/db-only/$dump" 2>/dev/null; then
                echo "    Removed from cloud: $dump"
            else
                echo "    WARNING: could not remove from cloud: $dump"
            fi
        done
    else
        echo "    Nothing to remove from cloud"
    fi
    return 0
}

if [ -n "$RCLONE_REMOTE" ]; then
    rclone_expand_targets "$RCLONE_REMOTE"
    if [ ${#RCLONE_TARGETS[@]} -eq 0 ]; then
        echo "WARNING: RCLONE_REMOTE is '$RCLONE_REMOTE' but names no usable remote"
        echo "         — this dump was NOT uploaded anywhere"
    else
        echo "Cloud destinations: ${RCLONE_TARGETS[*]}"
        CLOUD_OK=0
        for REMOTE in "${RCLONE_TARGETS[@]}"; do
            if upload_and_prune "$REMOTE"; then
                CLOUD_OK=$((CLOUD_OK + 1))
            fi
        done
        if [ "$CLOUD_OK" -eq 0 ]; then
            echo "WARNING: no cloud destination accepted this dump — it exists locally only"
        else
            echo "Cloud upload: $CLOUD_OK of ${#RCLONE_TARGETS[@]} destination(s) succeeded"
        fi
    fi
fi

echo "=== Database backup completed: $BACKUP_FILE ($(du -h "$BACKUP_FILE" | cut -f1)) ==="
