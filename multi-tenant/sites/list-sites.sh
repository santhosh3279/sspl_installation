#!/bin/bash
# List the tenant sites on this bench.
#
# Usage: list-sites.sh [--apps]     --apps also shows each site's installed apps
set -euo pipefail
source "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"

dc exec -T backend true >/dev/null 2>&1 || err "The backend is not running."
mapfile -t ALL < <(list_sites)
if [ ${#ALL[@]} -eq 0 ]; then
    echo "No sites yet. Create one: $TOOLS_DIR/sites/new-site.sh <host>"
    exit 0
fi
for s in "${ALL[@]}"; do
    if [ "${1:-}" = "--apps" ]; then
        printf '%s\t%s\n' "$s" "$(site_installed_apps "$s" 2>/dev/null | grep -vx frappe | tr '\n' ' ')"
    else
        printf '%s\n' "$s"
    fi
done
