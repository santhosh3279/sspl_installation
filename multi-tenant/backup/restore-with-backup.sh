#!/bin/bash
# Safety backup of the site, THEN restore it — one job, so the backup cannot
# be skipped: if it fails, the restore never starts.
#
# Usage: restore-with-backup.sh --site <host> <backup-folder> [--yes] [--allow-other-site]
set -euo pipefail
source "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"

SITE=""
prev=""
for a in "$@"; do
    [ "$prev" = "--site" ] && SITE="$a"
    case "$a" in --site=*) SITE="${a#--site=}" ;; esac
    prev="$a"
done
[ -n "$SITE" ] || err "Usage: $0 --site <host> <backup-folder> [--yes] [--allow-other-site]"
require_site "$SITE"
take_lock

echo "════════════════════════════════════════"
echo " Step 1/2 — safety backup of $SITE as it is now"
echo "════════════════════════════════════════"
"$TOOLS_DIR/backup/backup.sh" --site "$SITE" || err "Safety backup failed — restore NOT started."

echo ""
echo "════════════════════════════════════════"
echo " Step 2/2 — restore"
echo "════════════════════════════════════════"
exec "$TOOLS_DIR/backup/restore.sh" "$@"
