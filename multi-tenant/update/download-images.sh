#!/bin/bash
# Download the next images while every tenant keeps running. Tags stay on the
# current image IDs, so update.sh can still snapshot the old generation; it
# then switches to these staged copies instead of pulling again.
set -euo pipefail
source "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"
take_lock
cd "$ERP_DIR"

STAGE_FILE="$IMAGE_BACKUP_DIR/staged-images.tsv"
mkdir -p "$IMAGE_BACKUP_DIR"; chmod 700 "$IMAGE_BACKUP_DIR"
TMP_FILE=$(mktemp "$IMAGE_BACKUP_DIR/.staged-images.XXXXXX")
originals=(); original_ids=()
cleanup() {
    local status=$? i
    for i in "${!originals[@]}"; do docker tag "${original_ids[$i]}" "${originals[$i]}" || true; done
    [ "$status" -eq 0 ] || echo "❌ Download failed; current image tags restored."
    rm -f "$TMP_FILE"
}
trap cleanup EXIT

mapfile -t images < <(dc config --images | awk 'NF' | sort -u)
[ ${#images[@]} -gt 0 ] || err "No compose images found"
for img in "${images[@]}"; do
    old_id=$(docker image inspect --format '{{.Id}}' "$img")
    originals+=("$img"); original_ids+=("$old_id")
    stage="sspl-erp-staged:$(printf '%s' "$img" | sha256sum | cut -c1-32)"
    echo "→ Downloading $img (all tenants stay up)..."
    python3 "$TOOLS_DIR/update/pull-progress.py" "$img"
    new_id=$(docker image inspect --format '{{.Id}}' "$img")
    docker tag "$new_id" "$stage"
    docker tag "$old_id" "$img"
    printf '%s\t%s\t%s\t%s\n' "$img" "$old_id" "$stage" "$new_id" >> "$TMP_FILE"
done
mv -f "$TMP_FILE" "$STAGE_FILE"
echo "✓ Images downloaded. update.sh will use them after saving the current images."
