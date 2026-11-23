#!/bin/bash
# Move an existing multi-tenant stack to HTTP 80 / HTTPS 443, so every domain
# opens without a port: http://acme.example.com, not http://acme.example.com:8080
#
#   1. checks 80 and 443 are free (or already the host nginx's)
#   2. sets HTTP_PORT=80 and HTTPS_PORT=443 in the config (HTTPS stays off if
#      HTTPS_PORT is empty)
#   3. re-runs install-proxy.sh (nginx, default certificate, ufw, Authelia)
#   4. rewrites each site's host_name (email links, PDFs) without the port
#
# If step 3 fails the old config is put back. IP + port addresses
# (domains.sh port) are not touched. Links with the old port stop working.
# Idempotent: re-run it to finish step 4.
#
# Env: SSPL_CONF (default /opt/sspl-erp/sspl-mt.conf)
set -euo pipefail

export SSPL_CONF="${SSPL_CONF:-/opt/sspl-erp/sspl-mt.conf}"
[ "$(id -u)" -eq 0 ] || { echo "❌ Run as root (sudo)." >&2; exit 1; }
[ -f "$SSPL_CONF" ] || { echo "❌ $SSPL_CONF is missing — install the stack first." >&2; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/../lib/common.sh"
take_lock

step "Checking ports 80 and 443"
[ -f "$COMPOSE_FILE" ] || err "$COMPOSE_FILE is missing — install the stack first."
# The sites' host_name lives in the containers: without them step 4 cannot run.
dc exec -T backend true >/dev/null 2>&1 || err "the backend container is not running — start the stack first"
OLD_HTTP="$HTTP_PORT"; OLD_HTTPS="$HTTPS_PORT"
NEW_HTTPS=443; [ -n "$OLD_HTTPS" ] || NEW_HTTPS=""
for p in 80 $NEW_HTTPS; do
    [ "$FRONTEND_PORT" != "$p" ] || err "FRONTEND_PORT is $p — change it in $SSPL_CONF first"
    s=$(port_site "$p")
    [ -z "$s" ] || err "port $p is the IP + port address of $s — remove it first: $TOOLS_DIR/sites/domains.sh unport $p"
    if port_busy_by_other "$p"; then
        err "Port $p is held by another program: $(ss -ltnpH "sport = :$p" | head -1). Stop it or move it, then re-run."
    fi
done
ok "Free for the ERP's nginx"

if [ "$OLD_HTTP" != 80 ] || [ "$OLD_HTTPS" != "$NEW_HTTPS" ]; then
    step "Configuration"
    BAK="$SSPL_CONF.bak.$(date +%Y%m%d_%H%M%S)"
    cp -p "$SSPL_CONF" "$BAK"
    set_conf() {   # KEY VALUE
        if grep -q "^$1=" "$SSPL_CONF"; then
            sed -i "s|^$1=.*|$1=$2|" "$SSPL_CONF"
        else
            printf '%s=%s\n' "$1" "$2" >> "$SSPL_CONF"
        fi
    }
    set_conf HTTP_PORT 80
    set_conf HTTPS_PORT "$NEW_HTTPS"
    ok "HTTP $OLD_HTTP → 80${NEW_HTTPS:+, HTTPS $OLD_HTTPS → 443} (backup: $BAK)"

    # A child shell: it reads the new config. It inherits the lock.
    if ! bash "$HERE/install-proxy.sh"; then
        cp -p "$BAK" "$SSPL_CONF"
        warn "nginx setup failed — putting port $OLD_HTTP back"
        bash "$HERE/install-proxy.sh" >/dev/null 2>&1 \
            || warn "the old setup did not come back either — run $HERE/install-proxy.sh and read its error"
        err "Not moved. See the error above."
    fi
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
        for p in $OLD_HTTP $OLD_HTTPS; do
            case "$p" in 80|443) continue ;; esac
            [ -z "$(port_site "$p")" ] || continue
            ufw delete allow "$p/tcp" >/dev/null 2>&1 && ok "ufw: closed $p/tcp" || true
        done
    fi
else
    ok "Already on 80${NEW_HTTPS:+ and 443}"
fi
HTTP_PORT=80; HTTPS_PORT="$NEW_HTTPS"

step "Site URLs"
FAILED=""
mapfile -t SITES < <(list_sites)
for s in "${SITES[@]+"${SITES[@]}"}"; do
    sync_host_name "$s" || { FAILED="$FAILED $s"; warn "could not update host_name of $s"; }
    # exec without stdin: -T alone would still read ours.
    echo "   $s: $(site_host_name "$s" < /dev/null)"
done
[ -z "$FAILED" ] || err "nginx moved, but these sites keep their old URL:$FAILED — re-run this script"

echo ""
echo "✅ Every domain opens without a port."
