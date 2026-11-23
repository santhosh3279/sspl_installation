#!/bin/bash
# The Authelia login gate in front of tenant sites (install/install-auth.sh
# installs it).
#
# Usage:
#   auth.sh status
#   auth.sh link <site>                 put <site> behind Authelia
#   auth.sh unlink <site>
#   auth.sh users
#   auth.sh user-add <user> <email> <name> [<site>,<site>...] [--admin]
#   auth.sh user-passwd <user>
#   auth.sh user-sites <user> [<site>,<site>...]
#   auth.sh user-admin <user> yes|no
#   auth.sh user-disable <user> yes|no
#   auth.sh user-remove <user>
#   auth.sh user-reset-2fa <user>
#   auth.sh trusted ["<cidr> <cidr>..."]
#   auth.sh mail-test <to>
#   auth.sh apply
#
#   link        every domain and IP:port of the site asks for a login; the
#               site becomes HTTPS-only (self-signed until a domain gets a
#               certificate). Users need the site in their list, or admin.
#   user-*      passwords come from env AUTH_PASSWORD, or a prompt; never argv
#   user-reset-2fa  forget the user's TOTP / security keys (lost phone)
#   trusted     networks where the password is enough (no 2FA); "" = none
#   mail-test   send a test mail through the relay (Gmail) and show the result
#
# Logging in: https://<domain>/authelia/ is reached automatically. The first
# login asks the user to register 2FA (an e-mailed code confirms it). With
# "Remember me" ticked the browser stays logged in for AUTH_REMEMBER_ME (1y).
#
# Not for API clients: a linked site sends token-based API calls, the Frappe
# mobile app, webhooks and payment callbacks to the login page too. Use an
# unlinked domain for them, or AUTH_BYPASS_NETWORKS for fixed addresses.
set -euo pipefail
source "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"

usage() { sed -n '5,19p' "$0" | sed 's/^#  */Usage: /' >&2; exit 2; }
USERS_DB="$AUTH_DIR/config/users_database.yml"
users_py() { python3 "$TOOLS_DIR/lib/authusers.py" "$USERS_DB" "$@"; }

need_root()    { [ "$(id -u)" -eq 0 ] || err "Run as root (sudo)."; }
need_auth()    { auth_installed || err "Authelia is not installed — run install/install-auth.sh from the checkout"; }

# A password from AUTH_PASSWORD, or asked twice on the terminal.
ask_password() {
    if [ -n "${AUTH_PASSWORD:-}" ]; then return 0; fi
    [ -t 0 ] || err "set AUTH_PASSWORD (no terminal to ask on)"
    local p2
    while true; do
        read -rsp "Password for $1 (8+ characters): " AUTH_PASSWORD; echo ""
        read -rsp "Confirm: " p2; echo ""
        [ "${#AUTH_PASSWORD}" -ge 8 ] && [ "$AUTH_PASSWORD" = "$p2" ] && break
        echo "Too short or not matching."
    done
    export AUTH_PASSWORD
}

check_sites() {   # $1 = comma list; every name must be a site
    local s
    for s in ${1//,/ }; do site_exists "$s" || err "no site '$s'"; done
}

# The site's own domains must all suit Authelia's cookie rule.
check_linkable() {   # $1 site
    local d bad=""
    for d in $(site_domains "$1"); do auth_domain_ok "$d" || bad="$bad $d"; done
    [ -z "$bad" ] || err "Authelia needs domains with a dot (or IP addresses): remove or rename$bad first (domains.sh)"
    if [ -n "$(site_ports "$1")" ] && [ -z "$(server_ip)" ]; then
        err "$1 has IP:port addresses but the server IP is unknown — set SERVER_IP in $SSPL_CONF"
    fi
}

host_name_to_https() {   # $1 site — host_name follows the scheme change
    local hn new
    hn=$(dc exec -T backend cat "$BENCH_SITES/$1/site_config.json" 2>/dev/null \
        | grep -o '"host_name": *"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/' || true)
    [ -n "$hn" ] || return 0
    local host="${hn#*://}"; host="${host%%/*}"
    local name="${host%%:*}" port=""
    [[ "$host" == *:* ]] && port="${host##*:}"
    if [ -n "$port" ] && [ "$(port_site "$port")" = "$1" ] && [ "$name" = "$(server_ip)" ]; then
        new="$(https_on && auth_active && site_auth_linked "$1" && echo https || echo http)://$name:$port"
    elif [ "$(domain_site "$name")" = "$1" ]; then
        new=$(domain_url "$name")
    else
        return 0
    fi
    [ "$new" = "$hn" ] && return 0
    dc exec -T backend bench --site "$1" set-config host_name "$new" >/dev/null && ok "host_name of $1: $new"
}

CMD="${1:-}"; shift || true
case "$CMD" in
    status)
        if ! auth_installed; then echo "Authelia: not installed (install/install-auth.sh)"; exit 0; fi
        echo "Authelia:  $(curl -fsS -o /dev/null -w 'up' "http://127.0.0.1:$AUTH_PORT/authelia/api/health" 2>/dev/null || echo DOWN) (127.0.0.1:$AUTH_PORT)"
        echo "Mail:      ${SMTP_USER:-not configured} via $SMTP_RELAY_HOST:$SMTP_RELAY_PORT"
        echo "Remember:  $AUTH_REMEMBER_ME    no-2FA networks: ${AUTH_TRUSTED_NETWORKS:-none}"
        echo "Linked sites:"
        auth_sites | while read -r s; do
            printf '  %-30s %s\n' "$s" "$(site_domains "$s" | tr '\n' ' ')$(for p in $(site_ports "$s"); do printf '%s:%s ' "$(server_ip)" "$p"; done)"
        done
        [ -n "$(auth_sites)" ] || echo "  (none)"
        ;;

    link|unlink)
        SITE="${1:-}"; [ -n "$SITE" ] || usage
        need_root; need_auth
        require_site "$SITE"
        take_lock
        if [ "$CMD" = link ]; then
            https_on || err "HTTPS is off on the host nginx — Authelia only works on https:// pages (run install/install-proxy.sh)"
            check_linkable "$SITE"
            if site_auth_linked "$SITE"; then ok "$SITE is already behind Authelia"; fi
            auth_link_site "$SITE"
            if ! apply_proxy; then
                auth_unlink_site "$SITE"; apply_proxy >/dev/null 2>&1 || true
                err "could not apply — $SITE is NOT linked"
            fi
            ok "$SITE is behind Authelia"
        else
            site_auth_linked "$SITE" || { ok "$SITE is not linked"; exit 0; }
            auth_unlink_site "$SITE"
            apply_proxy || err "nginx was not reloaded — fix it, then: $0 apply"
            ok "$SITE no longer asks for an Authelia login"
        fi
        host_name_to_https "$SITE" || warn "could not update host_name of $SITE"
        if [ "$CMD" = link ]; then
            N=$(users_py list --json | python3 -c "import json,sys; s=sys.argv[1]; print(sum(1 for u in json.load(sys.stdin) if s in u['sites'] or u['admin']))" "$SITE")
            echo ""
            echo "✅ $SITE asks for a login on:"
            for d in $(site_domains "$SITE"); do echo "   $(domain_url "$d")"; done
            for p in $(site_ports "$SITE"); do echo "   https://$(server_ip):$p"; done
            [ "$N" -gt 0 ] || warn "no user may enter $SITE yet: $0 user-add <user> <email> <name> $SITE"
        fi
        ;;

    users)
        need_auth; users_py list
        ;;

    user-add)
        need_root; need_auth
        [ $# -ge 3 ] || usage
        U="$1" E="$2" N="$3"; shift 3
        SITES=""; ADMIN=()
        for a in "$@"; do
            if [ "$a" = --admin ]; then ADMIN=(--admin); else SITES="$a"; fi
        done
        check_sites "$SITES"
        ask_password "$U"
        users_py add "$U" "$E" "$N" "$SITES" ${ADMIN[@]+"${ADMIN[@]}"}
        # Without any user Authelia crash-loops: bring it up with the first one.
        if ! curl -fsS -m 5 -o /dev/null "http://127.0.0.1:$AUTH_PORT/authelia/api/health" 2>/dev/null; then
            echo "→ Authelia is not answering; restarting it..."
            auth_dc up -d authelia >/dev/null 2>&1 && auth_dc restart authelia >/dev/null 2>&1 \
                || err "could not start Authelia — docker compose -p $AUTH_PROJECT logs authelia"
            auth_wait || err "the user was added, but Authelia is not healthy"
            ok "Authelia is running"
        fi
        ;;

    user-passwd)
        need_root; need_auth
        [ -n "${1:-}" ] || usage
        ask_password "$1"
        users_py passwd "$1"
        ;;

    user-sites)
        need_root; need_auth
        [ -n "${1:-}" ] || usage
        check_sites "${2:-}"
        users_py sites "$1" "${2:-}"
        ;;

    user-admin|user-disable)
        need_root; need_auth
        [ -n "${1:-}" ] && [ -n "${2:-}" ] || usage
        users_py "${CMD#user-}" "$1" "$2"
        ;;

    user-remove)
        need_root; need_auth
        [ -n "${1:-}" ] || usage
        users_py remove "$1"
        # Its 2FA devices, so a re-created user with the same name starts clean.
        auth_dc exec -T authelia authelia storage user totp delete "$1" --config /config/configuration.yml >/dev/null 2>&1 || true
        auth_dc exec -T authelia authelia storage user webauthn delete "$1" --all --config /config/configuration.yml >/dev/null 2>&1 || true
        ;;

    user-reset-2fa)
        need_root; need_auth
        [ -n "${1:-}" ] || usage
        users_py list --json | grep -F "\"user\": \"$1\"" >/dev/null || err "no user '$1'"
        auth_dc exec -T authelia authelia storage user totp delete "$1" --config /config/configuration.yml >/dev/null 2>&1 \
            && ok "TOTP of $1 removed" || ok "$1 had no TOTP"
        auth_dc exec -T authelia authelia storage user webauthn delete "$1" --all --config /config/configuration.yml >/dev/null 2>&1 \
            && ok "security keys of $1 removed" || true
        ok "$1 registers 2FA again at the next login"
        ;;

    trusted)
        need_root; need_auth
        NETS="${1:-}"
        for c in $NETS; do valid_cidr "$c" || err "'$c' is not an IPv4 address or CIDR (e.g. 192.168.1.0/24)"; done
        take_lock
        tmp=$(mktemp "$SSPL_CONF.XXXXXX")
        awk -v v="$NETS" 'BEGIN{d=0} /^AUTH_TRUSTED_NETWORKS=/ {print "AUTH_TRUSTED_NETWORKS=\""v"\""; d=1; next} {print} END{if(!d) print "AUTH_TRUSTED_NETWORKS=\""v"\""}' \
            "$SSPL_CONF" > "$tmp"
        chmod --reference="$SSPL_CONF" "$tmp"; mv -f "$tmp" "$SSPL_CONF"
        AUTH_TRUSTED_NETWORKS="$NETS"
        apply_auth || err "Authelia did not take it"
        ok "password only (no 2FA) from: ${NETS:-nowhere}"
        ;;

    mail-test)
        need_root; need_auth
        TO="${1:-}"
        [[ "$TO" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || err "mail-test <to-address>"
        [ -n "$SMTP_USER" ] || err "no mail account configured — re-run install-auth.sh with SMTP_USER and SMTP_PASSWORD"
        FROM="${MAIL_FROM:-$SMTP_USER}"
        SINCE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        printf 'From: SSPL ERP <%s>\nTo: %s\nSubject: SSPL ERP mail test\n\nThe mail relay works (%s).\n' "$FROM" "$TO" "$(hostname)" \
            | auth_dc exec -T mail sendmail -f "$FROM" "$TO" || err "the relay did not accept the message"
        echo "→ Sent to the relay; waiting for $SMTP_RELAY_HOST..."
        for _ in $(seq 1 30); do
            LINE=$(auth_dc logs --since "$SINCE" mail 2>/dev/null | grep -F "to=<$TO>" | tail -1 || true)
            case "$LINE" in
                *status=sent*)     ok "delivered to $SMTP_RELAY_HOST: $(printf '%s' "$LINE" | grep -o 'status=sent.*')"; exit 0 ;;
                *status=bounced*|*status=deferred*)
                    echo "$LINE" | grep -o 'status=.*' >&2
                    err "not delivered — for Gmail: 2-Step Verification on, an app password (not the account password), outbound port $SMTP_RELAY_PORT open" ;;
            esac
            sleep 1
        done
        err "no answer from the relay after 30 s — docker compose -p $AUTH_PROJECT logs mail"
        ;;

    apply)
        need_root; need_auth
        take_lock
        apply_proxy || err "not applied — see above"
        ok "Authelia and nginx are up to date"
        ;;

    *) usage ;;
esac
