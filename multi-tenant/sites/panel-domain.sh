#!/bin/bash
# The admin panel's own domain. The host nginx serves it over HTTPS and passes
# it to the panel; the panel's https://<server-ip>:<port> address stays.
#
# Usage:
#   panel-domain.sh show
#   panel-domain.sh set <domain>
#   panel-domain.sh cert <domain>
#   panel-domain.sh remove
#
#   set     serve the panel on <domain> (self-signed until it has a certificate)
#   cert    the same, plus a Let's Encrypt certificate, renewed automatically.
#           Needs a public domain that resolves to this server, and port 80
#           reachable from the internet.
#
# One domain at a time: setting another replaces it. Not a site's domain.
#
# Env: PANEL_PORT, PANEL_SCHEME  where the panel listens (default: kept, else
#      8090 and https); LETSENCRYPT_EMAIL overrides the config value.
set -euo pipefail
_EMAIL_OVERRIDE="${LETSENCRYPT_EMAIL:-}"
source "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"
[ -z "$_EMAIL_OVERRIDE" ] || LETSENCRYPT_EMAIL="$_EMAIL_OVERRIDE"

usage() { sed -n '5,9p' "$0" | sed 's/^#  *//' >&2; exit 2; }

write_panel() {   # $1 the line, "" = no domain
    if [ -z "$1" ]; then rm -f "$PANEL_DOMAIN_FILE"; return; fi
    local tmp
    tmp=$(mktemp "$PANEL_DOMAIN_FILE.XXXXXX")
    printf '%s\n' "$1" > "$tmp"
    chmod 644 "$tmp"
    mv -f "$tmp" "$PANEL_DOMAIN_FILE"
}

# Write the line and apply it; nothing changes if nginx refuses.
change_panel() {
    local before=""
    if [ -r "$PANEL_DOMAIN_FILE" ]; then before=$(cat "$PANEL_DOMAIN_FILE"); fi
    write_panel "$1"
    if ! apply_proxy; then
        write_panel "$before"
        apply_proxy >/dev/null 2>&1 || true
        err "nginx did not take it — nothing changed"
    fi
}

set_panel() {   # $1 domain
    local old oldport oldscheme
    read -r old oldport oldscheme <<< "$(panel_line)" || true
    local port="${PANEL_PORT:-${oldport:-8090}}" scheme="${PANEL_SCHEME:-${oldscheme:-https}}"
    valid_port "$port" || err "'$port' is not a port number"
    case "$scheme" in http|https) ;; *) err "PANEL_SCHEME is http or https" ;; esac
    change_panel "$1 $port $scheme"
    ok "$1 → the admin panel ($scheme://127.0.0.1:$port)"
    if [ -n "$old" ] && [ "$old" != "$1" ]; then remove_cert "$old"; fi
}

check_domain() {   # $1 domain
    require_valid_site "$1"
    is_ip "$1" && err "enter a domain name, not an IP address"
    [[ "$1" == *.* ]] || err "enter a full domain name, e.g. admin.example.com"
    local owner
    owner=$(domain_site "$1")
    [ -z "$owner" ] || err "$1 is a domain of the site $owner"
    site_exists "$1" && err "$1 is the name of a site"
    proxy_installed || err "the host nginx is not installed — run install/install-proxy.sh from the checkout"
    https_on || err "HTTPS is not set up (HTTPS_PORT empty, or no default certificate) — run install/install-proxy.sh from the checkout"
}

CMD="${1:-}"; shift || true
case "$CMD" in
    show)
        D=$(panel_domain)
        if [ -n "$D" ]; then
            echo "$D ($(domain_has_cert "$D" && echo "Let's Encrypt" || echo self-signed))"
        else
            echo "no panel domain"
        fi
        ;;

    set|cert)
        DOMAIN="${1:-}"
        [ -n "$DOMAIN" ] || usage
        [ "$(id -u)" -eq 0 ] || err "run as root (sudo)"
        take_lock
        check_domain "$DOMAIN"
        if [ "$CMD" = cert ] && [ "$HTTP_PORT" != 80 ]; then
            err "Let's Encrypt checks the domain on port 80, but HTTP_PORT is $HTTP_PORT"
        fi
        set_panel "$DOMAIN"
        if [ "$CMD" = cert ]; then
            get_cert "$DOMAIN"
        fi
        HP=""; [ "$HTTPS_PORT" = 443 ] || HP=":$HTTPS_PORT"
        echo ""
        echo "✅ The admin panel answers on https://$DOMAIN$HP/"
        echo "   Make $DOMAIN resolve to this server (DNS A record, router, or hosts file)."
        ;;

    remove)
        [ "$(id -u)" -eq 0 ] || err "run as root (sudo)"
        take_lock
        OLD=$(panel_domain)
        [ -n "$OLD" ] || err "the panel has no domain"
        change_panel ""
        remove_cert "$OLD"     # after the reload: nginx no longer uses it
        echo ""
        echo "✅ $OLD no longer reaches the panel. Remove its DNS entry too."
        ;;

    *) usage ;;
esac
