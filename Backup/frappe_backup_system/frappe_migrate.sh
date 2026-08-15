#!/bin/bash

# Run 'bench migrate' against the deployed site, as a step of its own.
#
# The restore and the update both need this exact step with this exact failure
# handling, and it is also the thing you re-run by hand after a migration has
# failed: when migrate fails the database is already in place and correct, only
# the patches did not finish. Restoring the whole backup again just to reach
# another migrate attempt is wasted work — and on the update path it is not
# even the right database. So the step lives here once, and all three callers
# use it.
#
# Usage: frappe_migrate.sh [site-name]
#
# Env:
#   SSPL_SITE_NAME             site to migrate ($1 wins over it)
#   SSPL_COMPOSE_FILE          compose file (default /opt/sspl-erp/docker-compose.yml)
#   SSPL_MIGRATE_SKIP_FAILING  'yes' adds --skip-failing (opt-in, see below)
#
# Exits with bench's own status: 0 migrated, anything else it did not.

set -u

SITE_NAME="${1:-${SSPL_SITE_NAME:-your-site-name}}"
COMPOSE_FILE="${SSPL_COMPOSE_FILE:-/opt/sspl-erp/docker-compose.yml}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SELF="$SCRIPT_DIR/$(basename "$0")"

# The line above is what setup_frappe_backups.sh rewrites, but only at install
# time — a server that had the backup system before this script existed gets it
# from update_tooling.sh, which preserves settings it finds and cannot invent
# one for a file that was not there. So fall back to the site name configured
# in the backup script sitting next to this one, which every such server has.
# Without this, the no-argument call the guides document would fail on exactly
# the servers most likely to need it.
if [ "$SITE_NAME" = "your-site-name" ] && [ -r "$SCRIPT_DIR/frappe_backup.sh" ]; then
    # That line carries a trailing comment, so match the value, not the line.
    CONFIGURED=$(sed -n -e 's/^SITE_NAME="\([^"]*\)".*/\1/p' \
                        -e 's/^SITE_NAME=\([^"#[:space:]]*\).*/\1/p' \
        "$SCRIPT_DIR/frappe_backup.sh" | head -1)
    [ -n "$CONFIGURED" ] && SITE_NAME="$CONFIGURED"
fi

if [ -z "$SITE_NAME" ] || [ "$SITE_NAME" = "your-site-name" ]; then
    echo "❌ No site name. Pass one, or set SSPL_SITE_NAME." >&2
    exit 1
fi

# --skip-failing is deliberately not the default. It turns a failed patch into
# a warning and reports success, leaving the database part-patched — which is
# the same wrong state, just without anything telling you about it. It is here
# because after a known-broken patch it is sometimes the only way forward.
MIGRATE_ARGS=()
if [ "${SSPL_MIGRATE_SKIP_FAILING:-}" = "yes" ]; then
    MIGRATE_ARGS+=(--skip-failing)
    echo "⚠ --skip-failing is ON: a patch that fails will be skipped and this"
    echo "  will still report success, with the database only part-patched."
fi

echo "Running migrations on $SITE_NAME..."
if docker compose -f "$COMPOSE_FILE" exec -T backend \
        bench --site "$SITE_NAME" migrate \
        ${MIGRATE_ARGS[@]+"${MIGRATE_ARGS[@]}"}; then
    echo "✓ Migrations completed"
    exit 0
fi
STATUS=$?

cat >&2 <<EOF

❌ Migration failed on $SITE_NAME.

   The data is in place; what did not finish is the patching. Restoring again
   only puts you back at this same point — the database is not what is broken.

   What is broken is the app code in the running image against that data:
   read the traceback above for the app and the patch that failed.

   Once the app is fixed or updated, re-run just this step:
     sudo $SELF $SITE_NAME

   If the patch is known-broken upstream and you accept a part-patched
   database until it is fixed:
     sudo SSPL_MIGRATE_SKIP_FAILING=yes $SELF $SITE_NAME

   Or, if this bench has the command (check: 'bench --site $SITE_NAME --help'),
   mark that one patch as done, permanently, and migrate again:
     docker compose -f $COMPOSE_FILE exec backend \\
       bench --site $SITE_NAME bypass-patch <patch.module.name>
EOF
exit $STATUS
