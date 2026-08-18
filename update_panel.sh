#!/bin/bash

# SSPL admin panel updater — the dashboard's "Update Admin Panel" button.
#
# Does the two steps that previously needed an SSH session:
#   1. reset the repo checkout to what the remote has
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

    # A detached HEAD has no upstream to follow, and resetting one to a guess
    # would be picking a branch on the operator's behalf.
    if [ "$BRANCH" = "HEAD" ]; then
        echo "   ❌ The checkout is on a detached HEAD — nothing to follow."
        echo "      Put it on a branch over SSH:"
        echo "        cd $REPO_DIR && git checkout main"
        exit 1
    fi

    # Whatever this branch tracks, or origin/<branch> if it tracks nothing.
    # for-each-ref rather than 'rev-parse @{u}': with no upstream configured
    # that prints the literal '@{u}' on stdout as well as failing, which would
    # sail past an emptiness check and become a nonsense remote name.
    UPSTREAM=$("${GIT[@]}" for-each-ref --format='%(upstream:short)' \
                   "refs/heads/$BRANCH" 2>/dev/null || true)
    [ -n "$UPSTREAM" ] || UPSTREAM="origin/$BRANCH"
    REMOTE_NAME="${UPSTREAM%%/*}"

    if ! "${GIT[@]}" fetch --prune "$REMOTE_NAME"; then
        echo ""
        echo "   ❌ git fetch failed — the panel was NOT updated."
        echo "      No network access to the remote, or it needs credentials"
        echo "      this server does not have. Nothing was changed."
        exit 1
    fi

    if ! TARGET=$("${GIT[@]}" rev-parse --verify --quiet "$UPSTREAM^{commit}"); then
        echo ""
        echo "   ❌ $UPSTREAM does not exist on the remote — the panel was NOT"
        echo "      updated. The branch may have been deleted or renamed."
        exit 1
    fi

    # The remote is the truth; this checkout is a deploy target, not a place
    # anyone should be committing. So take whatever the remote has, rather
    # than fast-forwarding to it — a fast-forward cannot move backwards past a
    # force-push, and cannot move at all once the checkout has diverged, which
    # leaves the server stuck on a commit that exists nowhere else with no way
    # to fix it from the panel. Anything the reset is about to throw away is
    # listed first, because that is the cost of this being automatic.
    DIRTY=$("${GIT[@]}" status --porcelain --untracked-files=no)
    if [ -n "$DIRTY" ]; then
        echo "   ⚠ Discarding uncommitted changes to tracked files:"
        echo "$DIRTY" | sed 's/^/     /'
    fi
    AHEAD=$("${GIT[@]}" --no-pager log --oneline "$TARGET..$BEFORE" 2>/dev/null || true)
    if [ -n "$AHEAD" ]; then
        echo "   ⚠ Discarding commits on this server that the remote does not have:"
        echo "$AHEAD" | sed 's/^/     /'
        echo "     They survive in this checkout's reflog for a while; recover"
        echo "     one over SSH with 'git cherry-pick <sha>' if it mattered."
    fi

    if ! "${GIT[@]}" reset --hard "$TARGET"; then
        echo ""
        echo "   ❌ git reset failed — the panel was NOT updated."
        echo "      Sort it out over SSH:"
        echo "        cd $REPO_DIR && git status"
        exit 1
    fi

    AFTER=$("${GIT[@]}" rev-parse HEAD)
    if [ "$BEFORE" = "$AFTER" ]; then
        echo "   ✓ Already at $UPSTREAM — deploying the checkout anyway,"
        echo "     in case the running panel is older than it."
        CHANGED=""
    else
        echo "   ✓ Updated $(echo "$BEFORE" | cut -c1-7) → $(echo "$AFTER" | cut -c1-7)"
        NEWLOG=$("${GIT[@]}" --no-pager log --oneline "$BEFORE..$AFTER" 2>/dev/null || true)
        if [ -n "$NEWLOG" ]; then
            echo ""
            echo "   New commits:"
            echo "$NEWLOG" | sed 's/^/     /'
        fi
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
