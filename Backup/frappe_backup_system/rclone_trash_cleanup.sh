#!/bin/bash

# rclone trash cleanup — make room for an upload, and no more than that.
#
# WHY THIS EXISTS
# ---------------
# The cloud retention in frappe_backup.sh, frappe_db_backup.sh and
# sspl-erp-update-with-rollback.sh prunes the remote with 'rclone purge' /
# 'rclone deletefile'. On Google Drive those move the files to the account's
# trash, and trashed files still count against the quota. So the remote can
# report "full" while every byte of the overage is old backups that retention
# already decided to throw away.
#
# This script permanently deletes those trashed items — but only when an
# upload would not otherwise fit, and only enough of them to cover the
# shortfall plus HEADROOM_PERCENT. It is not 'rclone cleanup': that empties
# the whole trash unconditionally, which is far more than the caller asked for
# and takes the user's undo path with it.
#
# HOW IT DECIDES WHEN TO STOP
# ---------------------------
# It does not trust its own arithmetic about how big a trashed item is.
# Children of a Drive-trashed folder are not themselves flagged trashed, so a
# listing under-reports exactly the space that matters here, and under-reporting
# means over-deleting. Instead it re-reads 'rclone about' after every single
# delete and stops on the *observed* free-space increase. If the observed free
# space does not move after MAX_NO_PROGRESS deletes in a row (Drive's quota
# accounting can lag), it stops and warns rather than eating the whole trash
# chasing a number that is never going to arrive.
#
# BLAST RADIUS
# ------------
# The trash is account-wide, and this runs unattended as root. By default it
# only considers trash inside the backup folder the remote points at, and only
# items whose names match this project's own backup artifacts. --all-trash
# opts out of both restrictions and is never the default.
#
# Deletions here are PERMANENT and there is no undo. Use --dry-run first.
#
# USAGE
#   rclone_trash_cleanup.sh --remote REMOTE (--need-path PATH | --need-bytes N)
#
#   --remote REMOTE     e.g. "gdrive:frappe-backups" (same value the backup
#                       scripts use). Quota and trash are account-wide, so the
#                       remote's root is what gets measured.
#   --need-path PATH    file or directory about to be uploaded; its size on
#                       disk is the space needed
#   --need-bytes N      space needed, in bytes, if you already know it
#   --headroom PCT      free this much beyond the shortfall (default below)
#   --all-trash         consider every trashed item in the account, not just
#                       this project's backups in the backup folder
#   --dry-run           print the exact delete commands, delete nothing
#
# EXIT CODES
#   0  the upload fits (either it already did, or enough was freed)
#   1  tried, and the remote is still short
#   2  skipped — nothing could be determined or done (not Drive, no 'about'
#      support, rclone missing). Not an error.
#
# Callers must treat a non-zero exit as a WARNING and go ahead with the upload
# anyway: the size estimate is deliberately conservative, and a failed upload
# is a better outcome than a backup aborted before it was ever attempted.
#
# VERIFYING ON A REAL REMOTE (do this once, before trusting it in cron)
#
# The load-bearing one. Run it after a db backup has pruned something, so the
# trash is not empty. It asks for the trashed contents of a LIVE folder, which
# is what the walk below depends on:
#
#   rclone lsf --drive-trashed-only --files-only --format "tsp" gdrive:frappe-backups/db-only
#
#   - lists the pruned dumps -> the walk works, this script is sound
#   - errors, or prints nothing when the trash does hold dumps -> Drive will
#     not resolve a live path under --drive-trashed-only. Only trash sitting at
#     the remote's own root is then reachable, and this script needs reworking
#     to scan 'gdrive:' directly. It will say "listings failed", not "trash is
#     empty", so the log tells you which happened.
#
# Then:
#   rclone about --json gdrive:
#   rclone lsf --drive-trashed-only --dirs-only --format "tp" gdrive:frappe-backups
#   sudo /opt/scripts/v2/rclone_trash_cleanup.sh --remote gdrive:frappe-backups \
#        --need-bytes 999999999999 --dry-run

set -o pipefail   # deliberately NOT 'set -e': every failure here is handled
                  # explicitly, and an unhandled exit would take the caller's
                  # backup down with it.

# ------------------------------------------------------------------ settings
HEADROOM_PERCENT=20   # Free the shortfall plus this much, so the next upload
                      # is not immediately back at the same wall
ONLY_BACKUP_ITEMS=yes # Only delete trash that looks like this project's own
                      # backups (--all-trash overrides)
MAX_NO_PROGRESS=3     # Consecutive deletes that free no measurable space
                      # before giving up — the guard against Drive quota lag
MAX_DEPTH=3           # How deep to walk live folders looking for trashed items

# --------------------------------------------------------------------- args
REMOTE=""
NEED_BYTES=""
NEED_PATH=""
DRY_RUN=no

while [ $# -gt 0 ]; do
    case "$1" in
        --remote)     REMOTE="$2"; shift 2 ;;
        --need-bytes) NEED_BYTES="$2"; shift 2 ;;
        --need-path)  NEED_PATH="$2"; shift 2 ;;
        --headroom)   HEADROOM_PERCENT="$2"; shift 2 ;;
        --all-trash)  ONLY_BACKUP_ITEMS=no; shift ;;
        --dry-run)    DRY_RUN=yes; shift ;;
        -h|--help)    sed -n '1,60p' "$0"; exit 0 ;;
        *) echo "rclone-trash: unknown option '$1'" >&2; exit 2 ;;
    esac
done

log() { echo "  rclone-trash: $*"; }

# Only one sweep at a time. The daily full backup can still be uploading
# gigabytes when the 6-hourly DB backup starts: two sweeps would each watch the
# other's upload eat the free space they just made, neither would trip the
# no-progress guard, and between them they could clear far past need+20%.
# A second sweep simply steps aside — its caller uploads anyway, which is the
# same outcome as having no space to free.
exec 9>/var/lock/rclone-trash-cleanup.lock 2>/dev/null
if [ -e /proc/self/fd/9 ] && command -v flock >/dev/null 2>&1; then
    if ! flock -n 9; then
        log "another trash sweep is already running — skipping"
        exit 2
    fi
fi

human() {
    if command -v numfmt >/dev/null 2>&1; then
        numfmt --to=iec --suffix=B "$1" 2>/dev/null || echo "$1 bytes"
    else
        echo "$1 bytes"
    fi
}

if [ -z "$REMOTE" ]; then
    echo "rclone-trash: --remote is required" >&2
    exit 2
fi
case "$REMOTE" in
    :*)  log "connection-string remotes (':type:...') are not supported — skipping"; exit 2 ;;
    *:*) ;;
    *)   echo "rclone-trash: '$REMOTE' is not a remote (expected 'name:path')" >&2; exit 2 ;;
esac

REMOTE_NAME="${REMOTE%%:*}"
REMOTE_ROOT="$REMOTE_NAME:"   # quota and trash are per account, not per folder

if ! command -v rclone >/dev/null 2>&1; then
    log "rclone is not installed — skipping"
    exit 2
fi

# --------------------------------------------------------- how much is needed
if [ -z "$NEED_BYTES" ]; then
    if [ -z "$NEED_PATH" ]; then
        echo "rclone-trash: one of --need-bytes or --need-path is required" >&2
        exit 2
    fi
    if [ ! -e "$NEED_PATH" ]; then
        echo "rclone-trash: --need-path '$NEED_PATH' does not exist" >&2
        exit 2
    fi
    NEED_BYTES=$(du -sb "$NEED_PATH" 2>/dev/null | cut -f1)
fi
case "$NEED_BYTES" in
    ''|*[!0-9]*) echo "rclone-trash: could not determine the size needed" >&2; exit 2 ;;
esac
case "$HEADROOM_PERCENT" in
    ''|*[!0-9]*) echo "rclone-trash: --headroom must be a whole number of percent" >&2; exit 2 ;;
esac

# ------------------------------------------------------------- free space now
# Not every backend implements 'about' (S3 does not), and a Workspace account
# with unlimited storage reports no 'free' at all. Either way there is no
# shortfall to measure, so there is nothing to justify deleting anything.
#
# stdin is closed on every rclone call in this script: it is also called from
# inside a loop that is itself reading a list on stdin, and a password-prompting
# rclone config would otherwise swallow that list.
read_free() {
    rclone about --json "$REMOTE_ROOT" </dev/null 2>/dev/null \
        | grep -oP '"free"\s*:\s*\K[0-9]+' | head -1
}

FREE=$(read_free)
case "$FREE" in
    ''|*[!0-9]*)
        log "'rclone about' gave no free-space figure for $REMOTE_ROOT"
        log "(the backend may not support it, or the account has no quota) — skipping"
        exit 2 ;;
esac

log "upload needs $(human "$NEED_BYTES"), remote has $(human "$FREE") free"

if [ "$FREE" -ge "$NEED_BYTES" ]; then
    log "enough free space — nothing to clear"
    exit 0
fi

DEFICIT=$((NEED_BYTES - FREE))
TARGET=$((DEFICIT * (100 + HEADROOM_PERCENT) / 100))
log "short by $(human "$DEFICIT") — aiming to free $(human "$TARGET") (+${HEADROOM_PERCENT}%)"

# ------------------------------------------------------- backend must be Drive
# The selective part of this ('permanently delete these specific trashed
# items') needs a trashed-only listing, and Drive is the only backend rclone
# gives one for. Everywhere else the only tool is 'rclone cleanup', which
# empties the entire trash — more than was asked for, so it is not offered
# here, not even behind a flag. Mega gets its own message because it does have
# a bin that fills the same way; the other backends may not have one at all.
REMOTE_TYPE=$(rclone config show "$REMOTE_NAME" </dev/null 2>/dev/null \
    | grep -oP '^\s*type\s*=\s*\K\S+' | head -1)
if [ "$REMOTE_TYPE" = "drive" ]; then
    :
elif [ "$REMOTE_TYPE" = "mega" ]; then
    # Mega has a rubbish bin with the same quota problem, but none of the tools
    # to work on it selectively: rclone exposes no trashed-only listing for
    # Mega, so there is nothing to enumerate, sort oldest-first, or stop halfway
    # through. The only lever is 'rclone cleanup', which empties the whole bin —
    # exactly the unbounded delete this script exists to avoid. So it reports
    # and stops, and the backup scripts keep Mega's bin empty at the other end
    # instead, by pruning with --mega-hard-delete.
    log "remote '$REMOTE_NAME' is Mega."
    TRASHED=$(rclone about --json "$REMOTE_ROOT" </dev/null 2>/dev/null \
        | grep -oP '"trashed"\s*:\s*\K[0-9]+' | head -1)
    case "$TRASHED" in
        ''|0|*[!0-9]*) ;;
        *) log "its rubbish bin holds $(human "$TRASHED")" ;;
    esac
    log "rclone cannot list or delete individual items in a Mega bin — only"
    log "'rclone cleanup $REMOTE_ROOT', which empties all of it. That is more"
    log "than this upload needs, so it is not done here."
    log "With MEGA_HARD_DELETE=yes (the default) the backup scripts prune Mega"
    log "permanently, so the bin should not be filling. If it is, either that"
    log "was turned off or the bin predates it — empty it once by hand:"
    log "  rclone cleanup $REMOTE_ROOT"
    exit 2
else
    log "remote '$REMOTE_NAME' is type '${REMOTE_TYPE:-unknown}', not 'drive'"
    log "selective trash clearing is Drive-only ('rclone cleanup' elsewhere would"
    log "empty the whole trash, not just what is needed) — skipping"
    exit 2
fi

# ---------------------------------------------------------- collect the trash
# Walk live folders and, at each level, list the items that are in the trash.
# Only top-level trashed entries are collected: a recursive trashed-only
# listing cannot see inside a trashed folder anyway (its children are not
# individually flagged), so recursing there would just invent bad numbers.
#
# Records are "modtime|kind|size|path", kind is f or d.
TRASH_LIST=$(mktemp) || exit 2
LIST_ERRORS=$(mktemp) || exit 2
trap 'rm -f "$TRASH_LIST" "$LIST_ERRORS"' EXIT

if [ "$ONLY_BACKUP_ITEMS" = yes ]; then
    SCAN_ROOT="$REMOTE"        # just the backup folder
else
    SCAN_ROOT="$REMOTE_ROOT"   # the whole account
fi

# Join a remote path with a child name. "gdrive:" is already a complete path,
# so it takes no separator — "gdrive:/sub" would be a different (if usually
# equivalent) thing to hand back to rclone, and it corrupts the prefix
# arithmetic below.
join_remote() {
    case "$1" in
        *:) printf '%s%s' "$1" "$2" ;;
        *)  printf '%s/%s' "${1%/}" "$2" ;;
    esac
}

# Each record's path is relative to the directory it was listed in, so the
# absolute remote path is rebuilt by prefixing that directory.
#
# Every listing's exit status is checked. An 'lsf' that fails and one that
# finds nothing both produce no output, and the difference decides whether
# "the trash is empty" is a fact or a fabrication — the trashed-only listing
# of a *live* folder is the part of this that no test here could verify, so
# it has to be able to say when it did not work.
# Failures are recorded in $LIST_ERRORS, a file rather than a variable: this
# runs inside $( ), and a subshell's increments die with the subshell.
lsf_or_note_failure() {   # prints the listing; records a failure on error
    local out
    if out=$(rclone lsf "$@" </dev/null 2>/dev/null); then
        printf '%s' "$out"
    else
        echo "$*" >> "$LIST_ERRORS"
    fi
}

collect() {
    local dir="$1" depth="$2" line name mtime size rest
    local prefix="${dir#"$REMOTE_ROOT"}"
    [ -n "$prefix" ] && prefix="${prefix%/}/"

    while IFS= read -r line; do
        [ -n "$line" ] || continue
        mtime="${line%%|*}"; rest="${line#*|}"
        size="${rest%%|*}";  name="${rest#*|}"
        case "$size" in ''|*[!0-9]*) size=0 ;; esac
        printf '%s|f|%s|%s%s\n' "${mtime:-1970-01-01T00:00:00Z}" "$size" "$prefix" "$name" \
            >> "$TRASH_LIST"
    done <<< "$(lsf_or_note_failure --drive-trashed-only --files-only \
                    --format "tsp" --separator "|" "$dir")"

    # Folders have no size here — the sweep measures what they actually free.
    # A folder with no modtime sorts oldest, which is the order we delete in.
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        mtime="${line%%|*}"; name="${line#*|}"; name="${name%/}"
        printf '%s|d|0|%s%s\n' "${mtime:-1970-01-01T00:00:00Z}" "$prefix" "$name" \
            >> "$TRASH_LIST"
    done <<< "$(lsf_or_note_failure --drive-trashed-only --dirs-only \
                    --format "tp" --separator "|" "$dir")"

    [ "$depth" -ge "$MAX_DEPTH" ] && return 0

    # Recurse into the live (non-trashed) folders only
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        collect "$(join_remote "$dir" "${line%/}")" "$((depth + 1))"
    done <<< "$(lsf_or_note_failure --dirs-only --format "p" "$dir")"
}

log "listing trashed items under $SCAN_ROOT..."
collect "$SCAN_ROOT" 1
LIST_FAILURES=$(wc -l < "$LIST_ERRORS" 2>/dev/null || echo 0)

# Oldest first: the trash is a queue, and the oldest thing in it is the least
# likely to be wanted back.
CANDIDATES=$(sort -t'|' -k1,1 "$TRASH_LIST")

if [ -z "$CANDIDATES" ]; then
    if [ "$LIST_FAILURES" -gt 0 ]; then
        log "found nothing, but $LIST_FAILURES listing(s) failed — this is NOT"
        log "'the trash is empty'. Check by hand:"
        log "  rclone lsf --drive-trashed-only --files-only --format \"tsp\" $SCAN_ROOT"
    else
        log "the trash is empty — nothing to reclaim"
    fi
    exit 1
fi
if [ "$LIST_FAILURES" -gt 0 ]; then
    log "note: $LIST_FAILURES listing(s) failed — some trash may be invisible here"
fi

# Names this project creates. Anything else in the trash is somebody's file and
# is left alone unless --all-trash says otherwise.
is_backup_item() {
    local base="${1##*/}"
    case "$base" in
        db-only|image-snapshots) return 0 ;;   # our own folders, if trashed whole
    esac
    printf '%s' "$base" | grep -qE \
        '^([0-9]{8}_[0-9]{6}|[0-9]{8}_[0-9]{6}_.*\.sql\.gz|backup_[0-9]{8}_[0-9]{6}\.tar)$'
}

# ------------------------------------------------------------------ the sweep
FREED=0
NO_PROGRESS=0
DELETED=0
SKIPPED_FOREIGN=0
PREV_FREE="$FREE"
DRY_ESTIMATE=0

while IFS='|' read -r mtime kind size path; do
    [ -n "$path" ] || continue
    [ "$FREED" -ge "$TARGET" ] && break

    if [ "$ONLY_BACKUP_ITEMS" = yes ] && ! is_backup_item "$path"; then
        SKIPPED_FOREIGN=$((SKIPPED_FOREIGN + 1))
        continue
    fi

    FULL=$(join_remote "$REMOTE_ROOT" "$path")
    if [ "$kind" = d ]; then
        CMD=(rclone purge --drive-use-trash=false --drive-trashed-only "$FULL")
    else
        CMD=(rclone deletefile --drive-use-trash=false --drive-trashed-only "$FULL")
    fi

    if [ "$DRY_RUN" = yes ]; then
        echo "  would run: ${CMD[*]}"
        if [ "$kind" = f ]; then
            DRY_ESTIMATE=$((DRY_ESTIMATE + size))
            [ "$DRY_ESTIMATE" -ge "$TARGET" ] && break
        fi
        continue
    fi

    if ! "${CMD[@]}" </dev/null >/dev/null 2>&1; then
        log "WARNING: could not permanently delete $path"
        continue
    fi
    DELETED=$((DELETED + 1))

    NOW_FREE=$(read_free)
    case "$NOW_FREE" in
        ''|*[!0-9]*) NOW_FREE="$PREV_FREE" ;;
    esac

    if [ "$NOW_FREE" -gt "$PREV_FREE" ]; then
        FREED=$((NOW_FREE - FREE))
        NO_PROGRESS=0
        log "reclaimed $path — $(human "$FREED") of $(human "$TARGET") so far"
    else
        # Deleted, but the quota did not move. Could be an empty folder, could
        # be Drive's accounting lagging. Either way, keep going only briefly:
        # chasing a stale number is how "free 20% extra" becomes "empty the
        # entire trash".
        NO_PROGRESS=$((NO_PROGRESS + 1))
        log "removed $path but free space did not move (${NO_PROGRESS}/${MAX_NO_PROGRESS})"
        if [ "$NO_PROGRESS" -ge "$MAX_NO_PROGRESS" ]; then
            log "stopping: $MAX_NO_PROGRESS deletes in a row freed nothing measurable."
            log "Drive's quota figures can lag by a few minutes — try again later."
            break
        fi
    fi
    PREV_FREE="$NOW_FREE"
done <<< "$CANDIDATES"

# ----------------------------------------------------------------- the verdict
if [ "$DRY_RUN" = yes ]; then
    log "dry run: nothing was deleted."
    log "files listed above account for about $(human "$DRY_ESTIMATE"); trashed"
    log "folders have no size here — the real run measures each delete instead."
    exit 0
fi

if [ "$SKIPPED_FOREIGN" -gt 0 ]; then
    log "left $SKIPPED_FOREIGN trashed item(s) alone — not this project's backups"
    log "(pass --all-trash to include them)"
fi

FINAL_FREE=$(read_free)
case "$FINAL_FREE" in ''|*[!0-9]*) FINAL_FREE=$((FREE + FREED)) ;; esac

log "deleted $DELETED item(s), freed $(human "$FREED"); now $(human "$FINAL_FREE") free"

if [ "$FINAL_FREE" -ge "$NEED_BYTES" ]; then
    log "the upload fits now"
    exit 0
fi

log "still short of the $(human "$NEED_BYTES") this upload needs"
exit 1
