#!/bin/bash
# Remove a tenant site: full backup first, then drop its database and move
# its folder to the bench's archived_sites/.
#
# Usage: drop-site.sh <host> [--yes]
#
# Without --yes you must type the site name to confirm. The backup must
# succeed or nothing is dropped. The site's backups stay on disk (and in the
# cloud) after it is gone — the --all backup jobs only prune sites that exist,
# so delete $BACKUP_ROOT/sites/<host>/ by hand once you no longer need them.
set -euo pipefail
source "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"

SITE="${1:-}"
[ -n "$SITE" ] || { echo "Usage: $0 <host> [--yes]" >&2; exit 2; }
YES=no
[ "${2:-}" = "--yes" ] && YES=yes

require_site "$SITE"
take_lock

echo "=============================="
echo " Drop site $SITE - $(date)"
echo "=============================="

if [ "$YES" != yes ]; then
    [ -t 0 ] || err "No terminal to confirm on — pass --yes"
    echo "This DELETES the database of $SITE. Users of $SITE lose access."
    read -rp "Type the site name to confirm: " CONFIRM
    [ "$CONFIRM" = "$SITE" ] || { echo "Cancelled."; exit 0; }
fi

echo "→ Full backup of $SITE first..."
"$TOOLS_DIR/backup/backup.sh" --site "$SITE" || err "Backup failed — $SITE was NOT dropped."

ROOT_PW=$(db_root_password)
echo "→ Dropping $SITE..."
dc exec -T backend bench drop-site "$SITE" --root-password "$ROOT_PW" --no-backup --force
ok "$SITE dropped (folder moved to sites/archived_sites/)"

rm -f "$(skip_apps_file "$SITE")"

DOMAINS=$(site_domains "$SITE" | tr '\n' ' ')
PORTS=$(site_ports "$SITE" | tr '\n' ' ')
remove_site_domains "$SITE"
remove_site_ports "$SITE"
# Authelia: the site is no longer gated, and no user keeps it in their list.
if site_auth_linked "$SITE"; then auth_unlink_site "$SITE"; fi
if auth_installed; then
    python3 "$TOOLS_DIR/lib/authusers.py" "$AUTH_DIR/config/users_database.yml" drop-site "$SITE" \
        || warn "could not remove $SITE from the Authelia users"
fi
if [ -n "$PORTS" ]; then
    echo "   Ports freed: $PORTS"
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
        for p in $PORTS; do ufw delete allow "$p/tcp" >/dev/null 2>&1 || true; done
    fi
fi
if apply_proxy; then
    ok "nginx no longer routes: $DOMAINS"
    for d in $DOMAINS; do remove_cert "$d"; done
else
    warn "nginx not reloaded — run install/install-proxy.sh from the checkout"
fi

# The containers had an entry for the site's name and every other domain of it
NEED_HOSTS=no
for d in $SITE $DOMAINS; do is_ip "$d" || NEED_HOSTS=yes; done
if [ "$NEED_HOSTS" = yes ]; then
    echo "→ Removing $SITE from the containers' host list..."
    apply_tenant_hosts || warn "Host list not refreshed; harmless — re-run $TOOLS_DIR/sites/apply-hosts.sh"
fi

echo ""
echo "✅ $SITE removed. Its backups remain in $(site_backup_dir "$SITE")/"
echo "   Remove its DNS entries too: $DOMAINS"
