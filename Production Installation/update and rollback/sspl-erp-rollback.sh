#!/bin/bash
set -e

cd /opt/sspl-erp
source "$(dirname "$0")/sspl-erp-common.sh"

echo "=============================="
echo " SSPL ERP Rollback - $(date)"
echo "=============================="

# Check if backup directory exists
if [ ! -d "$BACKUP_DIR" ]; then
    echo "❌ Backup directory not found: $BACKUP_DIR"
    exit 1
fi

# Find the backup to restore: explicit BACKUP_FILE env var wins,
# then latest_backup.txt, then the most recent file on disk
if [ -n "${BACKUP_FILE:-}" ]; then
    echo "→ Using backup specified via BACKUP_FILE"
elif [ -f "$BACKUP_DIR/latest_backup.txt" ]; then
    BACKUP_FILE=$(cat "$BACKUP_DIR/latest_backup.txt")
else
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
docker compose -f "$COMPOSE_FILE" down

echo "→ Loading backup images..."
docker load -i "$BACKUP_FILE"
echo "   ✓ Images restored successfully"

echo "→ Starting all services with restored images..."
docker compose -f "$COMPOSE_FILE" up -d

wait_for_services
fix_db_grants

echo "→ Clearing cache..."
docker compose -f "$COMPOSE_FILE" exec backend \
  bench --site "$SITE_NAME" clear-cache

echo "✅ Rollback complete!"
docker compose -f "$COMPOSE_FILE" exec backend bench version
