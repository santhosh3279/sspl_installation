#!/bin/bash

# Frappe Restore Script
# Usage: ./frappe_restore.sh /path/to/backup/folder
#
# SSPL_SITE_NAME can be set by a parent (the admin panel's Restore action)
# to answer the site prompt. The MariaDB root password and the final
# confirmation are always asked interactively and are never stored.

set -e

if [ -z "$1" ]; then
    echo "Usage: $0 /path/to/backup/folder"
    echo "Example: $0 /opt/backups/frappe/20250330_120000"
    exit 1
fi

BACKUP_DIR="$1"
COMPOSE_FILE="/opt/sspl-erp/docker-compose.yml"
SITE_NAME="your-site-name"  # Change this to your site name

if [ ! -d "$BACKUP_DIR" ]; then
    echo "Error: Backup directory not found: $BACKUP_DIR"
    exit 1
fi

echo "=== Starting Frappe Restore from $BACKUP_DIR ==="
if [ -n "$SSPL_SITE_NAME" ]; then
    SITE_NAME="$SSPL_SITE_NAME"
    echo "Site to restore into: $SITE_NAME"
else
    read -p "Enter site name [$SITE_NAME]: " INPUT_SITE
    SITE_NAME=${INPUT_SITE:-$SITE_NAME}
fi
read -sp "Enter MariaDB root password: " DB_ROOT_PASSWORD
echo ""

if [ -z "$SITE_NAME" ] || [ -z "$DB_ROOT_PASSWORD" ]; then
    echo "Error: Site name and password are required"
    exit 1
fi

echo ""
echo "WARNING: This will overwrite existing data on site: $SITE_NAME"
read -p "Continue? (yes/no): " confirm

if [ "$confirm" != "yes" ]; then
    echo "Restore cancelled."
    exit 0
fi

# ─────────────────────────────────────────────── take the site offline
# Nothing may touch the database while it is being replaced. Users can submit
# a document from a browser, and the workers/scheduler fire background jobs on
# their own — either lands writes in a database that is about to be overwritten
# or, worse, half-way through the replacement. backend/db/redis stay up: the
# restore runs through them.
OFFLINE_WANT="frontend websocket queue-short queue-long scheduler"
OFFLINE_SERVICES=""
HAVE=$(docker compose -f "$COMPOSE_FILE" config --services 2>/dev/null || true)
for s in $OFFLINE_WANT; do
    printf '%s\n' "$HAVE" | grep -qx "$s" && OFFLINE_SERVICES="$OFFLINE_SERVICES $s"
done
OFFLINE_SERVICES="${OFFLINE_SERVICES# }"

SITE_OFFLINE=""
RESTORE_DONE=""

bring_site_online() {
    [ -z "$SITE_OFFLINE" ] && return 0
    echo ""
    if [ -z "$RESTORE_DONE" ]; then
        echo "⚠ The restore did NOT finish. Bringing the site back online anyway —"
        echo "  locked out is worse than up — but the database may be INCOMPLETE."
        echo "  Check the site. If it is broken, restore again from the safety"
        echo "  backup taken just now in $(dirname "$BACKUP_DIR")/."
    else
        echo "Bringing the site back online..."
    fi
    if docker compose -f "$COMPOSE_FILE" start $OFFLINE_SERVICES; then
        SITE_OFFLINE=""
    else
        echo ""
        echo "❌ Could not start:$OFFLINE_SERVICES"
        echo "   The site is still OFFLINE. Start it by hand:"
        echo "   docker compose -f $COMPOSE_FILE start$OFFLINE_SERVICES"
    fi
}

# Whatever happens from here — a command failing under 'set -e', Ctrl-C at a
# prompt, or the SIGTERM from 'systemctl restart sspl-admin' (the documented
# abort path) — the site has to come back up. INT/TERM exit so EXIT fires.
trap bring_site_online EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ -n "$OFFLINE_SERVICES" ]; then
    echo ""
    echo "Taking the site offline:$OFFLINE_SERVICES"
    echo "Users will see the server as unavailable until the restore finishes."
    docker compose -f "$COMPOSE_FILE" stop $OFFLINE_SERVICES
    SITE_OFFLINE=yes
else
    echo ""
    echo "⚠ WARNING: no known services found in $COMPOSE_FILE, so the site was"
    echo "  NOT taken offline. Users and background jobs can write to the"
    echo "  database while it is being replaced. Continuing anyway."
fi

# Find backup files (newest of each type; archives are .tgz with --compress, .tar otherwise)
DB_BACKUP=$(find "$BACKUP_DIR" -name "*-database.sql.gz" -type f | sort | tail -1)
FILES_BACKUP=$(find "$BACKUP_DIR" \( -name "*-files.tar" -o -name "*-files.tgz" \) ! -name "*-private-files.*" -type f | sort | tail -1)
PRIVATE_BACKUP=$(find "$BACKUP_DIR" \( -name "*-private-files.tar" -o -name "*-private-files.tgz" \) -type f | sort | tail -1)

if [ -z "$DB_BACKUP" ]; then
    echo "Error: Database backup not found in $BACKUP_DIR"
    exit 1
fi

echo "Found database backup:      $(basename "$DB_BACKUP")"
[ -n "$FILES_BACKUP" ] && echo "Found public files backup:  $(basename "$FILES_BACKUP")"
[ -n "$PRIVATE_BACKUP" ] && echo "Found private files backup: $(basename "$PRIVATE_BACKUP")"

# 1. Copy backups to container
echo "Copying backups to container..."
CONTAINER_TMP="/tmp/restore"
docker compose -f "$COMPOSE_FILE" exec -T backend mkdir -p "$CONTAINER_TMP"

docker compose -f "$COMPOSE_FILE" cp "$DB_BACKUP" backend:"$CONTAINER_TMP/"
[ -n "$FILES_BACKUP" ] && docker compose -f "$COMPOSE_FILE" cp "$FILES_BACKUP" backend:"$CONTAINER_TMP/"
[ -n "$PRIVATE_BACKUP" ] && docker compose -f "$COMPOSE_FILE" cp "$PRIVATE_BACKUP" backend:"$CONTAINER_TMP/"

# 2. Restore database and files in a single bench restore call
echo "Restoring site..."
RESTORE_ARGS=(--mariadb-root-password "$DB_ROOT_PASSWORD")
[ -n "$FILES_BACKUP" ] && RESTORE_ARGS+=(--with-public-files "$CONTAINER_TMP/$(basename "$FILES_BACKUP")")
[ -n "$PRIVATE_BACKUP" ] && RESTORE_ARGS+=(--with-private-files "$CONTAINER_TMP/$(basename "$PRIVATE_BACKUP")")

docker compose -f "$COMPOSE_FILE" exec -T backend \
    bench --site "$SITE_NAME" restore \
    "${RESTORE_ARGS[@]}" \
    "$CONTAINER_TMP/$(basename "$DB_BACKUP")"

# 3. Run migrations in case app versions differ from the backup
echo "Running migrations..."
docker compose -f "$COMPOSE_FILE" exec -T backend \
    bench --site "$SITE_NAME" migrate

RESTORE_DONE=yes

# 4. Clean up temporary files
echo "Cleaning up..."
docker compose -f "$COMPOSE_FILE" exec -T backend rm -rf "$CONTAINER_TMP"

# 5. Restart the backend so it serves the restored database, then put the
#    user-facing services back. bring_site_online is idempotent, so the EXIT
#    trap does nothing once this has run.
echo "Restarting the backend..."
docker compose -f "$COMPOSE_FILE" restart backend
bring_site_online

echo "=== Restore completed successfully ==="
echo "Site should be available shortly."
