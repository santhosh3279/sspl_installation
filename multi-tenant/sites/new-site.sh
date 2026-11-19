#!/bin/bash
# Create a new tenant site on the running bench.
#
# Usage: new-site.sh <host> [--apps app1,app2] [--yes]
#
#   <host>       the hostname users will type, e.g. acme.erp.lan (or an IP)
#   --apps LIST  install only these apps (default: every app in the image).
#                Apps left out are recorded so updates never auto-install them.
#   --yes        skip the confirmation (the panel / scripts)
#
# Env:
#   ADMIN_PASSWORD   Administrator password for the new site (asked if unset)
#
# The MariaDB root password is read from the stack's .env.
#
# SECURITY: never echoes ADMIN_PASSWORD. Do not add 'set -x'.
set -euo pipefail
source "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"

SITE="${1:-}"
[ -n "$SITE" ] && [ "${SITE#-}" = "$SITE" ] || { echo "Usage: $0 <host> [--apps a,b] [--yes]" >&2; exit 2; }
shift
APPS_SPEC=""
YES=no
while [ $# -gt 0 ]; do
    case "$1" in
        --apps) APPS_SPEC="${2:-}"; shift 2 ;;
        --yes)  YES=yes; shift ;;
        *) err "unknown option '$1'" ;;
    esac
done

require_valid_site "$SITE"
take_lock

echo "=============================="
echo " New site $SITE - $(date)"
echo "=============================="

dc exec -T backend true >/dev/null 2>&1 || err "The backend is not running — start the stack first."
site_exists "$SITE" && err "Site $SITE already exists."
OWNER=$(domain_site "$SITE")
[ -z "$OWNER" ] || err "$SITE is already a domain of site $OWNER (sites/domains.sh remove $SITE first)"

# ─────────────────────────────────────────────────────── which apps
mapfile -t AVAILABLE < <(image_apps)
[ ${#AVAILABLE[@]} -gt 0 ] || err "Could not list the apps in the image."
if [ -n "$APPS_SPEC" ]; then
    IFS=',' read -r -a WANT <<< "$APPS_SPEC"
    for a in "${WANT[@]}"; do
        [[ "$a" =~ ^[a-z][a-z0-9_]*$ ]] || err "'$a' is not a valid app name"
        printf '%s\n' "${AVAILABLE[@]}" | grep -qx -- "$a" || err "App '$a' is not in the image (has: ${AVAILABLE[*]})"
    done
else
    WANT=("${AVAILABLE[@]}")
fi
# erpnext must come before apps that depend on it; bench does not sort them
ORDERED=()
for a in erpnext "${WANT[@]}"; do
    printf '%s\n' "${WANT[@]}" | grep -qx -- "$a" || continue
    printf '%s\n' "${ORDERED[@]+"${ORDERED[@]}"}" | grep -qx -- "$a" && continue
    ORDERED+=("$a")
done
SKIPPED=()
for a in "${AVAILABLE[@]}"; do
    printf '%s\n' "${ORDERED[@]}" | grep -qx -- "$a" || SKIPPED+=("$a")
done

echo "Apps to install: ${ORDERED[*]}"
[ ${#SKIPPED[@]} -eq 0 ] || echo "Not installed (and never auto-installed by updates): ${SKIPPED[*]}"

# ─────────────────────────────────────────────────────── passwords
if [ -z "${ADMIN_PASSWORD:-}" ]; then
    [ -t 0 ] || err "ADMIN_PASSWORD is not set and there is no terminal to ask on"
    while true; do
        read -rsp "Administrator password for $SITE: " ADMIN_PASSWORD; echo ""
        read -rsp "Confirm: " P2; echo ""
        [ -n "$ADMIN_PASSWORD" ] && [ "$ADMIN_PASSWORD" = "$P2" ] && break
        echo "Empty or not matching — try again."
    done
fi
ROOT_PW=$(db_root_password)

if [ "$YES" != yes ]; then
    [ -t 0 ] || err "No terminal to confirm on — pass --yes"
    read -rp "Create $SITE? (yes/no): " GO
    [ "$GO" = yes ] || { echo "Cancelled."; exit 0; }
fi

# ─────────────────────────────────────────────────────── create
APP_ARGS=()
for a in "${ORDERED[@]}"; do APP_ARGS+=(--install-app "$a"); done

echo "→ Creating $SITE (5-10 minutes — do not interrupt)..."
dc exec -T backend bench new-site "$SITE" \
    --mariadb-root-password "$ROOT_PW" \
    --admin-password "$ADMIN_PASSWORD" \
    "${APP_ARGS[@]}"
ok "Site created"

for a in "${SKIPPED[@]+"${SKIPPED[@]}"}"; do skip_app "$SITE" "$a"; done

fix_db_grants "$SITE"

# The site's own URL: links in emails, and the address wkhtmltopdf fetches
# images/CSS from when rendering PDFs.
URL=$(domain_url "$SITE")
dc exec -T backend bench --site "$SITE" set-config host_name "$URL" >/dev/null
ok "host_name set to $URL"

# The host nginx routes the site's own name to it; more domains: domains.sh
add_domain "$SITE" "$SITE"
apply_proxy && ok "nginx routes $SITE" \
    || warn "$SITE is not reachable until nginx is fixed: run install/install-proxy.sh from the checkout"

if ! is_ip "$SITE"; then
    echo "→ Letting the containers resolve $SITE (backend and workers restart for a few seconds)..."
    apply_tenant_hosts || warn "PDFs may miss images until this is fixed: re-run $TOOLS_DIR/sites/apply-hosts.sh"
fi

echo ""
echo "✅ $SITE is ready: $URL  (login: Administrator)"
if ! is_ip "$SITE"; then
    echo ""
    echo "   Users' computers must resolve $SITE to this server. Either:"
    echo "   - a DNS record (public DNS or your router):  $SITE → <server-ip>"
    echo "   - or, per computer, a hosts-file line:        <server-ip>  $SITE"
fi
echo "   More domains for this site: $TOOLS_DIR/sites/domains.sh add $SITE <domain> [--primary]"
