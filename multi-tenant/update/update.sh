#!/bin/bash
# Update the ERP image for ALL tenants, with an image snapshot for rollback.
#
# Usage: update.sh [--yes]
#   --yes   continue without asking if the pre-update backup fails (cron/panel)
#
# One image serves every site, so an update is all-or-nothing on the code side:
#   1. full backup of every site
#   2. 'docker save' of the current images (the rollback point)
#   3. pull the new images (or use the ones download-images.sh staged) —
#      all tenants stay online during the download
#   4. refuse if any site still has an app the new image dropped
#   5. take all tenants offline, switch images, migrate EVERY site
#   6. per site: migrated → install new apps, clear cache, online
#                failed   → left in MAINTENANCE MODE (users see the
#                           maintenance page, not a half-patched database)
#   7. bring the stack back online; snapshot to the cloud; keep IMAGE_KEEP
#
# Exits non-zero if any site failed to migrate, after printing which.
set -euo pipefail
source "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"

YES=no
[ "${1:-}" = "--yes" ] && YES=yes

take_lock
cd "$ERP_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
SNAPSHOT="$IMAGE_BACKUP_DIR/backup_$TIMESTAMP.tar"
STAGE_FILE="$IMAGE_BACKUP_DIR/staged-images.tsv"

trap 'echo ""; echo "❌ Update failed. Services may be in a partial state."; echo "   Roll back images: $TOOLS_DIR/update/rollback.sh"; echo "   Restore a site:   $TOOLS_DIR/backup/restore-with-backup.sh --site <host> <folder>"' ERR
trap 'stack_online' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "=============================="
echo " SSPL ERP Update (all tenants) - $(date)"
echo "=============================="

mapfile -t ALL_SITES < <(list_sites)
[ ${#ALL_SITES[@]} -gt 0 ] || err "No sites found — is the stack up?"
echo "Sites: ${ALL_SITES[*]}"

# ─────────────────────────────────────────────────────────── 1. backups
echo ""
echo "→ Full backup of every site..."
if "$TOOLS_DIR/backup/backup.sh" --all; then
    ok "All sites backed up"
else
    warn "Backup failed for at least one site (see above)"
    if [ "$YES" != yes ]; then
        [ -t 0 ] || err "No terminal to ask on — re-run with --yes to continue anyway"
        read -rp "   Continue with the update anyway? (yes/no): " C
        [ "$C" = yes ] || { echo "Update cancelled."; exit 1; }
    fi
fi

# ─────────────────────────────────────────────────── 2. image snapshot
mkdir -p "$IMAGE_BACKUP_DIR"; chmod 700 "$IMAGE_BACKUP_DIR"
IMAGES=$(dc config --images | awk 'NF' | sort -u)
[ -n "$IMAGES" ] || err "No images in $COMPOSE_FILE"
echo ""
echo "→ Saving current images (rollback point):"
echo "$IMAGES" | sed 's/^/   - /'
# shellcheck disable=SC2086
docker save -o "$SNAPSHOT" $IMAGES
echo "$SNAPSHOT" > "$IMAGE_BACKUP_DIR/latest_backup.txt"
ok "Snapshot: $SNAPSHOT ($(du -h "$SNAPSHOT" | cut -f1))"

# Prune only reclaims the previous update's leftovers; it must run before
# the pull. DOCKER_PRUNE=no on hosts that run other Docker workloads.
if [ "$DOCKER_PRUNE" = yes ]; then
    docker image prune -f >/dev/null 2>&1 || warn "Image prune failed, continuing"
fi

# What each tag points at now, so a stop before the switch can put tags back
declare -A OLD_ID_FOR=()
while IFS= read -r img; do
    OLD_ID_FOR["$img"]=$(docker image inspect --format '{{.Id}}' "$img" 2>/dev/null || true)
done <<< "$IMAGES"
restore_tags() {
    local img
    for img in "${!OLD_ID_FOR[@]}"; do
        [ -n "${OLD_ID_FOR[$img]}" ] && docker tag "${OLD_ID_FOR[$img]}" "$img" || true
    done
}

# ─────────────────────────────────────────────────────── 3. new images
USE_STAGED=no
if [ -s "$STAGE_FILE" ]; then
    USE_STAGED=yes
    while IFS=$'\t' read -r img old_id stage new_id; do
        [ -n "$img" ] || { USE_STAGED=no; break; }
        [ "$(docker image inspect --format '{{.Id}}' "$img" 2>/dev/null)" = "$old_id" ] || USE_STAGED=no
        [ "$(docker image inspect --format '{{.Id}}' "$stage" 2>/dev/null)" = "$new_id" ] || USE_STAGED=no
    done < "$STAGE_FILE"
    diff -q <(printf '%s\n' "$IMAGES") <(cut -f1 "$STAGE_FILE" | sort -u) >/dev/null || USE_STAGED=no
fi
if [ "$USE_STAGED" = yes ]; then
    echo "→ Using previously downloaded images..."
    while IFS=$'\t' read -r img old_id stage new_id; do docker tag "$stage" "$img"; done < "$STAGE_FILE"
else
    [ ! -e "$STAGE_FILE" ] || echo "   Staged images are stale or incomplete; downloading fresh."
    echo "→ Pulling new images (all tenants stay online)..."
    while IFS= read -r img; do
        echo "→ $img"
        if ! python3 "$TOOLS_DIR/update/pull-progress.py" "$img"; then
            restore_tags
            trap - ERR
            echo "❌ Pull failed. Nothing changed — every site is still up on the current images."
            exit 1
        fi
    done <<< "$IMAGES"
fi

# ─────────────────────────────────────────── 4. apps dropped from the image
# Any site with an app the new image lacks would fail to migrate. Checked
# across every site before anything changes.
NEW_BACKEND_IMAGE=$(dc config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["backend"]["image"])')
NEW_APPS=$(docker run --rm --entrypoint ls "$NEW_BACKEND_IMAGE" apps 2>/dev/null) || NEW_APPS=""
[ -n "$NEW_APPS" ] || { restore_tags; trap - ERR; err "Could not list the apps in the new image"; }
DROPPED=()
for s in "${ALL_SITES[@]}"; do
    installed=$(site_installed_apps "$s") || { restore_tags; trap - ERR; err "Could not list apps on $s — nothing changed"; }
    for a in $installed; do
        [ "$a" = frappe ] && continue
        printf '%s\n' "$NEW_APPS" | grep -qx -- "$a" || DROPPED+=("$s:$a")
    done
done
if [ ${#DROPPED[@]} -gt 0 ]; then
    restore_tags
    trap - ERR
    echo "❌ The new image no longer ships apps these sites still have installed:"
    printf '     %s\n' "${DROPPED[@]}"
    echo "   Uninstall each first ($TOOLS_DIR/update/uninstall-app.sh --site <host> <app>),"
    echo "   or put the app back in the image. Nothing changed; every site is still up."
    exit 1
fi
ok "No installed app is missing from the new image"
rm -f "$STAGE_FILE"

# ───────────────────────────────────────────── 5. switch images, migrate
stack_offline
echo "→ Restarting the stack on the new images..."
dc up -d --remove-orphans
# 'up -d' started the user-facing services too — close them again until every
# site's schema matches the new code.
stack_offline
wait_for_services

FAILED=()
for s in "${ALL_SITES[@]}"; do
    fix_db_grants "$s"
done
echo ""
for s in "${ALL_SITES[@]}"; do
    if "$TOOLS_DIR/backup/migrate.sh" --site "$s"; then
        :
    else
        FAILED+=("$s")
    fi
done

# ───────────────────────────────────────────── 6. per-site follow-up
echo ""
for s in "${ALL_SITES[@]}"; do
    if printf '%s\n' "${FAILED[@]+"${FAILED[@]}"}" | grep -qx -- "$s"; then
        # Half-patched: the maintenance page is better than errors on every click
        dc exec -T backend bench --site "$s" set-maintenance-mode on >/dev/null 2>&1 || true
        continue
    fi
    # Apps added to the image reach an existing site only here: 'migrate'
    # walks the site's installed apps, and a new one is not among them.
    installed=$(site_installed_apps "$s" || true)
    for a in $(image_apps); do
        grep -qx -- "$a" <<< "$installed" && continue
        grep -qx -- "$a" "$(skip_apps_file "$s")" 2>/dev/null && continue
        echo "→ $s: installing new app $a..."
        if dc exec -T backend bench --site "$s" install-app "$a"; then
            ok "$s: $a installed"
        else
            echo "   ❌ $s: installing $a failed — check the site; restore its backup if broken"
            FAILED+=("$s")
            dc exec -T backend bench --site "$s" set-maintenance-mode on >/dev/null 2>&1 || true
            continue 2
        fi
    done
    dc exec -T backend bench --site "$s" clear-cache >/dev/null 2>&1 || warn "$s: cache clear failed"
done

trap - ERR
stack_online

echo ""
echo "Update summary:"
for s in "${ALL_SITES[@]}"; do
    if printf '%s\n' "${FAILED[@]+"${FAILED[@]}"}" | grep -qx -- "$s"; then
        echo "   ❌ $s — in MAINTENANCE MODE (migration or new-app install failed)"
    else
        echo "   ✓ $s — online"
    fi
done
dc exec -T backend bench version 2>/dev/null | sed 's/^/   /' || true

# ───────────────────────────────────────── 7. snapshot upload + retention
rclone_expand_targets "$RCLONE_REMOTE"
for remote in "${RCLONE_TARGETS[@]+"${RCLONE_TARGETS[@]}"}"; do
    rclone_make_room "${remote%/}/image-snapshots" "$SNAPSHOT"
    echo "→ Uploading image snapshot to $remote/image-snapshots..."
    rclone copy "$SNAPSHOT" "${remote%/}/image-snapshots" && ok "Uploaded to $remote" || warn "Upload to $remote failed"
done
echo "→ Keeping the newest $IMAGE_KEEP image snapshots..."
ls -t "$IMAGE_BACKUP_DIR"/backup_*.tar 2>/dev/null | tail -n +$((IMAGE_KEEP + 1)) | while read -r old; do
    rm -f "$old"
    for remote in "${RCLONE_TARGETS[@]+"${RCLONE_TARGETS[@]}"}"; do
        rclone_prune_flags "$remote"
        rclone deletefile ${PRUNE_FLAGS[@]+"${PRUNE_FLAGS[@]}"} "${remote%/}/image-snapshots/$(basename "$old")" 2>/dev/null || true
    done
    echo "   Removed: $(basename "$old")"
done

if [ ${#FAILED[@]} -gt 0 ]; then
    cat <<EOF

❌ Update finished with FAILED sites: ${FAILED[*]}
   They are in maintenance mode on the NEW images. The other sites are fine.
   Options, per failed site:
   1. Fix the app (traceback above), then:
        $TOOLS_DIR/backup/migrate.sh --site <host>
        docker compose -f $COMPOSE_FILE exec backend bench --site <host> set-maintenance-mode off
   2. Restore that site from the backup this update took at the start:
        $TOOLS_DIR/backup/restore.sh --site <host> <folder>
   Rolling the images back ($TOOLS_DIR/update/rollback.sh) affects EVERY site —
   the ones that migrated would then run old code on a new schema, so restore
   those from this update's backups too.
EOF
    exit 1
fi
echo ""
echo "✅ Update complete for all ${#ALL_SITES[@]} site(s). Rollback: $TOOLS_DIR/update/rollback.sh"
