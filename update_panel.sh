#!/bin/bash

# SSPL admin panel updater — the dashboard's "Update Admin Panel" button.
#
# Does the two steps that previously needed an SSH session:
#   1. git pull the repo checkout (fast-forward only)
#   2. deploy the panel from it, via update_tooling.sh with SSPL_ONLY=panel
#
# Only the panel is deployed. The v2 backup / update scripts are left alone
# even if the pull brought new versions of them — deploying those is the
# installation suite's "Update v2 scripts & panel" button. If the pull touched
# them, this script says so at the end rather than leaving you to notice.
#
# The whole body sits inside main() on purpose: this file lives in the tree it
# pulls, and bash reads a script incrementally. A pull that rewrites this file
# mid-run would otherwise make bash resume at a byte offset in different text.
# Wrapping in a function forces bash to parse it all before running any of it.
#
# Usage (on the server):
#   ./update_panel.sh
# or from the panel dashboard, which sets SSPL_FROM_PANEL=1 so the service
# restart is deferred until this job has finished.

set -e

main() {
    REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

    echo "==============================="
    echo " SSPL Admin Panel Update - $(date)"
    echo "==============================="
    echo ""
    echo "→ 1/2 Pulling the latest code into $REPO_DIR"

    if [ ! -d "$REPO_DIR/.git" ]; then
        echo "   ❌ $REPO_DIR is not a git checkout — nothing to pull."
        echo "      Deploy the panel from the installation suite instead."
        exit 1
    fi

    # The panel service runs as root while the checkout is usually owned by a
    # normal user. Root git in someone else's tree refuses with "detected
    # dubious ownership", so run git as whoever owns the checkout — that also
    # picks up their credential helper, if the repo ever becomes private.
    # Never let git stop for a username/password prompt. The panel runs one job
    # at a time, so a git process blocked on a prompt nobody can answer would
    # wedge every other button until someone kills it over SSH.
    NOPROMPT=(GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/true
              GIT_SSH_COMMAND="ssh -o BatchMode=yes")

    OWNER=$(stat -c %U "$REPO_DIR")
    if [ "$OWNER" = "$(id -un)" ]; then
        GIT=(env "${NOPROMPT[@]}" git -C "$REPO_DIR")
    else
        echo "   Running git as '$OWNER' (owner of the checkout)"
        # The variables above must be passed through `env` explicitly: sudo
        # resets the environment by default, so exporting them here would not
        # reach git — and a git that can still prompt is exactly what wedges
        # the panel. -H sets HOME to the owner's, so git reads their
        # ~/.gitconfig (and any credential helper, if the repo goes private).
        GIT=(sudo -n -H -u "$OWNER" env "${NOPROMPT[@]}" git -C "$REPO_DIR")
    fi

    BEFORE=$("${GIT[@]}" rev-parse HEAD)
    BRANCH=$("${GIT[@]}" rev-parse --abbrev-ref HEAD)
    echo "   Branch: $BRANCH, currently at $(echo "$BEFORE" | cut -c1-7)"

    DIRTY=$("${GIT[@]}" status --porcelain --untracked-files=no)
    if [ -n "$DIRTY" ]; then
        echo "   ⚠ The checkout has uncommitted changes to tracked files:"
        echo "$DIRTY" | sed 's/^/     /'
        echo "     A fast-forward that would overwrite them will be refused below."
    fi

    # --ff-only: never create a merge commit on the server, and fail loudly if
    # the checkout has diverged from the remote. A silent merge here would
    # deploy a state that exists on no other machine.
    if ! "${GIT[@]}" pull --ff-only; then
        echo ""
        echo "   ❌ git pull failed — the panel was NOT updated."
        echo "      Common causes: local commits or edits on this server that"
        echo "      have diverged from the remote, or no network access to the"
        echo "      remote. Nothing was changed. Sort it out over SSH:"
        echo "        cd $REPO_DIR && git status"
        exit 1
    fi

    AFTER=$("${GIT[@]}" rev-parse HEAD)
    if [ "$BEFORE" = "$AFTER" ]; then
        echo "   ✓ Already at the latest commit — deploying the checkout anyway,"
        echo "     in case the running panel is older than it."
        CHANGED=""
    else
        echo "   ✓ Updated $(echo "$BEFORE" | cut -c1-7) → $(echo "$AFTER" | cut -c1-7)"
        echo ""
        echo "   New commits:"
        "${GIT[@]}" --no-pager log --oneline "$BEFORE..$AFTER" | sed 's/^/     /'
        CHANGED=$("${GIT[@]}" diff --name-only "$BEFORE" "$AFTER")
    fi

    echo ""
    echo "→ 2/2 Deploying the admin panel"
    # SSPL_ONLY=panel keeps this to the panel. SSPL_FROM_PANEL is inherited
    # from the environment: when the dashboard started this job it is 1, and
    # update_tooling.sh then defers its own service restart so it does not kill
    # this very script mid-run.
    SSPL_ONLY=panel bash "$REPO_DIR/update_tooling.sh"

    # A panel-only deploy can leave the deployed v2 scripts behind the checkout,
    # and nothing in the panel shows that: the suite's version badge only
    # compares the panel. Say it here, where it is still on screen.
    if [ -n "$CHANGED" ] && echo "$CHANGED" | grep -qE '^(Backup/frappe_backup_system/|Production Installation/update and rollback/)'; then
        echo ""
        echo "⚠ This pull also changed the v2 scripts:"
        echo "$CHANGED" | grep -E '^(Backup/frappe_backup_system/|Production Installation/update and rollback/)' | sed 's/^/    /'
        echo "  Those are NOT deployed by this button. To deploy them, open the"
        echo "  ERP Next Installation suite and click 'Update v2 scripts & panel'."
    fi
}

main "$@"
