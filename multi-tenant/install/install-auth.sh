#!/bin/bash
# Install the Authelia login gate for tenant sites, with its Redis and a mail
# relay, as one compose project in AUTH_DIR (default /opt/sspl-auth):
#
#   authelia-*  one per linked site: login portal + 2FA (TOTP / security
#               keys) with the site's own users, on 127.0.0.1
#   redis       their sessions, persistent: "Remember me" lasts
#               AUTH_REMEMBER_ME (1y) and survives restarts
#   mail        Postfix relay → Gmail (smtp.gmail.com:587) with an app
#               password; not published on any host port, so it can never be
#               an open relay. Authelia sends 2FA registration codes through it.
#
# Nothing is gated yet. For each site: add its users, then link it
#   sites/auth.sh user-add <site> <user> <email> <name>
#   sites/auth.sh link <site>      (or the panel's Domains… → Authelia)
#
# Needs the host nginx with HTTPS (install-proxy.sh): Authelia only works on
# https:// pages. Idempotent: re-run to change the mail settings or repair.
#
# Env (all optional; empty keeps what is configured):
#   SMTP_USER            the Gmail address that sends
#   SMTP_PASSWORD        its app password (Google account → Security → 2-Step
#                        Verification → App passwords). Spaces are ignored.
#   MAIL_FROM            sender address (default SMTP_USER; Gmail only allows
#                        the account itself or a verified "Send mail as" alias)
#   AUTH_TRUSTED_NETWORKS  CIDRs where the password is enough, no 2FA,
#                        e.g. "192.168.1.0/24"
#   MAIL_TEST_TO         send a test mail there at the end
#   SSPL_CONF            default /opt/sspl-erp/sspl-mt.conf
#
# SECURITY: never echoes SMTP_PASSWORD or AUTH_PASSWORD. Do not add 'set -x'.
set -euo pipefail

export SSPL_CONF="${SSPL_CONF:-/opt/sspl-erp/sspl-mt.conf}"
[ "$(id -u)" -eq 0 ] || { echo "❌ Run as root (sudo)." >&2; exit 1; }
[ -f "$SSPL_CONF" ] || { echo "❌ $SSPL_CONF is missing — install the stack first." >&2; exit 1; }
_SMTP_USER="${SMTP_USER:-}"
_MAIL_FROM="${MAIL_FROM:-}"
_TRUSTED="${AUTH_TRUSTED_NETWORKS-__keep__}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
source "$HERE/lib/common.sh"
take_lock

# ─────────────────────────────────────────────── 1. config
step "Configuration"
set_conf() {   # KEY VALUE — rewrite or append KEY="VALUE" in SSPL_CONF
    local tmp
    tmp=$(mktemp "$SSPL_CONF.XXXXXX")
    awk -v k="$1" -v v="$2" 'BEGIN{done=0} $0 ~ "^"k"=" {print k"=\""v"\""; done=1; next} {print} END{if(!done) print k"=\""v"\""}' \
        "$SSPL_CONF" > "$tmp"
    chmod --reference="$SSPL_CONF" "$tmp"; mv -f "$tmp" "$SSPL_CONF"
}
if ! grep -q '^AUTH_DIR=' "$SSPL_CONF"; then
    cat >> "$SSPL_CONF" <<EOF

# Authelia login gate (install/install-auth.sh, sites/auth.sh) and its mail
# relay. Sites behind it: \$ERP_DIR/site-auth. Users: \$AUTH_DIR/sites/<site>/config
AUTH_DIR=$AUTH_DIR
AUTH_REMEMBER_ME=$AUTH_REMEMBER_ME        # "Remember me": no new login for this long
AUTH_EXPIRATION=$AUTH_EXPIRATION        # without it: session ends after this ...
AUTH_INACTIVITY=$AUTH_INACTIVITY         # ... or this long idle
AUTH_TRUSTED_NETWORKS="$AUTH_TRUSTED_NETWORKS"   # CIDRs: password only, no 2FA
AUTH_BYPASS_NETWORKS="$AUTH_BYPASS_NETWORKS"    # CIDRs: no login at all (integrations)
SMTP_RELAY_HOST=$SMTP_RELAY_HOST
SMTP_RELAY_PORT=$SMTP_RELAY_PORT
SMTP_USER="$SMTP_USER"                  # Gmail address; app password in \$AUTH_DIR/secrets/smtp_password
MAIL_FROM="$MAIL_FROM"
EOF
    ok "Added the Authelia settings to $SSPL_CONF"
fi
if [ -n "$_SMTP_USER" ]; then
    [[ "$_SMTP_USER" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || err "SMTP_USER '$_SMTP_USER' is not an e-mail address"
    SMTP_USER="$_SMTP_USER"; set_conf SMTP_USER "$SMTP_USER"
fi
if [ -n "$_MAIL_FROM" ]; then
    [[ "$_MAIL_FROM" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || err "MAIL_FROM '$_MAIL_FROM' is not an e-mail address"
    MAIL_FROM="$_MAIL_FROM"; set_conf MAIL_FROM "$MAIL_FROM"
fi
if [ "$_TRUSTED" != __keep__ ]; then
    for c in $_TRUSTED; do valid_cidr "$c" || err "AUTH_TRUSTED_NETWORKS: '$c' is not an IPv4 address or CIDR"; done
    AUTH_TRUSTED_NETWORKS="$_TRUSTED"; set_conf AUTH_TRUSTED_NETWORKS "$AUTH_TRUSTED_NETWORKS"
fi
ok "Sessions: remember me $AUTH_REMEMBER_ME"
ok "Mail: ${SMTP_USER:-not configured (2FA codes go to $AUTH_DIR/sites/<site>/config/notification.txt)} via $SMTP_RELAY_HOST:$SMTP_RELAY_PORT"
[ -n "$AUTH_TRUSTED_NETWORKS" ] && ok "Password only (no 2FA) from: $AUTH_TRUSTED_NETWORKS"
proxy_installed || warn "the host nginx is not installed yet — run install/install-proxy.sh before linking sites"
https_on || warn "HTTPS is off on the host nginx — linked sites need it (HTTPS_PORT + default certificate)"
command -v python3 >/dev/null 2>&1 || err "python3 is needed (users file)"

# ─────────────────────────────────────────────── 2. files and secrets
step "Files in $AUTH_DIR"
mkdir -p "$AUTH_DIR"/{sites,secrets,redis-data,mail}
chmod 700 "$AUTH_DIR" "$AUTH_DIR/secrets" "$AUTH_DIR/sites"
auth_new_secret "$AUTH_DIR/secrets/redis" 40
if [ -n "${SMTP_PASSWORD:-}" ]; then
    ( umask 077; printf '%s' "${SMTP_PASSWORD// /}" > "$AUTH_DIR/secrets/smtp_password" )
    ok "saved the Gmail app password"
elif [ ! -e "$AUTH_DIR/secrets/smtp_password" ]; then
    ( umask 077; : > "$AUTH_DIR/secrets/smtp_password" )
fi
[ -z "$SMTP_USER" ] || [ -s "$AUTH_DIR/secrets/smtp_password" ] \
    || warn "SMTP_USER is set but there is no app password yet — re-run with SMTP_PASSWORD"

# Redis reads its password from this file (not argv: 'ps' would show it). 644:
# the container's redis user reads it; the directory is 0700.
REDIS_CHANGED=no
render_auth_redis_conf | _auth_put "$AUTH_DIR/secrets/redis.conf" 644 && REDIS_CHANGED=yes

cat > "$AUTH_DIR/mail/Dockerfile" <<'EOF'
# Postfix relay: accepts mail from the containers on its Docker networks and
# forwards it through the provider's SMTP with a login (Gmail app password).
FROM alpine:3.21
RUN apk add --no-cache postfix cyrus-sasl cyrus-sasl-login ca-certificates tzdata
COPY entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh
EXPOSE 25
ENTRYPOINT ["/entrypoint.sh"]
EOF
cat > "$AUTH_DIR/mail/entrypoint.sh" <<'EOF'
#!/bin/sh
# Generated by install-auth.sh.
set -eu
postconf -e \
    "myhostname=sspl-mail.localdomain" \
    "mydestination=" \
    "inet_interfaces=all" \
    "inet_protocols=ipv4" \
    "mynetworks_style=subnet" \
    "smtpd_relay_restrictions=permit_mynetworks,reject" \
    "relayhost=[${SMTP_RELAY_HOST}]:${SMTP_RELAY_PORT}" \
    "smtp_sasl_auth_enable=yes" \
    "smtp_sasl_password_maps=lmdb:/etc/postfix/sasl_passwd" \
    "smtp_sasl_security_options=noanonymous" \
    "smtp_tls_security_level=encrypt" \
    "smtp_tls_CAfile=/etc/ssl/certs/ca-certificates.crt" \
    "message_size_limit=26214400" \
    "maillog_file=/dev/stdout"
umask 077
if [ -n "${SMTP_USER:-}" ] && [ -s /run/secrets/smtp_password ]; then
    printf '[%s]:%s %s:%s\n' "$SMTP_RELAY_HOST" "$SMTP_RELAY_PORT" "$SMTP_USER" \
        "$(tr -d ' \r\n' < /run/secrets/smtp_password)" > /etc/postfix/sasl_passwd
else
    echo "sspl-mail: no SMTP_USER / app password — mail stays queued" >&2
    : > /etc/postfix/sasl_passwd
fi
postmap lmdb:/etc/postfix/sasl_passwd
exec postfix start-fg
EOF

render_auth_compose | _auth_put "$AUTH_DIR/compose.yaml" 600 || true
ok "compose.yaml, mail relay, Redis config"

# ─────────────────────────────────────────────── 3. start
step "Authelia"
docker image inspect "$AUTH_IMAGE" >/dev/null 2>&1 || docker pull -q "$AUTH_IMAGE" >/dev/null || err "could not pull $AUTH_IMAGE"
# Redis and the relay; the sites' Authelias follow in apply_proxy.
auth_dc up -d --build redis mail >/dev/null 2>&1 || { auth_dc up -d --build redis mail 2>&1 | tail -5; err "docker compose up failed"; }
# A changed password needs a restart; 'up' only recreates on compose changes.
[ "$REDIS_CHANGED" = no ] || auth_dc restart redis >/dev/null 2>&1 || true
auth_dc restart mail >/dev/null 2>&1 || true
ok "Redis and the mail relay are running"

# ─────────────────────────────────────────────── 4. nginx
if proxy_installed && [ -f "$FD_DIR/.env" ]; then
    apply_proxy || err "nginx config not applied — see above"
    [ -z "$AUTH_FAILED" ] || warn "Authelia is not working on:$AUTH_FAILED"
    ok "nginx reloaded ($(auth_sites | wc -l) linked site(s))"
fi

if [ -n "${MAIL_TEST_TO:-}" ]; then
    "$TOOLS_DIR/sites/auth.sh" mail-test "$MAIL_TEST_TO" || warn "the test mail did not go out — see above"
fi

echo ""
echo "✅ Authelia is installed. Nothing is gated until you link a site:"
echo "   $TOOLS_DIR/sites/auth.sh user-add <site> <user> <email> \"<Name>\"   (password: env AUTH_PASSWORD or prompt)"
echo "   $TOOLS_DIR/sites/auth.sh link <site>"
echo "   Every site has its own users, passwords and 2FA."
echo "   Users log in at https://<site domain>/authelia/, register 2FA (a code is e-mailed),"
echo "   and tick \"Remember me\" to stay logged in for $AUTH_REMEMBER_ME on that browser."
echo "   Back up $AUTH_DIR/sites/ and $AUTH_DIR/secrets/: without them every user re-registers 2FA."
