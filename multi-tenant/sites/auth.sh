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
#   auth.sh ips [<site>]
#   auth.sh ip-add <site> bypass|trusted <ip|cidr>
#   auth.sh ip-remove <site> bypass|trusted <ip|cidr>
#   auth.sh sessions <site> [--json]
#   auth.sh session-revoke <site> <id>
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
#   ip-add      for one site, on top of the all-site lists: bypass = no Authelia
#               login from there (the ERPNext login still applies), trusted =
#               password only (no 2FA). /8 or narrower. Everyone behind that
#               address gets it; behind another proxy that is everyone.
#   sessions    browsers that ticked "Remember me", with the days left; revoke
#               logs one out of Authelia at its next request
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

# An IPv4 network in canonical form ("10.1.2.3/24" is 10.1.2.0/24); fails on
# anything else, and on networks so wide they would open the site.
canon_net() {
    python3 -I - "$1" <<'PY'
import ipaddress, sys
try:
    n = ipaddress.IPv4Network(sys.argv[1], strict=False)
except ValueError:
    sys.exit(1)
if n.prefixlen < 8:
    sys.exit(1)
print(n.network_address if n.prefixlen == 32 else n)
PY
}

# Authelia needs one address it can set a cookie on: a domain with a dot, or
# IP + port. The site's dotless names redirect there.
check_linkable() {   # $1 site
    local d
    [ -n "$(auth_site_hosts "$1")" ] \
        || err "$1 has no domain with a dot and no IP + port, which Authelia needs: add one first (domains.sh add $1 <domain>)"
    for d in $(site_domains "$1"); do
        auth_domain_ok "$d" || warn "$d has no dot: browsers that open it are sent to $(auth_home_url "$1")"
    done
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

    ips)
        need_auth
        echo "All sites: bypass: ${AUTH_BYPASS_NETWORKS:-none}   no 2FA: ${AUTH_TRUSTED_NETWORKS:-none}"
        for SITE in ${1:-$(list_sites)}; do
            echo "$SITE$(site_auth_linked "$SITE" && echo " (linked)")"
            for k in bypass trusted; do
                auth_site_nets "$SITE" "$k" | sed "s/^/   $k  /"
            done
        done
        ;;

    ip-add|ip-remove)
        [ $# -eq 3 ] || usage
        need_site "$1"
        KIND="$2"
        case "$KIND" in bypass|trusted) ;; *) usage ;; esac
        NET=$(canon_net "$3") || err "'$3' is not an IPv4 address or network of /8 or narrower (e.g. 203.0.113.7 or 192.168.1.0/24)"
        take_lock
        HAVE=no; auth_site_nets "$SITE" "$KIND" | grep -xF -- "$NET" >/dev/null && HAVE=yes
        if [ "$CMD" = ip-add ]; then
            [ "$HAVE" = no ] || { ok "$NET is already on the list"; exit 0; }
            [ "$(auth_site_nets "$SITE" "$KIND" | wc -l)" -lt 100 ] || err "100 entries per list at most"
        else
            [ "$HAVE" = yes ] || err "$NET is not on the $KIND list of $SITE"
        fi
        BAK=$(mktemp); cp "$AUTH_IPS_FILE" "$BAK" 2>/dev/null || : > "$BAK"
        if [ "$CMD" = ip-add ]; then auth_add_net "$SITE" "$KIND" "$NET"; else auth_remove_net "$SITE" "$KIND" "$NET"; fi
        if site_auth_linked "$SITE"; then
            if ! apply_proxy || [[ " $AUTH_FAILED " == *" $SITE "* ]]; then
                # Adding failed: take it back. Removing failed: keep it removed,
                # so the next apply closes it.
                if [ "$CMD" = ip-add ]; then
                    _edit_auth_ips sh -c 'cat "$1"' _ "$BAK"
                    apply_proxy >/dev/null 2>&1 || true
                    rm -f "$BAK"; err "not applied — see above; $NET was not added"
                fi
                rm -f "$BAK"; err "$NET is off the list but may still be in effect — see above, then: $0 apply"
            fi
        else
            warn "$SITE has no Authelia login: the list takes effect when it is linked"
        fi
        rm -f "$BAK"
        ok "$SITE, $KIND: $NET $([ "$CMD" = ip-add ] && echo added || echo removed)"
        ;;

    sessions|session-revoke)
        SITE="${1:-}"
        [ -n "$SITE" ] || usage
        need_root; need_auth
        require_site "$SITE"
        PORT=$(auth_site_port "$SITE")
        if [ "$CMD" = sessions ]; then
            [ $# -eq 1 ] || [ "$2" = --json ] || usage
            if [ -z "$PORT" ]; then      # never linked: it has no sessions
                if [ $# -gt 1 ]; then echo '{"sessions": [], "unreadable": 0}'; else echo "no remembered logins"; fi
                exit 0
            fi
            set -- list "${@:2}"
        else
            [ $# -eq 2 ] || usage
            [ -n "$PORT" ] || err "$SITE has no Authelia sessions"
            set -- revoke "$2"
        fi
        # The Redis password stays inside the container.
        python3 "$TOOLS_DIR/lib/authsessions.py" "$(auth_site_dir "$SITE")" "$@" -- \
            docker compose -p "$AUTH_PROJECT" -f "$AUTH_DIR/compose.yaml" exec -T redis sh -c \
            'REDISCLI_AUTH=$(sed -n "s/^requirepass //p" /usr/local/etc/redis/redis.conf) exec redis-cli -n "$1" --eval /dev/stdin' \
            _ "$((PORT - AUTH_BASE_PORT))"
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
