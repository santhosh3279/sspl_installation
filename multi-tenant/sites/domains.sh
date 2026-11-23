#!/bin/bash
# Domains each tenant site answers on. The host nginx rewrites every request
# for a domain to its site, so one site can have many domains.
#
# Usage:
#   domains.sh list [<site>]
#   domains.sh add <site> <domain> [--primary]
#   domains.sh remove <domain>
#   domains.sh primary <site> <domain>
#   domains.sh cert <domain>
#   domains.sh port <site> <port> [--primary]
#   domains.sh unport <port>
#
#   --primary / primary   make <domain> the site's host_name: the URL used in
#                         emails and the one PDFs fetch images from
#   cert                  get a Let's Encrypt certificate for <domain> (renewed
#                         automatically). Needs a public domain that resolves
#                         to this server, and HTTP_PORT 80 reachable from the
#                         internet. Without one, HTTPS uses the self-signed
#                         default certificate (browsers warn).
#   port                  nginx listens on <port> and sends everything to
#                         <site>: http://<server-ip>:<port>/ works with no DNS.
#                         --primary makes that the site's host_name. HTTP
#                         only. Browsers keep cookies per IP, not per port:
#                         a user logged in to two sites on the same IP at
#                         once gets logged out of one — use a domain per
#                         site, or another browser, for that.
#   primary <site> <port> a number: make http://<server-ip>:<port> primary
#
# A site always answers on its own name; that entry cannot be removed (drop
# the site instead). Adding or removing a domain reloads nginx (no downtime)
# and briefly restarts backend and workers, so PDFs can resolve the domain.
#
# Each domain must also resolve to this server: public DNS (an A record), the
# router's DNS, or a hosts-file line on each PC.
#
# Env: LETSENCRYPT_EMAIL overrides the config value for 'cert'; SERVER_IP the
#      address used for http://<ip>:<port>.
set -euo pipefail
_EMAIL_OVERRIDE="${LETSENCRYPT_EMAIL:-}"
_IP_OVERRIDE="${SERVER_IP:-}"     # the panel passes the address it shows
source "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"
[ -z "$_EMAIL_OVERRIDE" ] || LETSENCRYPT_EMAIL="$_EMAIL_OVERRIDE"
[ -z "$_IP_OVERRIDE" ] || SERVER_IP="$_IP_OVERRIDE"

usage() { sed -n '5,12p' "$0" | sed 's/^#  */Usage: /' >&2; exit 2; }

current_host_name() {   # $1 site
    dc exec -T backend cat "$BENCH_SITES/$1/site_config.json" 2>/dev/null \
        | grep -o '"host_name": *"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/' || true
}

# The domain in a site's host_name, without scheme and port.
primary_domain() {   # $1 site
    current_host_name "$1" | sed -E 's#^https?://##; s#[:/].*##'
}

# The port in a site's host_name when it is http://<server-ip>:<port>.
primary_port() {   # $1 site
    current_host_name "$1" | sed -nE "s#^https?://$(server_ip | sed 's/\./\\./g'):([0-9]+)/?\$#\1#p"
}

# A port of a site behind Authelia is HTTPS (self-signed).
port_url() {
    local scheme=http
    if https_on && auth_active && site_auth_linked "$(port_site "$1")"; then scheme=https; fi
    printf '%s://%s:%s' "$scheme" "$(server_ip)" "$1"
}

set_primary() {   # $1 site, $2 domain (or a port number)
    local url
    if valid_port "$2" && [[ "$2" =~ ^[0-9]+$ ]] && [ -n "$(port_site "$2")" ]; then
        url=$(port_url "$2")
    else
        url=$(domain_url "$2")
    fi
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
            domain_lines | while read -r d s; do
                printf '%-40s → %-30s %s\n' "$d" "$s" "$(domain_url "$d")"
            done
            port_lines | while read -r p s; do
                printf '%-40s → %-30s %s\n' "port $p" "$s" "$(port_url "$p")"
            done
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
        if site_auth_linked "$SITE" && ! auth_domain_ok "$DOMAIN"; then
            err "$SITE is behind Authelia, which needs a domain with a dot (or an IP address) — $DOMAIN has none"
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
        echo "✅ $SITE answers on $(domain_url "$DOMAIN")"
        echo "   Make $DOMAIN resolve to this server (DNS A record, router, or hosts file)."
        is_ip "$DOMAIN" || echo "   Public domain? Get a trusted HTTPS certificate: $0 cert $DOMAIN"
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
        if [ "$(primary_domain "$SITE")" = "$DOMAIN" ]; then
            set_primary "$SITE" "$SITE"
        fi
        remove_domain "$DOMAIN"
        ok "$DOMAIN removed from $SITE"
        refresh_routing "$DOMAIN"
        remove_cert "$DOMAIN"     # after the reload: nginx no longer uses it
        echo ""
        echo "✅ $DOMAIN no longer reaches $SITE. Remove its DNS entry too."
        ;;

    primary)
        SITE="${1:-}"; DOMAIN="${2:-}"
        [ -n "$SITE" ] && [ -n "$DOMAIN" ] || usage
        require_site "$SITE"
        if [[ "$DOMAIN" =~ ^[0-9]+$ ]]; then
            [ "$(port_site "$DOMAIN")" = "$SITE" ] || err "port $DOMAIN does not point at $SITE — add it first"
        else
            [ "$(domain_site "$DOMAIN")" = "$SITE" ] || err "$DOMAIN does not point at $SITE — add it first"
        fi
        take_lock
        set_primary "$SITE" "$DOMAIN"
        ;;

    cert)
        DOMAIN="${1:-}"
        [ -n "$DOMAIN" ] || usage
        require_valid_site "$DOMAIN"
        SITE=$(domain_site "$DOMAIN")
        [ -n "$SITE" ] || err "$DOMAIN is not in the domain table — add it first"
        is_ip "$DOMAIN" && err "Let's Encrypt does not issue certificates for IP addresses"
        [ "$HTTP_PORT" = 80 ] || err "Let's Encrypt checks the domain on port 80, but HTTP_PORT is $HTTP_PORT"
        https_on || err "HTTPS is not set up (HTTPS_PORT empty, or no default certificate) — run install/install-proxy.sh from the checkout"
        take_lock
        if ! command -v certbot >/dev/null 2>&1; then
            echo "→ Installing certbot..."
            DEBIAN_FRONTEND=noninteractive apt-get install -y -qq certbot >/dev/null \
                || err "could not install certbot (apt-get install certbot)"
        fi
        mkdir -p "$ACME_WEBROOT"
        # Make sure nginx serves the challenge path before Let's Encrypt asks.
        apply_proxy || err "nginx was not reloaded — fix it first"
        if [ -n "$LETSENCRYPT_EMAIL" ]; then
            ACCOUNT=(--email "$LETSENCRYPT_EMAIL")
        else
            ACCOUNT=(--register-unsafely-without-email)
        fi
        echo "→ Asking Let's Encrypt for $DOMAIN (it connects to http://$DOMAIN/ to check)..."
        # --cert-name keeps the files at $LE_LIVE/$DOMAIN/ (no -0001 suffix).
        # Never the --nginx plugin: it would edit the generated config.
        certbot certonly --webroot -w "$ACME_WEBROOT" \
            --cert-name "$DOMAIN" -d "$DOMAIN" \
            --non-interactive --agree-tos "${ACCOUNT[@]}" \
            --keep-until-expiring \
            --deploy-hook "systemctl reload nginx" \
            || err "Let's Encrypt refused. Check that $DOMAIN resolves to this server's public IP and port 80 is open from the internet."
        systemctl enable --now certbot.timer >/dev/null 2>&1 \
            || warn "certbot.timer not enabled — renewals need 'certbot renew' in cron"
        domain_has_cert "$DOMAIN" || err "certbot finished but $LE_LIVE/$DOMAIN/ has no certificate"
        apply_proxy || err "nginx did not take the new certificate — the previous config is kept"
        ok "nginx serves $DOMAIN with its Let's Encrypt certificate"
        # The primary domain's host_name moves to https.
        if [ "$(primary_domain "$SITE")" = "$DOMAIN" ]; then
            set_primary "$SITE" "$DOMAIN"
        fi
        echo ""
        echo "✅ $(domain_url "$DOMAIN") — renewed automatically (certbot.timer)."
        if [ "$HTTPS_REDIRECT" = yes ]; then echo "   http://$DOMAIN now redirects to HTTPS."; fi
        ;;

    port)
        SITE="${1:-}"; PORT="${2:-}"; PRIMARY=no
        [ -n "$SITE" ] && [ -n "$PORT" ] || usage
        [ "${3:-}" = "--primary" ] && PRIMARY=yes
        valid_port "$PORT" || err "'$PORT' is not a port number"
        require_site "$SITE"
        take_lock
        CUR=$(port_site "$PORT")
        [ -z "$CUR" ] || [ "$CUR" = "$SITE" ] || err "port $PORT already belongs to $CUR"
        for r in "$HTTP_PORT" "$HTTPS_PORT" "$FRONTEND_PORT" "$AUTH_PORT" 22 2785 8090; do
            [ "$PORT" != "$r" ] || err "port $PORT is reserved (HTTP/HTTPS, the stack's nginx, Authelia, SSH, OpenWA or the panel)"
        done
        if [ -z "$CUR" ]; then
            port_busy_by_other "$PORT" && err "port $PORT is in use by another program: $(ss -ltnpH "sport = :$PORT" | head -1)"
            add_port "$PORT" "$SITE"
            if ! apply_proxy; then
                remove_port "$PORT"; apply_proxy >/dev/null 2>&1 || true
                err "nginx could not listen on $PORT — nothing changed"
            fi
            ok "port $PORT → $SITE"
            if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
                ufw allow "$PORT/tcp" >/dev/null && ok "ufw: allowed $PORT/tcp"
            fi
        else
            ok "port $PORT already points at $SITE"
        fi
        [ "$PRIMARY" = yes ] && set_primary "$SITE" "$PORT"
        echo ""
        echo "✅ $SITE answers on $(port_url "$PORT")  (no DNS needed)"
        ;;

    unport)
        PORT="${1:-}"
        valid_port "$PORT" || usage
        take_lock
        SITE=$(port_site "$PORT")
        [ -n "$SITE" ] || err "port $PORT is not assigned to a site"
        if [ "$(primary_port "$SITE")" = "$PORT" ]; then
            set_primary "$SITE" "$SITE"
        fi
        remove_port "$PORT"
        apply_proxy || err "nginx was not reloaded — the port table is saved; fix nginx, then re-run"
        ok "port $PORT no longer reaches $SITE"
        if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
            ufw delete allow "$PORT/tcp" >/dev/null 2>&1 && ok "ufw: closed $PORT/tcp" || true
        fi
        ;;

    *) usage ;;
esac
