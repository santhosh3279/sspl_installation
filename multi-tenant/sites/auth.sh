#!/bin/bash
# The Authelia login gate in front of tenant sites (install/install-auth.sh
# installs it).
#
# Every site has its own Authelia: own users, passwords and 2FA. The same user
# name on two sites is two unrelated accounts.
#
# Usage:
#   auth.sh status
#   auth.sh link <site>                 put <site> behind Authelia
#   auth.sh unlink <site>
#   auth.sh users [<site>]
#   auth.sh user-add <site> <user> <email> <name>
#   auth.sh user-passwd <site> <user>
#   auth.sh user-disable <site> <user> yes|no
#   auth.sh user-remove <site> <user>
#   auth.sh user-reset-2fa <site> <user>
#   auth.sh trusted ["<cidr> <cidr>..."]
#   auth.sh mail-test <to>
#   auth.sh apply
#
#   link        every domain and IP:port of the site asks for a login; the
#               site becomes HTTPS-only (self-signed until a domain gets a
#               certificate). Add a user first: user-add.
#   unlink      stops asking; the site's users and 2FA are kept for later
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

usage() { sed -n '/^# Usage:/,/^#   auth.sh apply/p' "$0" | sed '1d; s/^#  */Usage: /' >&2; exit 2; }
users_py() { python3 "$TOOLS_DIR/lib/authusers.py" "$(auth_users_db "$SITE")" "${@}"; }

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

# A user command's site, with its files. Users set up by the old shared
# Authelia are moved to their sites first.
need_site() {
    SITE="${1:-}"
    [ -n "$SITE" ] || usage
    need_root; need_auth
    require_site "$SITE"
    if auth_legacy && [ ! -e "$AUTH_DIR/.per-site" ]; then take_lock; auth_migrate || err "could not move the old users to their sites"; fi
    auth_init_site "$SITE" || err "could not create $(auth_site_dir "$SITE")"
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

CMD="${1:-}"; shift || true
case "$CMD" in
    status)
        if ! auth_installed; then echo "Authelia: not installed (install/install-auth.sh)"; exit 0; fi
        echo "Mail:      ${SMTP_USER:-not configured} via $SMTP_RELAY_HOST:$SMTP_RELAY_PORT"
        echo "Remember:  $AUTH_REMEMBER_ME    no-2FA networks: ${AUTH_TRUSTED_NETWORKS:-none}"
        echo "Linked sites:"
        auth_sites | while read -r s; do
            printf '  %-30s %-5s %s user(s)  127.0.0.1:%s  %s\n' "$s" "$(auth_healthy "$s" && echo up || echo DOWN)" \
                "$(auth_user_count "$s")" "$(auth_upstream_port "$s")" \
                "$(site_domains "$s" | tr '\n' ' ')$(for p in $(site_ports "$s"); do printf '%s:%s ' "$(server_ip)" "$p"; done)"
        done
        if auth_legacy; then echo "Still on the old shared Authelia — run: $0 apply"; fi
        [ -n "$(auth_sites)" ] || echo "  (none)"
        ;;

    link|unlink)
        need_site "${1:-}"
        take_lock
        if [ "$CMD" = link ]; then
            https_on || err "HTTPS is off on the host nginx — Authelia only works on https:// pages (run install/install-proxy.sh)"
            check_linkable "$SITE"
            [ "$(auth_user_count "$SITE")" -gt 0 ] || err "add a user first, or nobody can enter: $0 user-add $SITE <user> <email> <name>"
            WAS=no; site_auth_linked "$SITE" && WAS=yes
            if site_auth_linked "$SITE"; then ok "$SITE is already behind Authelia"; fi
            auth_link_site "$SITE"
            if ! apply_proxy || [[ " $AUTH_FAILED " == *" $SITE "* ]]; then
                if [ "$WAS" = no ]; then auth_unlink_site "$SITE"; apply_proxy >/dev/null 2>&1 || true; fi
                err "could not apply — see above$([ "$WAS" = yes ] || echo "; $SITE is NOT linked")"
            fi
            ok "$SITE is behind Authelia"
        else
            site_auth_linked "$SITE" || { ok "$SITE is not linked"; exit 0; }
            auth_unlink_site "$SITE"
            apply_proxy || err "nginx was not reloaded — fix it, then: $0 apply"
            ok "$SITE no longer asks for an Authelia login"
        fi
        sync_host_name "$SITE" || warn "could not update host_name of $SITE"
        if [ "$CMD" = link ]; then
            echo ""
            echo "✅ $SITE asks for a login on:"
            for d in $(site_domains "$SITE"); do echo "   $(domain_url "$d")"; done
            for p in $(site_ports "$SITE"); do echo "   https://$(server_ip):$p"; done
        fi
        ;;

    users)
        need_auth
        for SITE in ${1:-$(list_sites)}; do
            echo "$SITE$(site_auth_linked "$SITE" && echo " (linked)")"
            users_py list | sed 's/^/   /'
        done
        ;;

    user-add)
        [ $# -eq 4 ] || usage
        need_site "$1"; shift
        ask_password "$1"
        users_py add "$1" "$2" "$3"
        # Its Authelia does not run without users: start it with the first one.
        if site_auth_linked "$SITE" && ! auth_healthy "$SITE"; then
            take_lock
            apply_proxy || err "the user was added, but Authelia was not applied — see above"
            auth_healthy "$SITE" || err "the user was added, but Authelia of $SITE is not healthy"
        fi
        ;;

    user-passwd)
        [ $# -eq 2 ] || usage
        need_site "$1"
        ask_password "$2"
        users_py passwd "$2"
        ;;

    user-disable)
        [ $# -eq 3 ] || usage
        need_site "$1"
        users_py disable "$2" "$3"
        ;;

    user-remove)
        [ $# -eq 2 ] || usage
        need_site "$1"
        users_py list --json | grep -F "\"user\": \"$2\"" >/dev/null || err "no user '$2' on $SITE"
        if site_auth_linked "$SITE" && [ "$(auth_user_count "$SITE")" -le 1 ]; then
            err "$2 is the only user of $SITE — Authelia does not start without one; add another or unlink the site first"
        fi
        # Its 2FA devices too, so a re-created user with the same name starts clean.
        auth_forget_2fa "$SITE" "$2" || err "could not remove the 2FA devices of $2 — nothing was changed"
        users_py remove "$2"
        ;;

    user-reset-2fa)
        [ $# -eq 2 ] || usage
        need_site "$1"
        users_py list --json | grep -F "\"user\": \"$2\"" >/dev/null || err "no user '$2' on $SITE"
        auth_forget_2fa "$SITE" "$2" || err "could not reset the 2FA of $2"
        ok "$2 registers 2FA again at the next login on $SITE"
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
        apply_proxy || err "Authelia did not take it"
        [ -z "$AUTH_FAILED" ] || err "not applied on:$AUTH_FAILED"
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
        [ -z "$AUTH_FAILED" ] || err "not working on:$AUTH_FAILED"
        ok "Authelia and nginx are up to date"
        ;;

    *) usage ;;
esac
