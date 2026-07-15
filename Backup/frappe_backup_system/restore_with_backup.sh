#!/bin/bash

# Safety backup + restore, as one job.
#
# This is what the admin panel's Restore button runs. The safety backup and
# the restore are deliberately welded into a single script so the backup
# cannot be skipped: under `set -e`, if the backup fails the restore never
# starts, and whatever is live right now stays recoverable.
#
# Usage: restore_with_backup.sh /path/to/backup/folder
#
# Env:
#   SSPL_SITE_NAME    site to restore into (answers frappe_restore.sh's prompt)
#   SSPL_SCRIPTS_DIR  where the frappe_*.sh scripts live (default /opt/scripts/v2)
#
# The MariaDB root password and the final yes/no confirmation are asked
# interactively by frappe_restore.sh. Run from the panel, they are typed into
# the job's terminal — they are never passed as arguments, stored, or logged.

set -e

SRC="$1"
INSTALL_DIR="${SSPL_SCRIPTS_DIR:-/opt/scripts/v2}"

if [ -z "$SRC" ]; then
    echo "Usage: $0 /path/to/backup/folder" >&2
    exit 1
fi
if [ ! -d "$SRC" ]; then
    echo "❌ Backup folder not found: $SRC" >&2
    exit 1
fi
if ! ls "$SRC"/*-database.sql.gz >/dev/null 2>&1; then
    echo "❌ No *-database.sql.gz in $SRC — that is not a restorable backup." >&2
    exit 1
fi

echo "════════════════════════════════════════"
echo " Step 1/2 — safety backup of the CURRENT system"
echo "════════════════════════════════════════"
echo "Backing up what is live now, before anything is overwritten."
echo "If this backup fails, the restore will NOT run."
echo ""
"$INSTALL_DIR/frappe_backup.sh"

echo ""
echo "════════════════════════════════════════"
echo " Step 2/2 — restore from $(basename "$SRC")"
echo "════════════════════════════════════════"
echo "The safety backup above is your way back if this restore goes wrong."
echo ""
exec "$INSTALL_DIR/frappe_restore.sh" "$SRC"
