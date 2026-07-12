#!/bin/bash
set -e

cd /opt/sspl-erp

BACKUP_DIR="/opt/sspl-erp/image-backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/backup_$TIMESTAMP.tar"

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

echo "→ Backing up current Docker images..."
# Get list of images used by the compose file
IMAGES=$(docker compose -f docker-compose.yml config | grep 'image:' | awk '{print $2}' | sort -u)

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
docker compose -f docker-compose.yml down

echo "→ Cleaning up unused Docker resources..."
docker system prune -f

echo "→ Pulling latest image..."
docker compose -f docker-compose.yml pull

echo "→ Starting all services..."
docker compose -f docker-compose.yml up -d

echo "→ Waiting for services to be ready..."
sleep 15

echo "→ Fixing MariaDB user grants (handling IP changes)..."
MARIADB_ROOT_PASSWORD=$(grep MARIADB_ROOT_PASSWORD .env | cut -d '=' -f2)
SITE_NAME="192.168.225.135"

# Get DB credentials from site_config.json
docker compose -f docker-compose.yml exec backend bash -c "cat ~/frappe-bench/sites/${SITE_NAME}/site_config.json" > /tmp/site_config.json
DB_NAME=$(grep -oP '"db_name":\s*"\K[^"]+' /tmp/site_config.json)
DB_PASS=$(grep -oP '"db_password":\s*"\K[^"]+' /tmp/site_config.json)
rm /tmp/site_config.json

if [ -n "$DB_NAME" ] && [ -n "$DB_PASS" ]; then
    echo "   Granting access for user: $DB_NAME on database: $DB_NAME"
    docker compose -f docker-compose.yml exec db mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" -e "
        DROP USER IF EXISTS '${DB_NAME}'@'%';
        CREATE USER '${DB_NAME}'@'%' IDENTIFIED BY '${DB_PASS}';
        GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_NAME}'@'%';
        FLUSH PRIVILEGES;
    " 2>/dev/null && echo "   ✓ Database grants updated" || echo "   ⚠ Grant update failed (may already be correct)"
else
    echo "   ⚠ Could not extract DB credentials, skipping grant fix"
fi

echo "→ Running migrations..."
docker compose -f docker-compose.yml exec backend \
  bench --site 192.168.225.135 migrate

echo "→ Clearing cache..."
docker compose -f docker-compose.yml exec backend \
  bench --site 192.168.225.135 clear-cache

echo "✅ Update complete!"
docker compose -f docker-compose.yml exec backend bench version

echo ""
echo "📦 Backup Information:"
echo "   Backup file: $BACKUP_FILE"

# Automatically keep only the last 3 backups
echo ""
echo "→ Cleaning old backups (keeping last 3)..."
BACKUPS=$(ls -t "$BACKUP_DIR"/backup_*.tar 2>/dev/null)
TOTAL=$(echo "$BACKUPS" | grep -c . || echo 0)

if [ "$TOTAL" -gt 3 ]; then
    TO_DELETE=$(echo "$BACKUPS" | tail -n +4)
    DELETE_COUNT=$(echo "$TO_DELETE" | grep -c . || echo 0)
    
    echo "   Found $TOTAL backups, deleting $DELETE_COUNT old backup(s)..."
    echo "$TO_DELETE" | while read backup; do
        rm -f "$backup"
        echo "   ✓ Deleted: $(basename "$backup")"
    done
    echo "   ✓ Cleanup complete - 3 most recent backups retained"
else
    echo "   ✓ Only $TOTAL backup(s) exist - no cleanup needed"
fi

echo ""
echo "   To rollback, run: ./sspl-erp-rollback.sh"
