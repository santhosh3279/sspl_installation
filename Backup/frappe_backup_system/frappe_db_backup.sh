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
RCLONE_REMOTE=""  # Optional: e.g. "gdrive:frappe-backups" — leave empty to skip cloud upload

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

# Clean old backups
find "$BACKUP_DIR" -name "*.sql.gz" -mtime +$RETENTION_DAYS -delete

# Optional: upload to cloud storage via rclone, into a db-only/ folder so the
# dumps don't mix with the full backups' timestamped directories. Same
# semantics as the full backup: a failed upload is a warning, not a failure.
if [ -n "$RCLONE_REMOTE" ]; then
    echo "Uploading backup to $RCLONE_REMOTE/db-only..."
    if rclone copy "$BACKUP_FILE" "$RCLONE_REMOTE/db-only"; then
        echo "Cloud upload completed"
    else
        echo "WARNING: Cloud upload failed — backup exists locally only"
    fi
fi

echo "=== Database backup completed: $BACKUP_FILE ($(du -h "$BACKUP_FILE" | cut -f1)) ==="
