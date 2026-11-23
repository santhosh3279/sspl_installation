#!/bin/bash
# Install the Authelia login gate for tenant sites, with its Redis and a mail
# relay, as one compose project in AUTH_DIR (default /opt/sspl-auth):
#
#   authelia  login portal + 2FA (TOTP / security keys), 127.0.0.1:AUTH_PORT
#   redis     Authelia's sessions, persistent: "Remember me" lasts
#             AUTH_REMEMBER_ME (1y) and survives restarts
#   mail      Postfix relay → Gmail (smtp.gmail.com:587) with an app
#             password; not published on any host port, so it can never be
#             an open relay. Authelia sends 2FA registration codes through it.
#
# Nothing is gated yet: link a site afterwards with
#   sites/auth.sh link <site>      (or the panel's Domains… → Authelia)
# and add users with sites/auth.sh user-add.
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
#   ADMIN_USER, ADMIN_EMAIL, AUTH_PASSWORD   create a first admin user
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
# relay. Sites behind it: \$ERP_DIR/site-auth. Users: \$AUTH_DIR/config/users_database.yml
AUTH_DIR=$AUTH_DIR
AUTH_PORT=$AUTH_PORT
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
[[ "$AUTH_PORT" =~ ^[0-9]+$ ]] || err "AUTH_PORT must be a number"
ok "Authelia on 127.0.0.1:$AUTH_PORT, sessions: remember me $AUTH_REMEMBER_ME"
ok "Mail: ${SMTP_USER:-not configured (2FA codes go to $AUTH_DIR/config/notification.txt)} via $SMTP_RELAY_HOST:$SMTP_RELAY_PORT"
[ -n "$AUTH_TRUSTED_NETWORKS" ] && ok "Password only (no 2FA) from: $AUTH_TRUSTED_NETWORKS"
proxy_installed || warn "the host nginx is not installed yet — run install/install-proxy.sh before linking sites"
https_on || warn "HTTPS is off on the host nginx — linked sites need it (HTTPS_PORT + default certificate)"
command -v python3 >/dev/null 2>&1 || err "python3 is needed (users file)"

# ─────────────────────────────────────────────── 2. files and secrets
step "Files in $AUTH_DIR"
mkdir -p "$AUTH_DIR"/{config,secrets,redis-data,mail}
chmod 700 "$AUTH_DIR" "$AUTH_DIR/secrets" "$AUTH_DIR/config"
new_secret() {   # $1 file, $2 length
    [ -s "$1" ] && return 0
    # openssl, not 'tr </dev/urandom | head': that pipe dies of SIGPIPE under pipefail.
    ( umask 077; openssl rand -hex "$(( $2 / 2 ))" | tr -d '\n' > "$1" )
    ok "created $(basename "$1") secret"
}
new_secret "$AUTH_DIR/secrets/session" 64
new_secret "$AUTH_DIR/secrets/storage" 64     # encrypts the 2FA data: back it up
new_secret "$AUTH_DIR/secrets/jwt"     64
new_secret "$AUTH_DIR/secrets/redis"   40
if [ -n "${SMTP_PASSWORD:-}" ]; then
    ( umask 077; printf '%s' "${SMTP_PASSWORD// /}" > "$AUTH_DIR/secrets/smtp_password" )
    ok "saved the Gmail app password"
elif [ ! -e "$AUTH_DIR/secrets/smtp_password" ]; then
    ( umask 077; : > "$AUTH_DIR/secrets/smtp_password" )
fi
[ -z "$SMTP_USER" ] || [ -s "$AUTH_DIR/secrets/smtp_password" ] \
    || warn "SMTP_USER is set but there is no app password yet — re-run with SMTP_PASSWORD"

# Redis reads its password from this file (not argv: 'ps' would show it).
( umask 077
  cat > "$AUTH_DIR/secrets/redis.conf" <<EOF
# Generated by install-auth.sh. Authelia's sessions: keep them on disk.
requirepass $(cat "$AUTH_DIR/secrets/redis")
appendonly yes
appendfsync everysec
save 3600 1 300 100
dir /data
maxmemory-policy noeviction
protected-mode no
EOF
)
chmod 644 "$AUTH_DIR/secrets/redis.conf"   # the container's redis user reads it; the directory is 0700

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

cat > "$AUTH_DIR/compose.yaml" <<EOF
# Generated by install-auth.sh — re-run it instead of editing.
name: $AUTH_PROJECT
services:
  redis:
    image: $AUTH_REDIS_IMAGE
    restart: unless-stopped
    command: ["redis-server", "/usr/local/etc/redis/redis.conf"]
    volumes:
      - ./secrets/redis.conf:/usr/local/etc/redis/redis.conf:ro
      - ./redis-data:/data
    healthcheck:
      test: ["CMD-SHELL", "REDISCLI_AUTH=\$\$(sed -n 's/^requirepass //p' /usr/local/etc/redis/redis.conf) redis-cli ping | grep -q PONG"]
      interval: 10s
      timeout: 5s
      retries: 10
  mail:
    build: ./mail
    image: sspl-mail-relay:local
    restart: unless-stopped
    environment:
      SMTP_RELAY_HOST: "$SMTP_RELAY_HOST"
      SMTP_RELAY_PORT: "$SMTP_RELAY_PORT"
      SMTP_USER: "$SMTP_USER"
    volumes:
      - ./secrets/smtp_password:/run/secrets/smtp_password:ro
  authelia:
    image: $AUTH_IMAGE
    restart: unless-stopped
    depends_on:
      redis:
        condition: service_healthy
      mail:
        condition: service_started
    environment:
      TZ: "$(cat /etc/timezone 2>/dev/null || echo Etc/UTC)"
      AUTHELIA_SESSION_SECRET_FILE: /secrets/session
      AUTHELIA_STORAGE_ENCRYPTION_KEY_FILE: /secrets/storage
      AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET_FILE: /secrets/jwt
      AUTHELIA_SESSION_REDIS_PASSWORD_FILE: /secrets/redis
    volumes:
      - ./config:/config
      - ./secrets:/secrets:ro
    ports:
      - "127.0.0.1:$AUTH_PORT:9091"
networks:
  default:
    name: $AUTH_PROJECT
EOF
ok "compose.yaml, mail relay, Redis config"

# Users file (JSON is valid YAML; lib/authusers.py owns it).
USERS_DB="$AUTH_DIR/config/users_database.yml"
[ -s "$USERS_DB" ] || { ( umask 077; echo '{"users": {}}' > "$USERS_DB" ); ok "empty users file"; }
if [ -n "${ADMIN_USER:-}" ]; then
    if python3 "$TOOLS_DIR/lib/authusers.py" "$USERS_DB" list --json | grep -F "\"user\": \"$ADMIN_USER\"" >/dev/null; then
        ok "user $ADMIN_USER already exists"
    else
        : "${ADMIN_EMAIL:?ADMIN_EMAIL is needed with ADMIN_USER}"
        python3 "$TOOLS_DIR/lib/authusers.py" "$USERS_DB" add "$ADMIN_USER" "$ADMIN_EMAIL" "$ADMIN_USER" "" --admin
    fi
fi

# ─────────────────────────────────────────────── 3. Authelia config + start
step "Authelia"
docker image inspect "$AUTH_IMAGE" >/dev/null 2>&1 || docker pull -q "$AUTH_IMAGE" >/dev/null || err "could not pull $AUTH_IMAGE"
CONF="$AUTH_DIR/config/configuration.yml"
TMP=$(mktemp "$AUTH_DIR/config/.configuration.XXXXXX")
render_authelia_conf > "$TMP"
if ! OUT=$(docker run --rm --network none -v "$TMP:/config/configuration.yml:ro" \
        -e AUTHELIA_SESSION_SECRET_FILE=/secrets/session \
        -e AUTHELIA_STORAGE_ENCRYPTION_KEY_FILE=/secrets/storage \
        -e AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET_FILE=/secrets/jwt \
        -e AUTHELIA_SESSION_REDIS_PASSWORD_FILE=/secrets/redis \
        -v "$AUTH_DIR/secrets:/secrets:ro" \
        "$AUTH_IMAGE" authelia validate-config --config /config/configuration.yml 2>&1); then
    rm -f "$TMP"
    printf '%s\n' "$OUT" | grep -E '^\s+- ' >&2 || printf '%s\n' "$OUT" | tail -5 >&2
    err "Authelia rejected the generated config"
fi
chmod 644 "$TMP"; mv -f "$TMP" "$CONF"
ok "configuration.yml (validated)"

auth_dc up -d --build --remove-orphans >/dev/null 2>&1 || { auth_dc up -d --build 2>&1 | tail -5; err "docker compose up failed"; }
# A changed config or secret needs a restart; 'up' only recreates on compose changes.
auth_dc restart authelia mail >/dev/null 2>&1 || true
auth_wait || err "Authelia is not healthy"
ok "Authelia, Redis and the mail relay are running"

# ─────────────────────────────────────────────── 4. nginx
if proxy_installed && [ -f "$FD_DIR/.env" ]; then
    apply_proxy || err "nginx config not applied — see above"
    ok "nginx reloaded${AUTH_SITES_FILE:+ ($(auth_sites | wc -l) linked site(s))}"
fi

if [ -n "${MAIL_TEST_TO:-}" ]; then
    "$TOOLS_DIR/sites/auth.sh" mail-test "$MAIL_TEST_TO" || warn "the test mail did not go out — see above"
fi

echo ""
echo "✅ Authelia is installed. Nothing is gated until you link a site:"
echo "   $TOOLS_DIR/sites/auth.sh user-add <user> <email> \"<Name>\" <site>[,<site>]   (password: env AUTH_PASSWORD or prompt)"
echo "   $TOOLS_DIR/sites/auth.sh link <site>"
echo "   Users log in at https://<site domain>/authelia/, register 2FA (a code is e-mailed),"
echo "   and tick \"Remember me\" to stay logged in for $AUTH_REMEMBER_ME on that browser."
echo "   Back up $AUTH_DIR/secrets/storage and $AUTH_DIR/config/: without them every user re-registers 2FA."
