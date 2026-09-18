#!/bin/bash
# Download update images while the site is running. Keep the compose tags on
# their current image IDs so a later update can snapshot the old generation.
set -euo pipefail
cd /opt/sspl-erp
source "$(dirname "$0")/sspl-erp-common.sh"

STAGE_FILE="$BACKUP_DIR/staged-images.tsv"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
TMP_FILE=$(mktemp "$BACKUP_DIR/.staged-images.XXXXXX")
declare -a originals=()
declare -a original_ids=()
restore_tags() {
    local i
    for i in "${!originals[@]}"; do
        docker tag "${original_ids[$i]}" "${originals[$i]}" || true
    done
}
cleanup() {
    local status=$?
    restore_tags
    if [ "$status" -ne 0 ]; then
        echo "❌ Image download failed; current image tags were restored."
    fi
    rm -f "$TMP_FILE"
}
trap cleanup EXIT

mapfile -t images < <(docker compose -f "$COMPOSE_FILE" config --images | sort -u)
[ "${#images[@]}" -gt 0 ] || { echo "No compose images found" >&2; exit 1; }
for image in "${images[@]}"; do
    old_id=$(docker image inspect --format '{{.Id}}' "$image")
    originals+=("$image")
    original_ids+=("$old_id")
    stage="sspl-erp-staged:$(printf '%s' "$image" | sha256sum | cut -c1-32)"
    echo "→ Downloading $image (services stay up)..."
    python3 "$(dirname "$0")/sspl-erp-pull-progress.py" "$image"
    new_id=$(docker image inspect --format '{{.Id}}' "$image")
    docker tag "$new_id" "$stage"
    docker tag "$old_id" "$image"
    printf '%s\t%s\t%s\t%s\n' "$image" "$old_id" "$stage" "$new_id" >> "$TMP_FILE"
done
mv -f "$TMP_FILE" "$STAGE_FILE"
echo "✓ Images downloaded. Update system will use them after saving the current images."
