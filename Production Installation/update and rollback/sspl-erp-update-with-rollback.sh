#!/bin/bash
set -e

cd /opt/sspl-erp
source "$(dirname "$0")/sspl-erp-common.sh"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/backup_$TIMESTAMP.tar"

trap 'echo ""; echo "❌ Update failed!"; echo "   Services may be in a partial state."; echo "   To roll back images: ./sspl-erp-rollback.sh"; echo "   To restore data:      sudo /opt/scripts/frappe_restore.sh <backup-folder>"' ERR

echo "=============================="
echo " SSPL ERP Update - $(date)"
echo "=============================="

# Run Frappe backup first
echo "→ Running Frappe backup..."
if sudo /opt/scripts/frappe_backup.sh; then
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

# Automatically keep only the last 3 backups
echo ""
echo "→ Cleaning old backups (keeping last 3)..."
OLD_BACKUPS=$(ls -t "$BACKUP_DIR"/backup_*.tar 2>/dev/null | tail -n +4)
if [ -n "$OLD_BACKUPS" ]; then
    echo "$OLD_BACKUPS" | while read backup; do
        rm -f "$backup"
        echo "   ✓ Deleted: $(basename "$backup")"
    done
    echo "   ✓ Cleanup complete - 3 most recent backups retained"
else
    echo "   ✓ No old backups to clean"
fi

echo ""
echo "   To rollback, run: ./sspl-erp-rollback.sh"
