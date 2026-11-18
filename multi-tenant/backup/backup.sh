#!/bin/bash
# Full backup (database + public files + private files + site_config.json)
# of one site, several, or all of them.
#
# Usage: backup.sh --site <host> [--site <host> ...] | --all
#
# Layout, per site:
#   $BACKUP_ROOT/sites/<host>/full/<YYYYmmdd_HHMMSS>/
#   <remote>/sites/<host>/full/<YYYYmmdd_HHMMSS>/       (if RCLONE_REMOTE set)
#
# Sites are backed up one after another. One failing site does not stop the
# rest; the script exits non-zero at the end if any site failed.
set -uo pipefail
source "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"

parse_site_args "$@"
[ ${#REST[@]} -eq 0 ] || err "unknown argument(s): ${REST[*]}"
take_lock 7200

umask 077
mkdir -p "$SITES_BACKUP_DIR"
chmod 700 "$BACKUP_ROOT" "$SITES_BACKUP_DIR" 2>/dev/null || true

# Copy the newest container file matching any of the globs into $DEST.
copy_newest() {
    local cdir="$1" glob="$2" file
    file=$(dc exec -T backend bash -c "ls -t $cdir/$glob 2>/dev/null | head -1" | tr -d '\r')
    [ -n "$file" ] || return 1
    dc cp "backend:$file" "$DEST/" >/dev/null && printf '%s\n' "$(basename "$file")"
}

backup_site() {
    local site="$1" ts cdir db stem suffix archive
    ts=$(date +%Y%m%d_%H%M%S)
    cdir="$BENCH_SITES/$site/private/backups"
    DEST="$(site_backup_dir "$site")/full/$ts"

    echo ""
    echo "=== $site — full backup ==="
    site_exists "$site" || { echo "ERROR: no site $site"; return 1; }

    if ! dc exec -T backend bench --site "$site" backup --with-files --compress; then
        echo "ERROR: bench backup failed for $site"; return 1
    fi
    mkdir -p "$DEST" || return 1

    # The database we copy decides which file archives belong with it: picking
    # each archive independently can pair different backup runs.
    if ! db=$(copy_newest "$cdir" "*-database.sql.gz"); then
        echo "ERROR: no database backup found in the container for $site"
        rm -rf "$DEST"; return 1
    fi
    echo "  Copied: $db"
    stem="${db%-database.sql.gz}"
    for suffix in files private-files; do
        archive=$(dc exec -T backend bash -c \
            'for ext in tgz tar; do p="$1/$2-$3.$ext"; [ -s "$p" ] && { printf "%s\n" "$p"; exit 0; }; done; exit 1' \
            bash "$cdir" "$stem" "$suffix" | tr -d '\r') || archive=""
        if [ -z "$archive" ]; then
            echo "ERROR: missing matching $suffix archive for $site — backup incomplete"
            rm -rf "$DEST"; return 1
        fi
        dc cp "backend:$archive" "$DEST/" >/dev/null || { rm -rf "$DEST"; return 1; }
        echo "  Copied: $(basename "$archive")"
    done

    # site_config.json carries the encryption key: without it, encrypted
    # fields (passwords, API keys) in the dump cannot be read after a restore.
    dc cp "backend:$BENCH_SITES/$site/site_config.json" "$DEST/" >/dev/null \
        || { echo "ERROR: could not copy site_config.json"; rm -rf "$DEST"; return 1; }

    cat > "$DEST/backup_manifest.txt" <<EOF
Backup Date: $(date)
Site: $site
Host: $(hostname)
Image: $(dc images backend --format '{{.Repository}}:{{.Tag}} {{.ID}}' 2>/dev/null | head -1)
Contents:
$(ls -lh "$DEST")
EOF
    chmod -R go-rwx "$DEST"

    # Frappe keeps its own copies inside the container; they pile up in the
    # sites volume. Ours are on the host now, so clear the ones just made.
    dc exec -T backend bash -c 'rm -f "$1/$2"-*' bash "$cdir" "$stem" >/dev/null 2>&1 || true

    echo "  Saved: $DEST ($(du -sh "$DEST" | cut -f1))"
    prune_local "$site"
    upload_site "$site" "$ts"
    return 0
}

# Older than RETENTION_DAYS *and* outside the newest LOCAL_KEEP_MIN. Without
# the floor, a cron that stalled past the retention window would empty the
# folder on its next successful run.
prune_local() {
    local dir old folder
    dir="$(site_backup_dir "$1")/full"
    old=$(find "$dir" -maxdepth 1 -type d -regextype posix-extended \
            -regex '.*/[0-9]{8}_[0-9]{6}' -printf '%f\n' 2>/dev/null \
          | sort -r | tail -n +$((LOCAL_KEEP_MIN + 1))) || true
    [ -n "$old" ] || return 0
    while read -r folder; do
        [ -n "$folder" ] || continue
        [ -n "$(find "$dir/$folder" -maxdepth 0 -mtime +"$RETENTION_DAYS" 2>/dev/null)" ] || continue
        rm -rf "${dir:?}/$folder" && echo "  Removed old: $folder"
    done <<< "$old"
}

upload_site() {
    local site="$1" ts="$2" remote base old folder ok_count=0
    [ -n "$RCLONE_REMOTE" ] || return 0
    rclone_expand_targets "$RCLONE_REMOTE"
    if [ ${#RCLONE_TARGETS[@]} -eq 0 ]; then
        echo "  WARNING: RCLONE_REMOTE='$RCLONE_REMOTE' names no usable remote — not uploaded"
        return 0
    fi
    for remote in "${RCLONE_TARGETS[@]}"; do
        base="${remote%/}/sites/$site"
        rclone_make_room "$base" "$DEST"
        echo "  Uploading to $base/full/$ts..."
        if ! rclone copy "$DEST" "$base/full/$ts"; then
            echo "  WARNING: upload to $remote failed — not pruning it"
            continue
        fi
        ok_count=$((ok_count + 1))
        # Pruned only after this remote took today's copy, or keep-10 would
        # quietly become keep-9. The remote is listed directly, so a backup
        # removed locally some other way cannot orphan its cloud copy.
        rclone_prune_flags "$remote"
        old=$(rclone lsf --dirs-only "$base/full" 2>/dev/null | sed 's:/$::' \
              | grep -E '^[0-9]{8}_[0-9]{6}$' | sort -r | tail -n +$((CLOUD_KEEP + 1))) || true
        while read -r folder; do
            [ -n "$folder" ] || continue
            rclone purge ${PRUNE_FLAGS[@]+"${PRUNE_FLAGS[@]}"} "$base/full/$folder" 2>/dev/null \
                && echo "    Removed from cloud: $folder" \
                || echo "    WARNING: could not remove from cloud: $folder"
        done <<< "$old"
    done
    echo "  Cloud: $ok_count of ${#RCLONE_TARGETS[@]} destination(s) succeeded"
}

echo "=== SSPL full backup — $(date) — ${#SITES[@]} site(s) ==="
FAILED=()
for s in "${SITES[@]}"; do
    backup_site "$s" || FAILED+=("$s")
done

echo ""
if [ ${#FAILED[@]} -gt 0 ]; then
    echo "=== Backup FAILED for: ${FAILED[*]} ($(( ${#SITES[@]} - ${#FAILED[@]} )) of ${#SITES[@]} succeeded) ==="
    exit 1
fi
echo "=== Backup completed for all ${#SITES[@]} site(s) — $(date) ==="
