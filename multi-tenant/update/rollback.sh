#!/bin/bash
# Put the previous images back (from an update's snapshot). ALL tenants.
#
# Usage: rollback.sh [--yes]            (BACKUP_FILE=/path/backup_X.tar to pick one)
#
# Images only. Databases stay as they are: a site the update migrated now has
# a NEWER schema than the code you are rolling back to. If that site breaks,
# restore it from the backup the update took before it started.
set -euo pipefail
source "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"

take_lock
cd "$ERP_DIR"

if [ -n "${BACKUP_FILE:-}" ]; then
    :
elif [ -f "$IMAGE_BACKUP_DIR/latest_backup.txt" ]; then
    BACKUP_FILE=$(cat "$IMAGE_BACKUP_DIR/latest_backup.txt")
else
    BACKUP_FILE=$(ls -t "$IMAGE_BACKUP_DIR"/backup_*.tar 2>/dev/null | head -1 || true)
fi
if [ -z "$BACKUP_FILE" ] || [ ! -f "$BACKUP_FILE" ]; then
    echo "❌ No image snapshot found. Available:"
    ls -lh "$IMAGE_BACKUP_DIR"/backup_*.tar 2>/dev/null || echo "   (none)"
    echo "Pick one with: BACKUP_FILE=/path/to/backup_X.tar $0"
    exit 1
fi

echo "=== Rollback (all tenants) — $(date) ==="
echo "Snapshot: $BACKUP_FILE ($(du -h "$BACKUP_FILE" | cut -f1))"
if [ "${1:-}" != "--yes" ]; then
    [ -t 0 ] || err "No terminal to confirm on — pass --yes"
    read -rp "Roll EVERY site back to these images? (yes/no): " C
    [ "$C" = yes ] || { echo "Cancelled."; exit 0; }
fi

echo "→ Stopping the stack..."
dc down
echo "→ Loading snapshot..."
docker load -i "$BACKUP_FILE"
echo "→ Starting the stack..."
dc up -d
wait_for_services
for s in $(list_sites); do
    fix_db_grants "$s"
    dc exec -T backend bench --site "$s" clear-cache >/dev/null 2>&1 || warn "$s: cache clear failed"
done
echo "✅ Rollback complete."
dc exec -T backend bench version | sed 's/^/   /' || true
