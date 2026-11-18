#!/bin/bash
# Database-only dump (mariadb-dump as the site's own DB user) of one site,
# several, or all of them. Quick and frequent; no files.
#
# Usage: db-backup.sh --site <host> [--site <host> ...] | --all
#
# Layout, per site:
#   $BACKUP_ROOT/sites/<host>/db-only/<YYYYmmdd_HHMMSS>_<db_name>.sql.gz
#   <remote>/sites/<host>/db-only/...                   (if RCLONE_REMOTE set)
set -uo pipefail
source "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"

parse_site_args "$@"
[ ${#REST[@]} -eq 0 ] || err "unknown argument(s): ${REST[*]}"
take_lock 7200

umask 077
mkdir -p "$SITES_BACKUP_DIR"
chmod 700 "$BACKUP_ROOT" "$SITES_BACKUP_DIR" 2>/dev/null || true

dump_site() {
    local site="$1" dir file ts old dump
    echo ""
    echo "=== $site — database dump ==="
    site_db_creds "$site"
    if [ -z "$DB_NAME" ] || [ -z "$DB_PASS" ]; then
        echo "ERROR: could not read db_name/db_password for $site"; return 1
    fi
    ts=$(date +%Y%m%d_%H%M%S)
    dir="$(site_backup_dir "$site")/db-only"
    mkdir -p "$dir" || return 1
    file="$dir/${ts}_${DB_NAME}.sql.gz"

    if ! dc exec -T -e MYSQL_PWD="$DB_PASS" db \
            mariadb-dump -u "$DB_NAME" "$DB_NAME" --single-transaction --quick \
            | gzip > "$file"; then
        echo "ERROR: dump failed for $site"; rm -f "$file"; return 1
    fi
    # A real ERPNext dump is never this small
    if [ "$(stat -c %s "$file")" -lt 10240 ]; then
        echo "ERROR: dump looks too small ($(du -h "$file" | cut -f1)) — treating as failed"
        rm -f "$file"; return 1
    fi
    echo "  Saved: $file ($(du -h "$file" | cut -f1))"

    # Retention: older than DB_RETENTION_DAYS and outside the newest LOCAL_KEEP_MIN
    old=$(find "$dir" -maxdepth 1 -type f -regextype posix-extended \
            -regex '.*/[0-9]{8}_[0-9]{6}_.*\.sql\.gz' -printf '%f\n' 2>/dev/null \
          | sort -r | tail -n +$((LOCAL_KEEP_MIN + 1))) || true
    while read -r dump; do
        [ -n "$dump" ] || continue
        [ -n "$(find "$dir/$dump" -maxdepth 0 -mtime +"$DB_RETENTION_DAYS" 2>/dev/null)" ] || continue
        rm -f "${dir:?}/$dump" && echo "  Removed old: $dump"
    done <<< "$old"

    upload_dump "$site" "$file"
    return 0
}

upload_dump() {
    local site="$1" file="$2" remote base old dump ok_count=0
    [ -n "$RCLONE_REMOTE" ] || return 0
    rclone_expand_targets "$RCLONE_REMOTE"
    if [ ${#RCLONE_TARGETS[@]} -eq 0 ]; then
        echo "  WARNING: RCLONE_REMOTE='$RCLONE_REMOTE' names no usable remote — not uploaded"
        return 0
    fi
    for remote in "${RCLONE_TARGETS[@]}"; do
        base="${remote%/}/sites/$site"
        rclone_make_room "$base" "$file"
        if ! rclone copy "$file" "$base/db-only"; then
            echo "  WARNING: upload to $remote failed — not pruning it"
            continue
        fi
        ok_count=$((ok_count + 1))
        rclone_prune_flags "$remote"
        old=$(rclone lsf --files-only "$base/db-only" 2>/dev/null \
              | grep -E '^[0-9]{8}_[0-9]{6}_.*\.sql\.gz$' | sort -r | tail -n +$((CLOUD_KEEP + 1))) || true
        while read -r dump; do
            [ -n "$dump" ] || continue
            rclone deletefile ${PRUNE_FLAGS[@]+"${PRUNE_FLAGS[@]}"} "$base/db-only/$dump" 2>/dev/null \
                && echo "    Removed from cloud: $dump" \
                || echo "    WARNING: could not remove from cloud: $dump"
        done <<< "$old"
    done
    echo "  Cloud: $ok_count of ${#RCLONE_TARGETS[@]} destination(s) succeeded"
}

echo "=== SSPL database dump — $(date) — ${#SITES[@]} site(s) ==="
FAILED=()
for s in "${SITES[@]}"; do
    dump_site "$s" || FAILED+=("$s")
done
echo ""
if [ ${#FAILED[@]} -gt 0 ]; then
    echo "=== Dump FAILED for: ${FAILED[*]} ==="
    exit 1
fi
echo "=== Dumps completed for all ${#SITES[@]} site(s) — $(date) ==="
