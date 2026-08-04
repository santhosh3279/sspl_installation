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
RCLONE_REMOTE=""  # Optional: e.g. "gdrive:frappe-backups" — leave empty to skip cloud upload
CLOUD_KEEP=10     # How many dumps to keep on the remote (local keeps RETENTION_DAYS)
CLEAR_CLOUD_TRASH=yes # When the remote is too full for the upload, permanently
                      # delete old backups out of its trash to make room

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
if [ -n "$RCLONE_REMOTE" ]; then
    # Make room first if the remote is short: the cloud prune below deletes
    # with 'rclone deletefile', which on Google Drive only moves the old dumps
    # to the account's trash, where they keep consuming quota. Never fatal —
    # if the space cannot be freed, the upload still gets its attempt.
    TRASH_CLEANUP="$(dirname "$0")/rclone_trash_cleanup.sh"
    if [ "$CLEAR_CLOUD_TRASH" = "yes" ] && [ -x "$TRASH_CLEANUP" ]; then
        echo "Checking free space on $RCLONE_REMOTE..."
        "$TRASH_CLEANUP" --remote "$RCLONE_REMOTE" --need-path "$BACKUP_FILE" \
            || echo "  Proceeding with the upload anyway"
    fi

    echo "Uploading backup to $RCLONE_REMOTE/db-only..."
    if rclone copy "$BACKUP_FILE" "$RCLONE_REMOTE/db-only"; then
        echo "Cloud upload completed"

        # Keep only the newest CLOUD_KEEP dumps on the remote: the local
        # find -mtime retention above does not touch the cloud. The remote is
        # listed directly, so a dump removed locally by any other means cannot
        # orphan its cloud copy. Names start with the timestamp, so a reverse
        # lexicographic sort is newest-first.
        echo "Cleaning old cloud dumps (keeping newest $CLOUD_KEEP)..."
        OLD_REMOTE=$(rclone lsf --files-only "$RCLONE_REMOTE/db-only" 2>/dev/null \
            | grep -E '^[0-9]{8}_[0-9]{6}_.*\.sql\.gz$' \
            | sort -r \
            | tail -n +$((CLOUD_KEEP + 1))) || true
        if [ -n "$OLD_REMOTE" ]; then
            echo "$OLD_REMOTE" | while read -r dump; do
                [ -n "$dump" ] || continue
                if rclone deletefile "$RCLONE_REMOTE/db-only/$dump" 2>/dev/null; then
                    echo "  Removed from cloud: $dump"
                else
                    echo "  WARNING: could not remove from cloud: $dump"
                fi
            done
        else
            echo "  Nothing to remove from cloud"
        fi
    else
        echo "WARNING: Cloud upload failed — backup exists locally only"
    fi
fi

echo "=== Database backup completed: $BACKUP_FILE ($(du -h "$BACKUP_FILE" | cut -f1)) ==="
