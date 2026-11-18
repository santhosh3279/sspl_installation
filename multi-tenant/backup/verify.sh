#!/bin/bash
# Check every site has a recent, complete full backup.
#
# Usage: verify.sh --site <host> ... | --all
# Exits non-zero if any site's newest backup is missing, stale or incomplete,
# so cron mail / monitoring can catch it.
set -uo pipefail
source "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"

parse_site_args "$@"
echo "=== Backup verification — $(date) ==="
BAD=()
for s in "${SITES[@]}"; do
    dir="$(site_backup_dir "$s")/full"
    latest=$(find "$dir" -maxdepth 1 -type d -regextype posix-extended \
               -regex '.*/[0-9]{8}_[0-9]{6}' 2>/dev/null | sort -r | head -1)
    if [ -z "$latest" ]; then
        echo "❌ $s: no full backup at all"; BAD+=("$s"); continue
    fi
    problems=()
    [ -n "$(find "$latest" -maxdepth 0 -mmin +$((MAX_AGE_HOURS * 60)))" ] && problems+=("older than ${MAX_AGE_HOURS}h")
    db=$(find "$latest" -name '*-database.sql.gz' | head -1)
    [ -n "$db" ] || problems+=("no database")
    [ -n "$db" ] && [ "$(du -k "$db" | cut -f1)" -lt 100 ] && problems+=("database suspiciously small")
    [ -n "$(find "$latest" \( -name '*-files.tgz' -o -name '*-files.tar' \) ! -name '*-private-files.*')" ] || problems+=("no public files")
    [ -n "$(find "$latest" \( -name '*-private-files.tgz' -o -name '*-private-files.tar' \))" ] || problems+=("no private files")
    [ -f "$latest/site_config.json" ] || problems+=("no site_config.json")
    count=$(find "$dir" -maxdepth 1 -type d -regextype posix-extended -regex '.*/[0-9]{8}_[0-9]{6}' | wc -l)
    if [ ${#problems[@]} -eq 0 ]; then
        echo "✓ $s: $(basename "$latest") ($(du -sh "$latest" | cut -f1)), $count kept"
    else
        echo "❌ $s: $(basename "$latest") — $(IFS=', '; echo "${problems[*]}")"
        BAD+=("$s")
    fi
done
if [ ${#BAD[@]} -gt 0 ]; then
    echo "=== Verification FAILED for: ${BAD[*]} ==="
    exit 1
fi
echo "=== All ${#SITES[@]} site(s) OK ==="
