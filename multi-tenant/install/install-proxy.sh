#!/bin/bash
# Install the host nginx that routes each domain to its tenant site, and move
# the stack's own nginx (the 'frontend' container) off the public port.
#
#   browser ──▶ host nginx :HTTP_PORT ──(Host: <site>)──▶ 127.0.0.1:FRONTEND_PORT
#               map: domain → site                         frontend → backend
#
# install-stack.sh runs this. Run it by hand (as root) on a stack installed
# before the proxy existed: it publishes the frontend on 127.0.0.1 only,
# seeds the domain table with every existing site, and starts nginx on
# HTTP_PORT. Every tenant is unreachable for a few seconds while the frontend
# moves. Idempotent: safe to re-run (it also repairs a broken proxy config).
#
# Env: SSPL_CONF (default /opt/sspl-erp/sspl-mt.conf)
set -euo pipefail

export SSPL_CONF="${SSPL_CONF:-/opt/sspl-erp/sspl-mt.conf}"
[ "$(id -u)" -eq 0 ] || { echo "❌ Run as root (sudo)." >&2; exit 1; }
[ -f "$SSPL_CONF" ] || { echo "❌ $SSPL_CONF is missing — install the stack first." >&2; exit 1; }
source "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"
take_lock

# ─────────────────────────────────────────────── 1. config
step "Configuration"
if ! grep -q '^FRONTEND_PORT=' "$SSPL_CONF"; then
    printf '\n# The stack'"'"'s own nginx, on 127.0.0.1 only; the host nginx (HTTP_PORT)\n# proxies to it. Domains → sites: %s\nFRONTEND_PORT=%s\n' \
        "$DOMAINS_FILE" "$FRONTEND_PORT" >> "$SSPL_CONF"
    ok "Added FRONTEND_PORT=$FRONTEND_PORT to $SSPL_CONF"
fi
[[ "$HTTP_PORT" =~ ^[0-9]+$ && "$FRONTEND_PORT" =~ ^[0-9]+$ ]] || err "HTTP_PORT and FRONTEND_PORT must be numbers"
[ "$HTTP_PORT" != "$FRONTEND_PORT" ] || err "HTTP_PORT and FRONTEND_PORT are both $HTTP_PORT — change FRONTEND_PORT in $SSPL_CONF"
ok "Public port $HTTP_PORT (host nginx) → 127.0.0.1:$FRONTEND_PORT (stack frontend)"

# ─────────────────────────────────────────────── 2. nginx (installed, not started yet)
step "Host nginx"
if ! command -v nginx >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    # Its default site listens on :80 too: install without starting it, so a
    # port clash cannot fail the package install.
    if [ -e /usr/sbin/policy-rc.d ]; then
        apt-get install -y -qq nginx >/dev/null
    else
        printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d; chmod 755 /usr/sbin/policy-rc.d
        trap 'rm -f /usr/sbin/policy-rc.d' EXIT
        apt-get install -y -qq nginx >/dev/null
        rm -f /usr/sbin/policy-rc.d; trap - EXIT
    fi
    ok "nginx installed"
else
    ok "nginx already installed ($(nginx -v 2>&1 | sed 's/.*\///'))"
fi
# The stock site claims default_server on :80 — ours is the default now.
if [ -L /etc/nginx/sites-enabled/default ]; then
    rm -f /etc/nginx/sites-enabled/default
    ok "Disabled nginx's stock default site"
fi

# ─────────────────────────────────────────────── 3. frontend off the public port
FD_ENV="$FD_DIR/.env"
[ -f "$FD_ENV" ] || err "$FD_ENV is missing — install the stack first."
WANT="127.0.0.1:$FRONTEND_PORT"
step "Stack frontend on $WANT"
set_fd_env() {   # KEY VALUE — rewrite or append one line of frappe_docker's .env
    if grep -q "^$1=" "$FD_ENV"; then
        sed -i "s|^$1=.*|$1=$2|" "$FD_ENV"
    else
        printf '%s=%s\n' "$1" "$2" >> "$FD_ENV"
    fi
}
# Only the host nginx can reach the frontend now, so trust its X-Forwarded-For
# (it arrives from the Docker bridge gateway): logs and Frappe see the real
# client IP.
set_fd_env UPSTREAM_REAL_IP_ADDRESS 0.0.0.0/0
set_fd_env HTTP_PUBLISH_PORT "$WANT"
if [ -f "$COMPOSE_FILE" ]; then
    generate_compose || err "docker compose config failed"
    # 'up -d' recreates the frontend only if its definition changed.
    if dc ps --status running --services 2>/dev/null | grep -qx frontend; then
        dc up -d frontend >/dev/null 2>&1 || err "could not recreate the frontend: docker compose -f $COMPOSE_FILE up -d frontend"
    fi
fi
ok "Frontend published on $WANT"

if ss -ltnH "sport = :$HTTP_PORT" 2>/dev/null | grep -q . && ! systemctl is-active --quiet nginx; then
    err "Port $HTTP_PORT is in use by something else: $(ss -ltnpH "sport = :$HTTP_PORT" | head -1)"
fi

# ─────────────────────────────────────────────── 4. domain table
step "Domain table $DOMAINS_FILE"
if [ ! -f "$DOMAINS_FILE" ]; then
    printf '# <domain> <site> — one per line. Managed by %s/sites/domains.sh\n' "$TOOLS_DIR" > "$DOMAINS_FILE"
    chmod 644 "$DOMAINS_FILE"
fi
# Every existing site answers on its own name (stacks from before the proxy).
if [ -f "$COMPOSE_FILE" ] && dc exec -T backend true >/dev/null 2>&1; then
    while IFS= read -r s; do
        [ -n "$(domain_site "$s")" ] || { add_domain "$s" "$s"; ok "$s → $s"; }
    done < <(list_sites)
fi
ok "$(domain_lines | wc -l) domain(s)"

apply_proxy || err "nginx config not applied — see above"
ok "nginx routing on port $HTTP_PORT"
# Docker-published ports bypass ufw; the host nginx does not. Without this an
# active ufw would refuse every tenant (and the containers' own PDF fetches).
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
    ufw allow "$HTTP_PORT/tcp" >/dev/null && ok "ufw: allowed $HTTP_PORT/tcp"
fi
# The containers already resolve every site name (compose.sspl-tenants.yaml);
# only extra domains, added later with domains.sh, change that list.

echo ""
echo "✅ Host nginx routes domains to sites on port $HTTP_PORT."
echo "   Add a domain:  $TOOLS_DIR/sites/domains.sh add <site> <domain> [--primary]"
echo "   Each domain must resolve to this server (public DNS, router, or hosts file)."
