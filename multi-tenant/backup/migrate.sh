#!/bin/bash
# Run 'bench migrate' on one site, several, or all of them.
#
# Usage: migrate.sh --site <host> [--site <host> ...] | --all
#
# Env:
#   SSPL_MIGRATE_SKIP_FAILING=yes   add --skip-failing (opt-in, see below)
#
# This is the step the restore and the update both run, and the one to re-run
# by hand when a migration failed: the data is in place, only the patches did
# not finish — restoring again just reaches the same failing patch.
#
# Sites are migrated one after another; a failure does not stop the rest.
# Exits 0 only if every site migrated. Prints one line per site at the end.
set -uo pipefail
source "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"

parse_site_args "$@"
[ ${#REST[@]} -eq 0 ] || err "unknown argument(s): ${REST[*]}"
take_lock

# --skip-failing turns a failed patch into a warning and reports success with
# the database part-patched. Here because after a known-broken upstream patch
# it is sometimes the only way forward — never the default.
ARGS=()
if [ "${SSPL_MIGRATE_SKIP_FAILING:-}" = yes ]; then
    ARGS+=(--skip-failing)
    echo "⚠ --skip-failing is ON: failing patches are skipped and reported as success."
fi

FAILED=()
for s in "${SITES[@]}"; do
    echo ""
    echo "→ Migrating $s..."
    if dc exec -T backend bench --site "$s" migrate ${ARGS[@]+"${ARGS[@]}"}; then
        ok "$s migrated"
    else
        echo "   ❌ $s: migration FAILED"
        FAILED+=("$s")
    fi
done

echo ""
echo "Migration summary:"
for s in "${SITES[@]}"; do
    if printf '%s\n' "${FAILED[@]+"${FAILED[@]}"}" | grep -qx -- "$s"; then
        echo "   ❌ $s"
    else
        echo "   ✓ $s"
    fi
done
[ ${#FAILED[@]} -eq 0 ] && exit 0

SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cat >&2 <<EOF

❌ Migration failed on: ${FAILED[*]}

   The data is in place; the patching did not finish. Restoring again only
   gets you back to this same point — the database is not what is broken,
   the app code in the image is (the traceback above names the app/patch).

   Once the app is fixed, re-run just this step for each failed site:
     sudo $SELF --site <host>

   If the patch is known-broken and you accept a part-patched database:
     sudo SSPL_MIGRATE_SKIP_FAILING=yes $SELF --site <host>
EOF
exit 1
