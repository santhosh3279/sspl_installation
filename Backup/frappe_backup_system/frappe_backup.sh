#!/bin/bash

# Frappe Docker Backup Script
# Run this script via cron for automated backups

set -e
set -o pipefail

# Configuration
BACKUP_DIR="/opt/backups/frappe"
SITE_NAME="your-site-name"  # Change this to your site name
COMPOSE_FILE="/opt/sspl-erp/docker-compose.yml"
RETENTION_DAYS=30
LOCAL_KEEP_MIN=20 # Newest N on this server survive the cleanup however old they are
RCLONE_REMOTE=""  # Optional: e.g. "gdrive:frappe-backups" — leave empty to skip cloud upload
CLOUD_KEEP=10     # How many backups to keep on the remote (local keeps RETENTION_DAYS)

# Create backup directory if it doesn't exist (restricted: backups contain credentials)
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

# Timestamp for backup files
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

echo "=== Starting Frappe Backup at $(date) ==="

# 1. Trigger Frappe's built-in backup
echo "Running Frappe backup..."
docker compose -f "$COMPOSE_FILE" exec -T backend \
    bench --site "$SITE_NAME" backup \
    --with-files \
    --compress

# 2. Copy backups from container to host
echo "Copying backups to host..."
CONTAINER_BACKUP_DIR="/home/frappe/frappe-bench/sites/$SITE_NAME/private/backups"

# Create dated backup directory
DATED_BACKUP_DIR="$BACKUP_DIR/$TIMESTAMP"
mkdir -p "$DATED_BACKUP_DIR"

# Copy the newest container file matching any of the given globs.
# $1 optionally excludes matches (empty string = no exclusion).
copy_latest() {
    local exclude="$1"
    shift
    local globs="" p file
    for p in "$@"; do
        globs="$globs $CONTAINER_BACKUP_DIR/$p"
    done
    file=$(docker compose -f "$COMPOSE_FILE" exec -T backend \
        bash -c "ls -t $globs 2>/dev/null" | \
        { [ -n "$exclude" ] && grep -v -- "$exclude" || cat; } | head -1 | tr -d '\r')
    if [ -n "$file" ]; then
        docker compose -f "$COMPOSE_FILE" cp "backend:$file" "$DATED_BACKUP_DIR/"
        echo "  Copied: $(basename "$file")"
        return 0
    fi
    return 1
}

# Database backup is mandatory
if ! copy_latest "" "*-database.sql.gz"; then
    echo "ERROR: No database backup found in container"
    exit 1
fi

# Files backups: .tgz when --compress is used, .tar otherwise.
# The public-files glob also matches private-files, so exclude those explicitly.
copy_latest "-private-files." "*-files.tar" "*-files.tgz" || \
    echo "WARNING: No public files backup found"
copy_latest "" "*-private-files.tar" "*-private-files.tgz" || \
    echo "WARNING: No private files backup found"

# Copy site_config.json
docker compose -f "$COMPOSE_FILE" cp \
    backend:/home/frappe/frappe-bench/sites/$SITE_NAME/site_config.json \
    "$DATED_BACKUP_DIR/"

# 3. Backup docker-compose and configs
echo "Backing up configuration files..."
cp "$COMPOSE_FILE" "$DATED_BACKUP_DIR/"
cp -r /opt/sspl-erp/.env "$DATED_BACKUP_DIR/" 2>/dev/null || true
chmod -R go-rwx "$DATED_BACKUP_DIR"

# 4. Create a manifest file
cat > "$DATED_BACKUP_DIR/backup_manifest.txt" <<EOF
Backup Date: $(date)
Site: $SITE_NAME
Host: $(hostname)
Docker Compose Version: $(docker compose version)
Contents:
$(ls -lh "$DATED_BACKUP_DIR")
EOF

# 5. Clean up old backups: older than RETENTION_DAYS *and* outside the newest
# LOCAL_KEEP_MIN. The floor matters — with a plain age cleanup, a cron that
# stopped producing fresh backups for longer than the retention window empties
# the backup folder completely on its next successful run. This is the same
# floor the admin panel's manual cleanup applies.
#
# Only timestamped folders are candidates: db-only/ is a sibling directory
# under $BACKUP_DIR with its own retention, and must never be swept up here.
echo "Cleaning up old backups (keeping newest $LOCAL_KEEP_MIN, then $RETENTION_DAYS days)..."
OLD_LOCAL=$(find "$BACKUP_DIR" -maxdepth 1 -type d \
        -regextype posix-extended -regex '.*/[0-9]{8}_[0-9]{6}' -printf '%f\n' 2>/dev/null \
    | sort -r \
    | tail -n +$((LOCAL_KEEP_MIN + 1))) || true
if [ -n "$OLD_LOCAL" ]; then
    echo "$OLD_LOCAL" | while read -r folder; do
        # An empty name here would expand to $BACKUP_DIR itself
        [ -n "$folder" ] || continue
        # Age is mtime, the clock the old find -mtime cleanup used
        [ -n "$(find "$BACKUP_DIR/$folder" -maxdepth 0 -mtime +$RETENTION_DAYS 2>/dev/null)" ] || continue
        if rm -rf "${BACKUP_DIR:?}/$folder"; then
            echo "  Removed: $folder"
        else
            echo "  WARNING: could not remove: $folder"
        fi
    done
fi

# 6. Calculate backup size
BACKUP_SIZE=$(du -sh "$DATED_BACKUP_DIR" | cut -f1)
echo "=== Backup completed successfully ==="
echo "Backup location: $DATED_BACKUP_DIR"
echo "Backup size: $BACKUP_SIZE"

# 7. Optional: upload to cloud storage via rclone (see Rclone_Configuration_Guide)
if [ -n "$RCLONE_REMOTE" ]; then
    echo "Uploading backup to $RCLONE_REMOTE..."
    if rclone copy "$DATED_BACKUP_DIR" "$RCLONE_REMOTE/$TIMESTAMP"; then
        echo "Cloud upload completed"

        # Keep only the newest CLOUD_KEEP backups on the remote. The local
        # find -mtime retention above does not touch the cloud, so without
        # this the remote grows without bound.
        #
        # The remote is listed directly rather than deriving the deletes from
        # what was just removed locally (which is what the update script does
        # for image snapshots): a local backup that disappears any other way —
        # a manual rm, the backup manager — would otherwise orphan its cloud
        # copy forever and quietly turn keep-10 into keep-N.
        #
        # Only timestamped folders are candidates. db-only/ and
        # image-snapshots/ live in the same remote root and have their own
        # retention, so the pattern must stay exact.
        echo "Cleaning old cloud backups (keeping newest $CLOUD_KEEP)..."
        OLD_REMOTE=$(rclone lsf --dirs-only "$RCLONE_REMOTE" 2>/dev/null \
            | sed 's:/$::' \
            | grep -E '^[0-9]{8}_[0-9]{6}$' \
            | sort -r \
            | tail -n +$((CLOUD_KEEP + 1))) || true
        if [ -n "$OLD_REMOTE" ]; then
            echo "$OLD_REMOTE" | while read -r folder; do
                # An empty name here would purge the whole remote
                [ -n "$folder" ] || continue
                if rclone purge "$RCLONE_REMOTE/$folder" 2>/dev/null; then
                    echo "  Removed from cloud: $folder"
                else
                    echo "  WARNING: could not remove from cloud: $folder"
                fi
            done
        else
            echo "  Nothing to remove from cloud"
        fi
    else
        echo "WARNING: Cloud upload failed — backup exists locally only"
    fi
fi

echo "=== Backup finished at $(date) ==="
