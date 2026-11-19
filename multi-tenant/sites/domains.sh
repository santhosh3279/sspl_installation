#!/bin/bash
# Domains each tenant site answers on. The host nginx rewrites every request
# for a domain to its site, so one site can have many domains.
#
# Usage:
#   domains.sh list [<site>]
#   domains.sh add <site> <domain> [--primary]
#   domains.sh remove <domain>
#   domains.sh primary <site> <domain>
#
#   --primary / primary   make <domain> the site's host_name: the URL used in
#                         emails and the one PDFs fetch images from
#
# A site always answers on its own name; that entry cannot be removed (drop
# the site instead). Adding or removing a domain reloads nginx (no downtime)
# and briefly restarts backend and workers, so PDFs can resolve the domain.
#
# Each domain must also resolve to this server: public DNS (an A record), the
# router's DNS, or a hosts-file line on each PC.
set -euo pipefail
source "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"

usage() { sed -n '5,8p' "$0" | sed 's/^#  */Usage: /' >&2; exit 2; }

site_url_for() {   # $1 host → http://host[:port]
    local u="http://$1"
    [ "$HTTP_PORT" = 80 ] || u="$u:$HTTP_PORT"
    printf '%s' "$u"
}

current_host_name() {   # $1 site
    dc exec -T backend cat "$BENCH_SITES/$1/site_config.json" 2>/dev/null \
        | grep -o '"host_name": *"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/' || true
}

set_primary() {   # $1 site, $2 domain
    local url
    url=$(site_url_for "$2")
    dc exec -T backend bench --site "$1" set-config host_name "$url" >/dev/null
    ok "host_name of $1 set to $url"
}

refresh_routing() {
    apply_proxy || err "nginx was not reloaded — the domain table is saved; fix nginx, then re-run"
    ok "nginx reloaded"
    if ! is_ip "$1"; then   # IPs need no host entry
        echo "→ Letting the containers resolve the domain (backend and workers restart for a few seconds)..."
        apply_tenant_hosts || warn "PDFs may miss images until this is fixed: re-run $TOOLS_DIR/sites/apply-hosts.sh"
    fi
}

CMD="${1:-}"; shift || true
case "$CMD" in
    list)
        if [ -n "${1:-}" ]; then
            require_valid_site "$1"
            site_domains "$1"
        else
            domain_lines | awk '{printf "%-40s → %s\n", $1, $2}'
        fi
        ;;

    add)
        SITE="${1:-}"; DOMAIN="${2:-}"; PRIMARY=no
        [ -n "$SITE" ] && [ -n "$DOMAIN" ] || usage
        [ "${3:-}" = "--primary" ] && PRIMARY=yes
        require_valid_site "$DOMAIN"
        require_site "$SITE"
        take_lock
        CUR=$(domain_site "$DOMAIN")
        [ -z "$CUR" ] || [ "$CUR" = "$SITE" ] || err "$DOMAIN already points at $CUR"
        if [ "$DOMAIN" != "$SITE" ] && list_sites | grep -qx -- "$DOMAIN"; then
            err "$DOMAIN is the name of another site"
        fi
        if [ -n "$CUR" ]; then
            ok "$DOMAIN already points at $SITE"
        else
            add_domain "$DOMAIN" "$SITE"
            ok "$DOMAIN → $SITE"
            refresh_routing "$DOMAIN"
        fi
        [ "$PRIMARY" = yes ] && set_primary "$SITE" "$DOMAIN"
        echo ""
        echo "✅ $SITE answers on $(site_url_for "$DOMAIN")"
        echo "   Make $DOMAIN resolve to this server (DNS A record, router, or hosts file)."
        ;;

    remove)
        DOMAIN="${1:-}"
        [ -n "$DOMAIN" ] || usage
        require_valid_site "$DOMAIN"
        take_lock
        SITE=$(domain_site "$DOMAIN")
        [ -n "$SITE" ] || err "$DOMAIN is not in the domain table"
        [ "$DOMAIN" != "$SITE" ] || err "$DOMAIN is the site's own name — drop the site to remove it"
        # A domain that is the site's host_name: fall back to the site's own name.
        if [ "$(current_host_name "$SITE")" = "$(site_url_for "$DOMAIN")" ]; then
            set_primary "$SITE" "$SITE"
        fi
        remove_domain "$DOMAIN"
        ok "$DOMAIN removed from $SITE"
        refresh_routing "$DOMAIN"
        echo ""
        echo "✅ $DOMAIN no longer reaches $SITE. Remove its DNS entry too."
        ;;

    primary)
        SITE="${1:-}"; DOMAIN="${2:-}"
        [ -n "$SITE" ] && [ -n "$DOMAIN" ] || usage
        require_site "$SITE"
        [ "$(domain_site "$DOMAIN")" = "$SITE" ] || err "$DOMAIN does not point at $SITE — add it first"
        take_lock
        set_primary "$SITE" "$DOMAIN"
        ;;

    *) usage ;;
esac
