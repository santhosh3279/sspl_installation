#!/bin/bash
# Multi-tenant panel + tools updater — the dashboard's "Update panel & tools".
#
#   1. reset the repo checkout to what the remote has
#   2. refresh the tools in $SSPL_TOOLS_DIR (install-tools.sh; cron and
#      sspl-mt.conf are not touched)
#   3. deploy this folder's app.py, templates, static + service to /opt/sspl-admin and restart
#
# NOT the repo-root update_panel.sh / update_tooling.sh: those deploy the
# single-site panel ("Admin Panel/app.py") over this one and install the
# single-site v2 scripts.
#
# The body is inside main() on purpose: this file lives in the tree it pulls,
# and bash reads scripts incrementally. A pull that rewrites this file mid-run
# would otherwise make bash resume at a byte offset in different text.
#
# From the panel, SSPL_FROM_PANEL=1 defers the service restart until after
# this job has finished (restarting now would kill this very script).
set -e

main() {
    HERE="$(cd "$(dirname "$0")" && pwd)"
    REPO_DIR="$(cd "$HERE/../.." && pwd)"
    PANEL_DST="${SSPL_PANEL_DST:-/opt/sspl-admin}"
    TOOLS_DIR="${SSPL_TOOLS_DIR:-/opt/sspl-erp/tools}"

    echo "==============================="
    echo " SSPL multi-tenant panel & tools update - $(date)"
    echo "==============================="
    echo ""
    echo "→ 1/3 Pulling the latest code into $REPO_DIR"
    [ -d "$REPO_DIR/.git" ] || { echo "   ❌ $REPO_DIR is not a git checkout."; exit 1; }

    # Never let git stop for a prompt nobody can answer: the panel runs one
    # job at a time, so a blocked git would wedge every button.
    NOPROMPT=(GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/true
              GIT_SSH_COMMAND="ssh -o BatchMode=yes")
    OWNER=$(stat -c %U "$REPO_DIR")
    if [ "$OWNER" = "$(id -un)" ]; then
        GIT=(env "${NOPROMPT[@]}" git -C "$REPO_DIR")
    else
        echo "   Running git as '$OWNER' (owner of the checkout)"
        GIT=(sudo -n -H -u "$OWNER" env "${NOPROMPT[@]}" git -C "$REPO_DIR")
    fi

    BEFORE=$("${GIT[@]}" rev-parse HEAD)
    BRANCH=$("${GIT[@]}" rev-parse --abbrev-ref HEAD)
    echo "   Branch: $BRANCH, currently at $(echo "$BEFORE" | cut -c1-7)"
    if [ "$BRANCH" = "HEAD" ]; then
        echo "   ❌ The checkout is on a detached HEAD. Over SSH:"
        echo "        cd $REPO_DIR && git checkout Multi-Site"
        exit 1
    fi
    UPSTREAM=$("${GIT[@]}" for-each-ref --format='%(upstream:short)' \
                   "refs/heads/$BRANCH" 2>/dev/null || true)
    [ -n "$UPSTREAM" ] || UPSTREAM="origin/$BRANCH"
    if ! "${GIT[@]}" fetch --prune "${UPSTREAM%%/*}"; then
        echo "   ❌ git fetch failed — nothing was changed."
        exit 1
    fi
    if ! TARGET=$("${GIT[@]}" rev-parse --verify --quiet "$UPSTREAM^{commit}"); then
        echo "   ❌ $UPSTREAM does not exist on the remote — nothing was changed."
        exit 1
    fi
    # The remote is the truth; this checkout is a deploy target. List what
    # the reset throws away first.
    DIRTY=$("${GIT[@]}" status --porcelain --untracked-files=no)
    [ -z "$DIRTY" ] || { echo "   ⚠ Discarding uncommitted changes:"; echo "$DIRTY" | sed 's/^/     /'; }
    AHEAD=$("${GIT[@]}" --no-pager log --oneline "$TARGET..$BEFORE" 2>/dev/null || true)
    [ -z "$AHEAD" ] || { echo "   ⚠ Discarding local commits (still in the reflog):"; echo "$AHEAD" | sed 's/^/     /'; }
    "${GIT[@]}" reset --hard "$TARGET" >/dev/null
    AFTER=$("${GIT[@]}" rev-parse HEAD)
    if [ "$BEFORE" = "$AFTER" ]; then
        echo "   ✓ Already at $UPSTREAM — redeploying anyway"
    else
        echo "   ✓ Updated $(echo "$BEFORE" | cut -c1-7) → $(echo "$AFTER" | cut -c1-7)"
        "${GIT[@]}" --no-pager log --oneline "$BEFORE..$AFTER" | sed 's/^/     /'
    fi

    # Re-exec the freshly pulled copy of the rest: steps 2-3 may have changed.
    exec bash "$HERE/update_panel.sh" --deploy
}

deploy() {
    HERE="$(cd "$(dirname "$0")" && pwd)"
    PANEL_DST="${SSPL_PANEL_DST:-/opt/sspl-admin}"

    echo ""
    echo "→ 2/3 Refreshing the multi-tenant tools"
    if [ -f "${SSPL_CONF:-/opt/sspl-erp/sspl-mt.conf}" ]; then
        # No --cron/--no-cron and no terminal: install-tools.sh leaves cron alone
        bash "$HERE/../install/install-tools.sh" </dev/null
    else
        echo "   (no stack installed yet — skipped)"
    fi

    echo ""
    echo "→ 3/3 Deploying the admin panel to $PANEL_DST"
    [ -d "$HERE/templates" ] && [ -d "$HERE/static/app" ] \
        || { echo "   ❌ templates/ or static/app/ is missing from the checkout."; exit 1; }
    # Copied beside the old ones, then swapped. Removed first because the built
    # pages have a new file name per build.
    for d in templates static; do
        rm -rf "$PANEL_DST/$d.new"
        cp -r "$HERE/$d" "$PANEL_DST/$d.new"
        rm -rf "${PANEL_DST:?}/$d"
        mv "$PANEL_DST/$d.new" "$PANEL_DST/$d"
    done
    cp "$HERE/app.py" "$PANEL_DST/app.py"
    VER=$(grep -m1 '^PANEL_VERSION' "$HERE/app.py" | cut -d'"' -f2)
    echo "   ✓ app.py, templates, static (v$VER)"
    if [ -f /etc/systemd/system/sspl-admin.service ]; then
        cp "$HERE/sspl-admin.service" /etc/systemd/system/
        systemctl daemon-reload
        if [ "${SSPL_FROM_PANEL:-}" = "1" ]; then
            # Inside the panel's cgroup: restart from a transient timer that
            # fires after this job has ended.
            if systemd-run --on-active=3 --timer-property=AccuracySec=1s \
                    systemctl restart sspl-admin >/dev/null 2>&1; then
                echo "   ✓ The panel restarts into v$VER a few seconds after this job ends — reload the page then."
            else
                echo "   ❌ could not schedule the restart: sudo systemctl restart sspl-admin"
                exit 1
            fi
        else
            systemctl restart sspl-admin
            sleep 2
            systemctl is-active --quiet sspl-admin \
                && echo "   ✓ sspl-admin is running v$VER" \
                || { echo "   ❌ sspl-admin failed to start: sudo journalctl -u sspl-admin -n 30"; exit 1; }
        fi
    fi
    echo ""
    echo "✅ Done."
}

[ "$(id -u)" -eq 0 ] || { echo "❌ Run as root (sudo)." >&2; exit 1; }
if [ "${1:-}" = "--deploy" ]; then deploy; else main "$@"; fi
