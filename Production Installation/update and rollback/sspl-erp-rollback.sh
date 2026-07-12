#!/bin/bash
set -e

cd /opt/sspl-erp

BACKUP_DIR="/opt/sspl-erp/image-backups"

echo "=============================="
echo " SSPL ERP Rollback - $(date)"
echo "=============================="

# Check if backup directory exists
if [ ! -d "$BACKUP_DIR" ]; then
    echo "❌ Backup directory not found: $BACKUP_DIR"
    exit 1
fi

# Find the latest backup
if [ -f "$BACKUP_DIR/latest_backup.txt" ]; then
    BACKUP_FILE=$(cat "$BACKUP_DIR/latest_backup.txt")
else
    # Fallback: find the most recent backup file
    BACKUP_FILE=$(ls -t "$BACKUP_DIR"/backup_*.tar 2>/dev/null | head -1)
fi

if [ -z "$BACKUP_FILE" ] || [ ! -f "$BACKUP_FILE" ]; then
    echo "❌ No backup file found!"
    echo ""
    echo "Available backups:"
    ls -lh "$BACKUP_DIR"/backup_*.tar 2>/dev/null || echo "   (none)"
    echo ""
    echo "To manually specify a backup, run:"
    echo "   BACKUP_FILE=/path/to/backup.tar $0"
    exit 1
fi

echo "→ Found backup: $BACKUP_FILE"
BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
BACKUP_DATE=$(stat -c %y "$BACKUP_FILE" | cut -d' ' -f1,2 | cut -d'.' -f1)
echo "   Size: $BACKUP_SIZE"
echo "   Created: $BACKUP_DATE"
echo ""

read -p "⚠️  This will restore the system to the backup state. Continue? (yes/no): " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
    echo "Rollback cancelled."
    exit 0
fi

echo ""
echo "→ Stopping all services..."
docker compose -f docker-compose.yml down

echo "→ Loading backup images..."
docker load -i "$BACKUP_FILE"

if [ $? -eq 0 ]; then
    echo "   ✓ Images restored successfully"
else
    echo "   ❌ Failed to restore images!"
    exit 1
fi

echo "→ Starting all services with restored images..."
docker compose -f docker-compose.yml up -d

echo "→ Waiting for services to be ready..."
sleep 15

echo "→ Fixing MariaDB user grants..."
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
fi

echo "→ Clearing cache..."
docker compose -f docker-compose.yml exec backend \
  bench --site 192.168.225.135 clear-cache

echo "✅ Rollback complete!"
docker compose -f docker-compose.yml exec backend bench version
