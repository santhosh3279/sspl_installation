#!/bin/bash
# Shared configuration and helpers for the SSPL ERP multi-tenant tools.
# Source this from a tool script; do not run it directly.
#
#   source "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"
#
# One bench, one image, many sites. Every site is reached by its own hostname
# (nginx routes on the Host header), so everything here takes a site name and
# nothing assumes there is only one.

# ───────────────────────────────────────────────────────────────── config
# Every tool reads the same config file. SSPL_CONF overrides the path (tests,
# or a second stack on one machine); the defaults below apply for any key the
# file leaves out, so an old config keeps working after a tooling update.
SSPL_CONF="${SSPL_CONF:-/opt/sspl-erp/sspl-mt.conf}"

ERP_DIR=/opt/sspl-erp
COMPOSE_PROJECT=sspl-erp
HTTP_PORT=80          # public port: the host nginx (domain → site router)
FRONTEND_PORT=18080   # the stack's own nginx, published on 127.0.0.1 only
IMAGE=ghcr.io/santhosh3279/sspl-erpnext
IMAGE_TAG=latest
BACKUP_ROOT=/opt/backups/frappe
RETENTION_DAYS=30
DB_RETENTION_DAYS=14
LOCAL_KEEP_MIN=20
CLOUD_KEEP=10
RCLONE_REMOTE=""
CLEAR_CLOUD_TRASH=yes
MEGA_HARD_DELETE=yes
IMAGE_KEEP=3
DOCKER_PRUNE=yes
SERVICE_WAIT_TIMEOUT=180
MAX_AGE_HOURS=26
LOCK_FILE=/run/lock/sspl-erp.lock

if [ -r "$SSPL_CONF" ]; then
    # shellcheck disable=SC1090
    source "$SSPL_CONF"
fi

FD_DIR="$ERP_DIR/frappe_docker"
COMPOSE_FILE="$ERP_DIR/docker-compose.yml"
ENV_FILE="$ERP_DIR/.env"                       # MARIADB_ROOT_PASSWORD, 0600
IMAGE_BACKUP_DIR="$ERP_DIR/image-backups"
SKIP_APPS_DIR="$ERP_DIR/site-skip-apps"        # per-site "never auto-install"
DOMAINS_FILE="$ERP_DIR/site-domains"           # "<domain> <site>" per line
PROXY_CONF=/etc/nginx/conf.d/sspl-erp.conf     # generated from DOMAINS_FILE
TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SITES_BACKUP_DIR="$BACKUP_ROOT/sites"
BENCH_SITES="/home/frappe/frappe-bench/sites"  # inside the containers

# Services that serve users or run jobs. backend/db/redis are not here: every
# maintenance step runs through them.
USER_SERVICES="frontend websocket queue-short queue-long scheduler"
JOB_SERVICES="queue-short queue-long scheduler"

# ──────────────────────────────────────────────────────────────── basics
err()  { echo "❌ $*" >&2; exit 1; }
warn() { echo "   ⚠ $*"; }
ok()   { echo "   ✓ $*"; }
step() { echo ""; echo "──────── $* ────────"; }

dc() { docker compose -f "$COMPOSE_FILE" "$@"; }

# Read a value from the stack .env; f2- keeps values that contain '='
get_env_value() {
    grep "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d '=' -f2-
}

db_root_password() {
    local pw
    pw=$(get_env_value MARIADB_ROOT_PASSWORD)
    if [ -z "$pw" ]; then
        if [ -t 0 ]; then
            read -rsp "MariaDB root password: " pw; echo "" >&2
        fi
    fi
    [ -n "$pw" ] || err "No MariaDB root password (expected MARIADB_ROOT_PASSWORD in $ENV_FILE)"
    printf '%s' "$pw"
}

# ────────────────────────────────────────────────────────────────── sites
#
# Site names end up in paths, in 'bash -c' strings inside the containers, in
# rclone paths and in compose YAML. One strict check, used everywhere, is what
# keeps all of those safe: lowercase hostname or IPv4, nothing else.
SITE_RE='^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$'
RESERVED_SITES="assets archived_sites logs apps.txt"

valid_site_name() {
    local s="$1" r
    [ -n "$s" ] && [ "${#s}" -le 253 ] || return 1
    [[ "$s" =~ $SITE_RE ]] || return 1
    for r in $RESERVED_SITES; do [ "$s" = "$r" ] && return 1; done
    return 0
}

require_valid_site() {
    valid_site_name "$1" || err "'$1' is not a valid site name (lowercase hostname or IP, e.g. acme.erp.lan)"
}

is_ip() { [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; }

# Every folder in sites/ with a site_config.json, one per line, sorted.
# The bench itself is the registry: nothing to keep in sync by hand.
list_sites() {
    dc exec -T backend bash -c '
        cd '"$BENCH_SITES"' || exit 1
        for d in */; do
            d=${d%/}
            [ -f "$d/site_config.json" ] && printf "%s\n" "$d"
        done' 2>/dev/null | tr -d '\r' | while IFS= read -r s; do
            valid_site_name "$s" && printf '%s\n' "$s"
        done | sort
}

site_exists() {
    require_valid_site "$1"
    dc exec -T backend test -f "$BENCH_SITES/$1/site_config.json" 2>/dev/null
}

require_site() {
    require_valid_site "$1"
    if ! site_exists "$1"; then
        echo "❌ No site '$1' on this bench." >&2
        echo "   Sites here:" >&2
        list_sites | sed 's/^/     /' >&2
        exit 1
    fi
}

# Parse the common "--site X | --all" arguments into SITES (array) and REST
# (remaining args). Exits with a usage error on anything malformed.
SITES=()
REST=()
parse_site_args() {
    local all=no
    SITES=(); REST=()
    while [ $# -gt 0 ]; do
        case "$1" in
            --site)  [ -n "${2:-}" ] || err "--site needs a value"
                     require_valid_site "$2"; SITES+=("$2"); shift 2 ;;
            --site=*) require_valid_site "${1#--site=}"; SITES+=("${1#--site=}"); shift ;;
            --all)   all=yes; shift ;;
            *)       REST+=("$1"); shift ;;
        esac
    done
    if [ "$all" = yes ]; then
        [ ${#SITES[@]} -eq 0 ] || err "use --site or --all, not both"
        mapfile -t SITES < <(list_sites)
        [ ${#SITES[@]} -gt 0 ] || err "No sites found on this bench (is the stack up?)"
    fi
    [ ${#SITES[@]} -gt 0 ] || err "Name a site with --site <host>, or pass --all"
}

# '.' in a site name becomes '_' in Frappe's backup file names
site_file_stem() { printf '%s' "${1//./_}"; }

site_backup_dir() { printf '%s/%s' "$SITES_BACKUP_DIR" "$1"; }

site_db_creds() {   # sets DB_NAME and DB_PASS for site $1
    local cfg
    cfg=$(dc exec -T backend cat "$BENCH_SITES/$1/site_config.json" 2>/dev/null)
    DB_NAME=$(printf '%s' "$cfg" | grep -oP '"db_name":\s*"\K[^"]+' || true)
    DB_PASS=$(printf '%s' "$cfg" | grep -oP '"db_password":\s*"\K[^"]+' || true)
}

# ────────────────────────────────────────────────────── stack and services
wait_for_services() {
    local root_pw waited=0
    root_pw=$(db_root_password)
    echo "→ Waiting for database to be ready..."
    until dc exec -T -e MYSQL_PWD="$root_pw" db mariadb-admin -uroot ping --silent >/dev/null 2>&1; do
        waited=$((waited + 5))
        if [ "$waited" -ge "$SERVICE_WAIT_TIMEOUT" ]; then
            echo "   ❌ Database not ready after ${SERVICE_WAIT_TIMEOUT}s"; return 1
        fi
        sleep 5
    done
    echo "→ Waiting for backend to be ready..."
    until dc exec -T backend true >/dev/null 2>&1; do
        waited=$((waited + 5))
        if [ "$waited" -ge "$SERVICE_WAIT_TIMEOUT" ]; then
            echo "   ❌ Backend not ready after ${SERVICE_WAIT_TIMEOUT}s"; return 1
        fi
        sleep 5
    done
    ok "Services are ready"
}

# Keep only the services in $1 that this compose file defines
present_services() {
    local have s out=""
    have=$(dc config --services 2>/dev/null || true)
    for s in $1; do
        printf '%s\n' "$have" | grep -qx "$s" && out="$out $s"
    done
    printf '%s' "${out# }"
}

# Re-create a site's DB user with host '%' so grants survive container IP changes
fix_db_grants() {
    local site="$1" root_pw
    site_db_creds "$site"
    if [ -z "$DB_NAME" ] || [ -z "$DB_PASS" ]; then
        warn "$site: could not read DB credentials, skipping grant fix"; return 0
    fi
    root_pw=$(db_root_password)
    if dc exec -T -e MYSQL_PWD="$root_pw" db mariadb -uroot -e "
        DROP USER IF EXISTS '${DB_NAME}'@'%';
        CREATE USER '${DB_NAME}'@'%' IDENTIFIED BY '${DB_PASS}';
        GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_NAME}'@'%';
        FLUSH PRIVILEGES;" >/dev/null 2>&1; then
        ok "$site: database grants fixed"
    else
        warn "$site: grant fix failed (may already be correct)"
    fi
}

# Write the compose override that lets the containers reach every tenant by
# its own hostname. Needed for PDFs: wkhtmltopdf fetches images and CSS from
# the site's own URL (host_name), and that request has to carry the tenant's
# Host header or nginx cannot tell which site it is for. IP-named sites need
# no entry. $@ = the sites to include.
write_tenant_hosts_override() {
    local f="$FD_DIR/overrides/compose.sspl-tenants.yaml" svc s hosts=()
    for s in "$@"; do is_ip "$s" || hosts+=("$s"); done
    {
        echo "# Generated by the SSPL multi-tenant tools — do not edit by hand."
        echo "# Lets the containers resolve every tenant hostname to this server."
        if [ ${#hosts[@]} -eq 0 ]; then
            echo "{}"
        else
            echo "services:"
            for svc in backend queue-short queue-long scheduler; do
                echo "  $svc:"
                echo "    extra_hosts:"
                for s in "${hosts[@]}"; do echo "      - \"$s:host-gateway\""; done
            done
        fi
    } > "$f"
}

# Regenerate the merged compose file from frappe_docker + our overrides.
generate_compose() {
    local tmp
    tmp=$(mktemp)
    if ! (cd "$FD_DIR" && docker compose \
            --project-name "$COMPOSE_PROJECT" \
            -f compose.yaml \
            -f overrides/compose.mariadb.yaml \
            -f overrides/compose.redis.yaml \
            -f overrides/compose.noproxy.yaml \
            -f overrides/compose.sspl.yaml \
            -f overrides/compose.sspl-tenants.yaml \
            --env-file .env config) > "$tmp"; then
        rm -f "$tmp"; return 1
    fi
    mv -f "$tmp" "$COMPOSE_FILE"
}

# Regenerate the tenant hosts and apply them. 'up -d' recreates only the
# services whose definition changed (backend, workers, scheduler) — a few
# seconds of interruption for every tenant, so callers say so first.
apply_tenant_hosts() {
    local sites=()
    mapfile -t sites < <(list_sites)
    # Mapped domains too: a site's host_name may be one of them, and that is
    # the URL wkhtmltopdf fetches.
    mapfile -t sites < <({ printf '%s\n' "${sites[@]+"${sites[@]}"}"; all_domains; } | grep . | sort -u)
    write_tenant_hosts_override "${sites[@]+"${sites[@]}"}"
    generate_compose || { warn "could not regenerate $COMPOSE_FILE"; return 1; }
    dc up -d --remove-orphans >/dev/null 2>&1 || { warn "docker compose up failed"; return 1; }
    wait_for_services || return 1
    # nginx resolves 'backend' once, at start. The backend was just recreated
    # with a new IP, so without this every tenant gets 502 Bad Gateway.
    dc restart frontend >/dev/null 2>&1 || { warn "could not restart frontend"; return 1; }
}

# ───────────────────────────────────────────── domains → sites (host nginx)
#
# A host nginx owns HTTP_PORT and routes each domain to its site by rewriting
# the Host header, so the stack's own nginx (127.0.0.1:FRONTEND_PORT, routing
# on $host) and Frappe only ever see the site name. One site can answer on
# many domains (erp.acme.com, acme.lan, ...); unknown domains are dropped.
#
# DOMAINS_FILE is the whole routing table, one "<domain> <site>" per line.
# new-site.sh adds "<site> <site>", drop-site.sh removes every line of the
# site, sites/domains.sh manages the rest. Both fields pass valid_site_name,
# which is what makes them safe to write into the nginx config.
#
# nginx only routes. Every domain must still resolve to this server: public
# DNS, the router's DNS, or a hosts-file line on each PC.

# Valid "<domain> <site>" lines of DOMAINS_FILE, comments and junk skipped.
domain_lines() {
    [ -r "$DOMAINS_FILE" ] || return 0
    local d s _
    while read -r d s _; do
        case "$d" in ''|'#'*) continue ;; esac
        valid_site_name "$d" && valid_site_name "$s" && printf '%s %s\n' "$d" "$s"
    done < "$DOMAINS_FILE" | awk '!seen[$1]++'   # first entry wins; nginx rejects duplicates
}

all_domains()  { domain_lines | awk '{print $1}'; }
site_domains() { domain_lines | awk -v s="$1" '$2 == s {print $1}'; }
domain_site()  { domain_lines | awk -v d="$1" '$1 == d && !f {print $2; f = 1}'; }   # no 'exit': SIGPIPE + pipefail

# Rewrite DOMAINS_FILE through a filter command (old file on stdin, new file
# on stdout), atomically.
_edit_domains() {
    local tmp
    mkdir -p "$(dirname "$DOMAINS_FILE")"
    tmp=$(mktemp "$DOMAINS_FILE.XXXXXX")
    if [ -r "$DOMAINS_FILE" ]; then "$@" < "$DOMAINS_FILE"; else "$@" < /dev/null; fi > "$tmp" \
        || { rm -f "$tmp"; err "could not update $DOMAINS_FILE"; }
    chmod 644 "$tmp"
    mv -f "$tmp" "$DOMAINS_FILE"
}

add_domain() {      # $1 domain, $2 site
    require_valid_site "$1"; require_valid_site "$2"
    local cur
    cur=$(domain_site "$1")
    [ -z "$cur" ] || [ "$cur" = "$2" ] || err "$1 already points at $cur"
    [ "$cur" = "$2" ] && return 0
    # shellcheck disable=SC2016
    _edit_domains sh -c 'cat; printf "%s %s\n" "$1" "$2"' _ "$1" "$2"
}

remove_domain()       { _edit_domains awk -v d="$1" '$1 != d'; }   # $1 domain
remove_site_domains() { _edit_domains awk -v s="$1" '$2 != s'; }   # $1 site

proxy_installed() { command -v nginx >/dev/null 2>&1 && [ -d /etc/nginx/conf.d ]; }

# The host nginx config, from DOMAINS_FILE.
render_proxy_conf() {
    local d s
    printf '%s\n' \
        "# Generated by the SSPL multi-tenant tools from $DOMAINS_FILE — do not edit." \
        "# Change domains with: ${SSPL_TOOLS_DIR:-$TOOLS_DIR}/sites/domains.sh" \
        "" \
        'map $host $sspl_site {' \
        '    default "";'
    domain_lines | while read -r d s; do printf '    %-40s %s;\n' "$d" "$s"; done
    sed -e "s/@HTTP_PORT@/$HTTP_PORT/g" -e "s/@FRONTEND_PORT@/$FRONTEND_PORT/g" <<'NGINX'
}

map $http_upgrade $sspl_connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen @HTTP_PORT@ default_server;
    listen [::]:@HTTP_PORT@ default_server;
    server_name _;

    # A domain that is not in the table: close the connection, no page.
    if ($sspl_site = "") { return 444; }

    client_max_body_size 0;      # the stack's own nginx enforces the limit
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;

    location / {
        proxy_pass http://127.0.0.1:@FRONTEND_PORT@;
        proxy_http_version 1.1;
        # The stack's nginx and Frappe route on Host: give them the site name.
        proxy_set_header Host $sspl_site;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $sspl_connection_upgrade;
        # Absolute redirects to the site name would leave the user's domain.
        proxy_redirect http://$sspl_site/ /;
        proxy_redirect http://$sspl_site:@HTTP_PORT@/ /;
    }
}
NGINX
}

# Write the config, test it, reload. A config nginx rejects is rolled back,
# so a bad edit never takes every tenant down.
apply_proxy() {
    proxy_installed || { warn "nginx is not installed — run install/install-proxy.sh from the checkout"; return 1; }
    # A stack from before the proxy: its frontend still owns HTTP_PORT, and a
    # 'listen HTTP_PORT' file here would race it for the port at every boot.
    grep -q '^HTTP_PUBLISH_PORT=127\.0\.0\.1:' "$FD_DIR/.env" 2>/dev/null \
        || { warn "the frontend still holds port $HTTP_PORT — run install/install-proxy.sh from the checkout"; return 1; }
    local tmp bak=""
    tmp=$(mktemp)
    render_proxy_conf > "$tmp"
    if [ -f "$PROXY_CONF" ]; then
        bak=$(mktemp); cp -p "$PROXY_CONF" "$bak"
    fi
    install -m 644 "$tmp" "$PROXY_CONF"; rm -f "$tmp"
    if ! nginx -t >/dev/null 2>&1; then
        nginx -t 2>&1 | tail -3 | sed 's/^/     /' >&2
        if [ -n "$bak" ]; then mv -f "$bak" "$PROXY_CONF"; else rm -f "$PROXY_CONF"; fi
        warn "nginx rejected the new config — the previous one is kept"
        return 1
    fi
    rm -f "$bak"
    if systemctl is-active --quiet nginx; then
        systemctl reload nginx || { warn "nginx reload failed"; return 1; }
    else
        systemctl enable --now nginx >/dev/null 2>&1 \
            || { warn "nginx did not start — systemctl status nginx"; return 1; }
    fi
}

# ─────────────────────────────────────────────── per-site maintenance window
#
# Taking ONE tenant offline without touching the others. Two switches, both in
# the site's own site_config.json (so 'bench restore', which replaces the
# database, does not reset them):
#   maintenance_mode  web requests get the maintenance page
#   pause_scheduler   the scheduler enqueues no new jobs for the site
# Jobs already queued for the site would still run on the shared workers, so
# callers that replace or remove data also stop the shared job services for
# the duration (other tenants' jobs wait in Redis; their web stays up).
#
# The web workers cache each site's config in memory for up to 60 s (Frappe's
# site_cache TTL), so the switch takes that long to reach every worker — in
# both directions. site_offline waits it out before returning: the caller is
# about to replace or delete data, and a worker that still thinks the site is
# up could write into the database mid-replacement.
CONF_CACHE_SECONDS=65
site_offline() {
    dc exec -T backend bench --site "$1" set-maintenance-mode on >/dev/null
    dc exec -T backend bench --site "$1" set-config -p pause_scheduler 1 >/dev/null
    echo "   (waiting ${CONF_CACHE_SECONDS}s until every web worker sees the maintenance switch...)"
    sleep "$CONF_CACHE_SECONDS"
}

site_online() {
    local rc=0
    dc exec -T backend bench --site "$1" set-config -p pause_scheduler 0 >/dev/null || rc=1
    dc exec -T backend bench --site "$1" set-maintenance-mode off >/dev/null || rc=1
    [ $rc -eq 0 ] && echo "   $1 is back online (users may see the maintenance page for up to a minute more)"
    return $rc
}

JOBS_STOPPED=""
jobs_stop() {
    JOBS_STOPPED=$(present_services "$JOB_SERVICES")
    [ -n "$JOBS_STOPPED" ] || return 0
    echo "→ Pausing background jobs for all tenants (their web stays up):$JOBS_STOPPED"
    dc stop $JOBS_STOPPED >/dev/null 2>&1
}
jobs_start() {
    [ -n "$JOBS_STOPPED" ] || return 0
    if dc start $JOBS_STOPPED >/dev/null 2>&1; then
        echo "→ Background jobs resumed"
        JOBS_STOPPED=""
    else
        echo "   ❌ Could not start: $JOBS_STOPPED — start by hand:"
        echo "      docker compose -f $COMPOSE_FILE start $JOBS_STOPPED"
    fi
}

# Whole-stack window, for the update: the image swap affects every tenant.
STACK_OFFLINE=""
stack_offline() {
    STACK_OFFLINE=$(present_services "$USER_SERVICES")
    [ -n "$STACK_OFFLINE" ] || return 0
    echo "→ Taking all tenants offline:$STACK_OFFLINE"
    dc stop $STACK_OFFLINE >/dev/null 2>&1 || true
}
stack_online() {
    [ -n "$STACK_OFFLINE" ] || return 0
    echo "→ Bringing all tenants back online..."
    if dc start $STACK_OFFLINE >/dev/null 2>&1; then
        STACK_OFFLINE=""
    else
        echo "   ❌ Could not start:$STACK_OFFLINE — start by hand:"
        echo "      docker compose -f $COMPOSE_FILE start $STACK_OFFLINE"
    fi
}

# ─────────────────────────────────────────────────────────────────── apps
# Apps installed on a site, one per line. JSON rather than 'list-apps', whose
# layout differs between bench versions.
site_installed_apps() {
    dc exec -T backend bench --site "$1" execute frappe.get_installed_apps 2>/dev/null \
        | python3 -c '
import json, sys
for line in reversed(sys.stdin.read().splitlines()):
    try:
        apps = json.loads(line)
    except ValueError:
        continue
    if isinstance(apps, list):
        print("\n".join(apps))
        break
else:
    sys.exit(1)'
}

# Apps the running image ships, frappe excluded
image_apps() {
    dc exec -T backend ls -1 /home/frappe/frappe-bench/apps 2>/dev/null | tr -d '\r' | grep -vx frappe
}

skip_apps_file() { printf '%s/%s' "$SKIP_APPS_DIR" "$1"; }

skip_app() {   # never auto-install $2 on site $1
    mkdir -p "$SKIP_APPS_DIR"
    grep -qx -- "$2" "$(skip_apps_file "$1")" 2>/dev/null || echo "$2" >> "$(skip_apps_file "$1")"
}

# ─────────────────────────────────────────────────────────────── locking
# One mutating job at a time across all tools: an update, a restore and a
# site drop must never interleave. A child started by a lock holder inherits
# SSPL_LOCK_HELD and does not try again (that would deadlock on itself).
#
# $1 = seconds to wait for the lock (default 0: fail at once). Interactive
# jobs fail fast; cron backups wait, so a 6-hourly dump that lands during the
# nightly full backup runs after it instead of being skipped.
take_lock() {
    local wait="${1:-0}"
    [ -n "${SSPL_LOCK_HELD:-}" ] && return 0
    exec 8>"$LOCK_FILE" || err "Cannot open lock file $LOCK_FILE"
    if ! flock -w "$wait" 8; then
        echo "❌ Another SSPL job is running (lock: $LOCK_FILE). Try again when it finishes." >&2
        exit 75
    fi
    export SSPL_LOCK_HELD=$$
}

# ───────────────────────────────────────────────────────────────── rclone
# Expand RCLONE_REMOTE into RCLONE_TARGETS:
#   ""                            no upload
#   "gdrive:backups"              that one
#   "gdrive:backups mega:backups" both, independently
#   "*:backups"                   every remote 'rclone listremotes' reports
# '*:' picks up remotes added later, silently — and backups carry each site's
# credentials. Naming destinations explicitly is the form that cannot surprise.
RCLONE_TARGETS=()
rclone_expand_targets() {
    local spec="$1" folder r
    RCLONE_TARGETS=()
    case "$spec" in
        "") return 0 ;;
        '*:'*)
            folder="${spec#'*:'}"
            while IFS= read -r r; do
                case "$r" in *:) ;; *) continue ;; esac
                RCLONE_TARGETS+=("${r}${folder}")
            done < <(rclone listremotes </dev/null 2>/dev/null)
            ;;
        *)
            # Deliberate split, with globbing off.
            set -f
            # shellcheck disable=SC2206
            RCLONE_TARGETS=($spec)
            set +f
            ;;
    esac
}

# Flags that make pruning permanent where the backend would otherwise bin it
rclone_prune_flags() {
    PRUNE_FLAGS=()
    case "$1" in
        :*) return 0 ;;
        *:*) ;;
        *) return 0 ;;
    esac
    case "$(rclone config show "${1%%:*}" </dev/null 2>/dev/null \
            | grep -oP '^\s*type\s*=\s*\K\S+' | head -1)" in
        drive) PRUNE_FLAGS=(--drive-use-trash=false) ;;
        mega)  [ "$MEGA_HARD_DELETE" = yes ] && PRUNE_FLAGS=(--mega-hard-delete) ;;
    esac
    return 0
}

# Make room on $1 for $2 (a path), if the remote supports it. Never fatal.
rclone_make_room() {
    local cleanup="$TOOLS_DIR/backup/rclone_trash_cleanup.sh"
    [ "$CLEAR_CLOUD_TRASH" = yes ] && [ -x "$cleanup" ] || return 0
    "$cleanup" --remote "$1" --need-path "$2" || echo "  Proceeding with the upload anyway"
}
