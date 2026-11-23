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
HTTPS_PORT=443        # host nginx HTTPS; "" = HTTP only
HTTPS_REDIRECT=yes    # domains with a Let's Encrypt certificate: HTTP → HTTPS
LETSENCRYPT_EMAIL=""  # expiry notices from Let's Encrypt; "" = none
SERVER_IP=""          # address for http://<ip>:<port> site links; "" = first of hostname -I
SSL_DIR=/etc/sspl-erp/ssl                # default (self-signed) certificate
LE_LIVE=/etc/letsencrypt/live            # certbot's certificates
ACME_WEBROOT=/var/www/sspl-acme          # Let's Encrypt HTTP-01 challenges
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
# Authelia login gate + mail relay (install/install-auth.sh, sites/auth.sh)
AUTH_DIR=/opt/sspl-auth
AUTH_PROJECT=sspl-auth
AUTH_PORT=9091                 # Authelia, on 127.0.0.1 only
AUTH_IMAGE=authelia/authelia:4.39.28
AUTH_REDIS_IMAGE=redis:7.4-alpine
AUTH_REMEMBER_ME=1y            # "Remember me" ticked: no login for this long
AUTH_EXPIRATION=12h            # not ticked: the session ends after this ...
AUTH_INACTIVITY=2h             # ... or this long without a request
AUTH_TRUSTED_NETWORKS=""       # CIDRs where the password alone is enough (no 2FA)
AUTH_BYPASS_NETWORKS=""        # CIDRs that skip the gate entirely (integrations)
SMTP_RELAY_HOST=smtp.gmail.com
SMTP_RELAY_PORT=587
SMTP_USER=""                   # the Gmail address; its app password: $AUTH_DIR/secrets/smtp_password
MAIL_FROM=""                   # sender; "" = SMTP_USER
# WireGuard tunnel to the CUPS print servers (install/install-wireguard.sh, sites/wireguard.sh)
WG_IFACE=sspl-wg
WG_PORT=51820                  # UDP, must be reachable from the print servers
WG_SUBNET=10.99.0.0/24         # always a /24; this server is .1
WG_ENDPOINT=""                 # public IP or DNS name the print servers dial

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
PORTS_FILE="$ERP_DIR/site-ports"               # "<port> <site>" per line
PROXY_CONF=/etc/nginx/conf.d/sspl-erp.conf     # generated from DOMAINS_FILE
AUTH_SITES_FILE="$ERP_DIR/site-auth"           # sites behind Authelia, one per line
WG_PEERS_FILE="$ERP_DIR/wg-peers"              # "<name> <ip> <public key> [site]" per line
WG_DIR=/etc/wireguard                          # keys and the generated interface config
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

# MariaDB memory. Its default 128M InnoDB buffer pool leaves an ERPNext
# database of a GB or more mostly on disk, and every bill save (20+ queries per
# item row) waits on it. The size lives in $ERP_DIR/mariadb/99-sspl.cnf, which
# overrides/compose.sspl-db.yaml mounts on the db container's conf.d. The file
# is written once: the admin panel's RAM monitor changes it later and a re-run
# must not undo that. DB_BUFFER_POOL (e.g. 2G) overrides the auto size, 25% of RAM.
write_db_memory_override() {
    local dir="$ERP_DIR/mariadb" cnf pool total_mb pool_mb
    cnf="$dir/99-sspl.cnf"
    mkdir -p "$dir"
    if [ ! -f "$cnf" ]; then
        pool="${DB_BUFFER_POOL:-}"; pool="${pool^^}"
        if [ -z "$pool" ]; then
            total_mb=$(awk '/^MemTotal:/ {print int($2 / 1024)}' /proc/meminfo)
            pool_mb=$(( total_mb / 4 / 128 * 128 ))
            [ "$pool_mb" -ge 128 ] || pool_mb=128
            pool="${pool_mb}M"
        fi
        [[ "$pool" =~ ^[0-9]+[MG]$ ]] || err "DB_BUFFER_POOL must look like 2G or 1536M (got '$pool')"
        printf '%s\n' "# InnoDB buffer pool for the ERPNext database. Change it from the admin" \
            "# panel's RAM monitor page (it applies live and rewrites this file)." \
            "[mysqld]" "innodb_buffer_pool_size = $pool" > "$cnf"
        # MariaDB skips world-writable option files
        chmod 644 "$cnf"
        ok "MariaDB buffer pool: $pool ($cnf)"
    fi
    cat > "$FD_DIR/overrides/compose.sspl-db.yaml" <<EOF
# SSPL: MariaDB settings from $dir (InnoDB buffer pool, see 99-sspl.cnf).
services:
  db:
    volumes:
      - type: bind
        source: $dir
        target: /etc/mysql/conf.d
        read_only: true
EOF
}

# Regenerate the merged compose file from frappe_docker + our overrides.
generate_compose() {
    local tmp extra=()
    # Stacks installed before the MariaDB memory override get it from the
    # panel's RAM monitor; until then there is no file to include.
    [ -f "$FD_DIR/overrides/compose.sspl-db.yaml" ] && extra=(-f overrides/compose.sspl-db.yaml)
    tmp=$(mktemp)
    if ! (cd "$FD_DIR" && docker compose \
            --project-name "$COMPOSE_PROJECT" \
            -f compose.yaml \
            -f overrides/compose.mariadb.yaml \
            -f overrides/compose.redis.yaml \
            -f overrides/compose.noproxy.yaml \
            -f overrides/compose.sspl.yaml \
            -f overrides/compose.sspl-tenants.yaml \
            ${extra[@]+"${extra[@]}"} \
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

# ───── per-site ports: "<port> <site>" per line in PORTS_FILE. nginx listens
# on each port and sends everything on it to that one site, so a site is
# reachable as http://<server-ip>:<port>/ without any DNS.
valid_port() { [[ "$1" =~ ^[0-9]{1,5}$ ]] && [ "$1" -ge 1 ] && [ "$1" -le 65535 ]; }

port_lines() {
    [ -r "$PORTS_FILE" ] || return 0
    local p s _
    while read -r p s _; do
        case "$p" in ''|'#'*) continue ;; esac
        valid_port "$p" && valid_site_name "$s" && printf '%s %s\n' "$p" "$s"
    done < "$PORTS_FILE" | awk '!seen[$1]++'
}
site_ports() { port_lines | awk -v s="$1" '$2 == s {print $1}'; }
port_site()  { port_lines | awk -v p="$1" '$1 == p && !f {print $2; f = 1}'; }

_edit_ports() {
    local tmp
    mkdir -p "$(dirname "$PORTS_FILE")"
    tmp=$(mktemp "$PORTS_FILE.XXXXXX")
    if [ -r "$PORTS_FILE" ]; then "$@" < "$PORTS_FILE"; else "$@" < /dev/null; fi > "$tmp" \
        || { rm -f "$tmp"; err "could not update $PORTS_FILE"; }
    chmod 644 "$tmp"
    mv -f "$tmp" "$PORTS_FILE"
}
# shellcheck disable=SC2016
add_port()          { _edit_ports sh -c 'cat; printf "%s %s\n" "$1" "$2"' _ "$1" "$2"; }
remove_port()       { _edit_ports awk -v p="$1" '$1 != p'; }
remove_site_ports() { _edit_ports awk -v s="$1" '$2 != s'; }

# The address this server is reached on, for http://<ip>:<port> links and
# host_name. SERVER_IP in the config wins; else the first address.
server_ip() {
    if [ -n "${SERVER_IP:-}" ]; then printf '%s' "$SERVER_IP"; return; fi
    hostname -I 2>/dev/null | awk '{print $1}'
}

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

# Is TCP port $1 held by something other than the host nginx?
port_busy_by_other() {
    local l
    l=$(ss -ltnpH "sport = :$1" 2>/dev/null) || return 1
    [ -n "$l" ] && ! printf '%s' "$l" | grep -q '"nginx"'
}

# Does the host nginx listen on port $1?
nginx_listens() { ss -ltnpH "sport = :$1" 2>/dev/null | grep -q '"nginx"'; }

# ─────────────────────────────────────────────────────────── HTTPS
#
# Every domain gets HTTPS on HTTPS_PORT:
#   - with a self-signed default certificate (install-proxy.sh makes it), so
#     LAN names like acme.erp.lan work at once — browsers show a warning;
#   - with a Let's Encrypt certificate once 'domains.sh cert <domain>' got
#     one (public domains only: Let's Encrypt must reach it on port 80).
#     Those domains are redirected from HTTP to HTTPS (HTTPS_REDIRECT=yes),
#     and a primary one becomes the site's https:// host_name.
# HTTPS_PORT="" turns HTTPS off. Without the default certificate (a stack
# updated from before HTTPS, until install-proxy.sh runs) the config stays
# plain HTTP, so nginx -t never fails on a missing file.

domain_has_cert() { [ -s "$LE_LIVE/$1/fullchain.pem" ] && [ -s "$LE_LIVE/$1/privkey.pem" ]; }
default_cert_ok() { [ -s "$SSL_DIR/default.crt" ] && [ -s "$SSL_DIR/default.key" ]; }
https_on()        { [ -n "$HTTPS_PORT" ] && default_cert_ok; }

# The URL users type for a domain: https when it has a real certificate.
domain_url() {   # $1 domain
    # https when it has a real certificate, or its site is behind Authelia
    # (HTTPS-only, self-signed until it gets one).
    if https_on && { domain_has_cert "$1" || { auth_active && site_auth_linked "$(domain_site "$1")"; }; }; then
        printf 'https://%s' "$1"; [ "$HTTPS_PORT" = 443 ] || printf ':%s' "$HTTPS_PORT"
    else
        printf 'http://%s' "$1"; [ "$HTTP_PORT" = 80 ] || printf ':%s' "$HTTP_PORT"
    fi
}

# Delete a domain's Let's Encrypt certificate (and its renewal). Call after
# apply_proxy has stopped referencing it.
remove_cert() {   # $1 domain
    [ -d "$LE_LIVE/$1" ] || return 0
    command -v certbot >/dev/null 2>&1 || return 0
    certbot delete --cert-name "$1" --non-interactive >/dev/null 2>&1 \
        && ok "Let's Encrypt certificate for $1 deleted" \
        || warn "could not delete the certificate for $1 (certbot delete --cert-name $1)"
}

# ───────────────────────────────────────────── Authelia login gate
#
# install/install-auth.sh runs Authelia, its Redis and a mail relay (Postfix
# → Gmail) as a separate compose project in AUTH_DIR. A site linked with
# 'sites/auth.sh link <site>' is gated: the host nginx asks Authelia about
# every request (auth_request) and sends a browser without a session to the
# login portal, served at /authelia/ on the site's own domain.
#
#   - Users and their sites: AUTH_DIR/users.json (lib/authusers.py). A user
#     may enter the sites whose name is one of their groups; group "admins"
#     enters every linked site.
#   - "Remember me" keeps the session for AUTH_REMEMBER_ME (1y), in Redis
#     (persistent), so a browser that logged in with 2FA is not asked again.
#   - AUTH_TRUSTED_NETWORKS: there the password is enough (no 2FA).
#   - The ERP containers themselves (PDF rendering fetches the site's own
#     URL) and AUTH_BYPASS_NETWORKS skip the gate. nginx decides that from
#     the connection's address, never from a header.
#
# Authelia only accepts https:// portals, so a linked site is HTTPS-only:
# its domains redirect HTTP to HTTPS, and its IP:port addresses switch to
# TLS (self-signed default certificate).
#
# Authelia rules match the hostname, not the port, and every IP:port site
# shares the one IP. So the gate tells Authelia which port it is asking
# about with a marker path, https://<ip>:<port>/__sspl_port/<port>/..., and
# the rules match that path. After the login the browser lands on the
# marker URL; nginx strips it again.

auth_dc() { docker compose -p "$AUTH_PROJECT" -f "$AUTH_DIR/compose.yaml" "$@"; }
auth_installed() { [ -f "$AUTH_DIR/compose.yaml" ] && [ -f "$AUTH_DIR/config/configuration.yml" ]; }

# Linked sites that still exist in the domain table, one per line.
auth_sites() {
    [ -r "$AUTH_SITES_FILE" ] || return 0
    local s
    while read -r s _; do
        case "$s" in ''|'#'*) continue ;; esac
        if valid_site_name "$s"; then printf '%s\n' "$s"; fi
    done < "$AUTH_SITES_FILE" | awk '!seen[$1]++'
}
site_auth_linked() { auth_sites | grep -xF -- "$1" >/dev/null; }   # no -q: SIGPIPE + pipefail
auth_active() { auth_installed && [ -n "$(auth_sites)" ]; }

_edit_auth_sites() {
    local tmp
    mkdir -p "$(dirname "$AUTH_SITES_FILE")"
    tmp=$(mktemp "$AUTH_SITES_FILE.XXXXXX")
    if [ -r "$AUTH_SITES_FILE" ]; then "$@" < "$AUTH_SITES_FILE"; else "$@" < /dev/null; fi > "$tmp" \
        || { rm -f "$tmp"; err "could not update $AUTH_SITES_FILE"; }
    chmod 644 "$tmp"
    mv -f "$tmp" "$AUTH_SITES_FILE"
}
auth_link_site()   { site_auth_linked "$1" || _edit_auth_sites sh -c 'cat; printf "%s\n" "$1"' _ "$1"; }
auth_unlink_site() { _edit_auth_sites awk -v s="$1" '$1 != s'; }

# Authelia's cookie rule: a domain needs a dot, or must be an IP address.
auth_domain_ok() { is_ip "$1" || [[ "$1" == *.* ]]; }

auth_linked_domains() {   # every domain of every linked site
    local s
    auth_sites | while read -r s; do site_domains "$s"; done | awk '!seen[$1]++'
}
auth_linked_ports() {     # "<port> <site>" for ports of linked sites
    local p s
    port_lines | while read -r p s; do
        if site_auth_linked "$s"; then printf '%s %s\n' "$p" "$s"; fi
    done
}

# Session cookie domains: every linked domain (plus the server IP when a
# linked site has a port), minus those under another one — Authelia refuses
# overlapping cookie domains, and the parent's cookie covers its subdomains.
auth_cookie_domains() {
    local all d c under
    all=$( { auth_linked_domains; [ -z "$(auth_linked_ports)" ] || server_ip; echo; } \
           | while read -r d; do if [ -n "$d" ] && auth_domain_ok "$d"; then echo "$d"; fi; done | awk '!seen[$1]++')
    for d in $all; do
        under=no
        if ! is_ip "$d"; then
            for c in $all; do
                [ "$c" != "$d" ] && [[ "$d" == *".$c" ]] && { under=yes; break; }
            done
        fi
        [ "$under" = yes ] || echo "$d"
    done
    return 0
}

auth_portal_url() {   # $1 domain
    printf 'https://%s' "$1"; [ "$HTTPS_PORT" = 443 ] || printf ':%s' "$HTTPS_PORT"; printf '/authelia'
}

# Subnets of the ERP stack's Docker networks: its containers fetch the sites'
# own URLs (PDFs) and must not meet a login page.
erp_stack_subnets() {
    local n
    for n in $(docker network ls -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" 2>/dev/null); do
        docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}}{{"\n"}}{{end}}' "$n" 2>/dev/null
    done | grep -E '^[0-9.]+/[0-9]+$' | sort -u || true
}

valid_cidr() { [[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]{1,2})?$ ]]; }
_cidr_list() { local c; for c in $1; do valid_cidr "$c" && echo "$c"; done; return 0; }

_yq() { printf "'%s'" "${1//\'/\'\'}"; }   # YAML single-quoted scalar

# Authelia's configuration.yml, from the linked sites and the config.
render_authelia_conf() {
    local d s p trusted subj from
    trusted=$(_cidr_list "$AUTH_TRUSTED_NETWORKS")
    from="${MAIL_FROM:-$SMTP_USER}"
    cat <<EOF
# Generated by the SSPL multi-tenant tools — do not edit; sites/auth.sh apply
# rewrites it. Secrets come from /secrets (the *_FILE variables in compose).
theme: auto
server:
  address: 'tcp://:9091/authelia'
log:
  level: info
ntp:
  disable_failure: true
totp:
  issuer: 'SSPL ERP'
authentication_backend:
  # Users are managed by sites/auth.sh / the panel (lib/authusers.py), which
  # owns the users file: Authelia must never rewrite it.
  password_reset:
    disable: true
  password_change:
    disable: true
  # Re-read the user on every request: removing a site from a user, or
  # disabling them, takes effect at once — even on a 1-year session.
  refresh_interval: always
  file:
    path: /config/users_database.yml
    watch: true
    password:
      algorithm: argon2
regulation:
  max_retries: 5
  find_time: 10m
  ban_time: 15m
storage:
  local:
    path: /config/db.sqlite3
session:
  name: sspl_auth_session
  same_site: lax
  expiration: $AUTH_EXPIRATION
  inactivity: $AUTH_INACTIVITY
  remember_me: $AUTH_REMEMBER_ME
  redis:
    host: redis
    port: 6379
  cookies:
EOF
    local cookies
    cookies=$(auth_cookie_domains)
    if [ -z "$cookies" ]; then
        # Authelia needs one; nothing is linked, nothing reaches it.
        printf "    - domain: 'sspl-auth.invalid'\n      authelia_url: 'https://sspl-auth.invalid/authelia'\n"
    fi
    for d in $cookies; do
        printf '    - domain: %s\n      authelia_url: %s\n' "$(_yq "$d")" "$(_yq "$(auth_portal_url "$d")")"
    done
    if [ -n "$SMTP_USER" ]; then
        cat <<EOF
notifier:
  smtp:
    # The relay container; it forwards to $SMTP_RELAY_HOST:$SMTP_RELAY_PORT.
    address: 'smtp://mail:25'
    sender: $(_yq "SSPL ERP <$from>")
    disable_require_tls: true
EOF
    else
        cat <<'EOF'
notifier:
  # No mail relay configured (install-auth.sh SMTP_USER): codes are written here.
  filesystem:
    filename: /config/notification.txt
EOF
    fi
    cat <<'EOF'
access_control:
  default_policy: deny
  rules:
EOF
    # Per-site ports first: the marker path tells them apart on one IP. A
    # deny after each, so a broader rule for the same IP can never match.
    local ip
    ip=$(server_ip)
    auth_linked_ports | while read -r p s; do
        [ -n "$ip" ] || continue
        subj="['group:$s', 'group:admins']"
        if [ -n "$trusted" ]; then
            printf "    - domain: %s\n      resources: ['^/__sspl_port/%s([/?]|\$)']\n      subject: %s\n      networks: [%s]\n      policy: one_factor\n" \
                "$(_yq "$ip")" "$p" "$subj" "$(printf "'%s', " $trusted | sed 's/, $//')"
        fi
        printf "    - domain: %s\n      resources: ['^/__sspl_port/%s([/?]|\$)']\n      subject: %s\n      policy: two_factor\n" \
            "$(_yq "$ip")" "$p" "$subj"
        printf "    - domain: %s\n      resources: ['^/__sspl_port/%s([/?]|\$)']\n      policy: deny\n" "$(_yq "$ip")" "$p"
    done
    # Nothing else may use the marker path.
    printf "    - domain_regex: '^.*\$'\n      resources: ['^/__sspl_port/']\n      policy: deny\n"
    auth_sites | while read -r s; do
        local doms
        doms=$(site_domains "$s" | while read -r d; do if auth_domain_ok "$d"; then printf '%s, ' "$(_yq "$d")"; fi; done | sed 's/, $//')
        [ -n "$doms" ] || continue
        subj="['group:$s', 'group:admins']"
        if [ -n "$trusted" ]; then
            printf "    - domain: [%s]\n      subject: %s\n      networks: [%s]\n      policy: one_factor\n" \
                "$doms" "$subj" "$(printf "'%s', " $trusted | sed 's/, $//')"
        fi
        printf "    - domain: [%s]\n      subject: %s\n      policy: two_factor\n" "$doms" "$subj"
    done
    return 0
}

# Write Authelia's config if it changed, after Authelia itself validated it,
# and restart it (sessions live in Redis: nobody is logged out).
apply_auth() {
    auth_installed || return 0
    local conf="$AUTH_DIR/config/configuration.yml" tmp out
    tmp=$(mktemp "$AUTH_DIR/config/.configuration.XXXXXX")
    render_authelia_conf > "$tmp"
    if cmp -s "$tmp" "$conf"; then rm -f "$tmp"; return 0; fi
    if ! out=$(docker run --rm --network none \
            -v "$tmp:/config/configuration.yml:ro" \
            -e AUTHELIA_SESSION_SECRET_FILE=/secrets/session \
            -e AUTHELIA_STORAGE_ENCRYPTION_KEY_FILE=/secrets/storage \
            -e AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET_FILE=/secrets/jwt \
            -e AUTHELIA_SESSION_REDIS_PASSWORD_FILE=/secrets/redis \
            -v "$AUTH_DIR/secrets:/secrets:ro" \
            "$AUTH_IMAGE" authelia validate-config --config /config/configuration.yml 2>&1); then
        rm -f "$tmp"
        printf '%s\n' "$out" | grep -E '^\s+- ' | sed 's/^\s*/     /' >&2
        warn "Authelia rejected the new config — the previous one is kept"
        return 1
    fi
    chmod 644 "$tmp"
    mv -f "$tmp" "$conf"
    if auth_dc ps --status running --services 2>/dev/null | grep -x authelia >/dev/null; then
        auth_dc restart authelia >/dev/null 2>&1 || { warn "could not restart Authelia"; return 1; }
        auth_wait || return 1
    fi
}

auth_wait() {   # until Authelia answers (it checks Redis and SMTP at start)
    local i
    for i in $(seq 1 60); do
        curl -fsS -o /dev/null "http://127.0.0.1:$AUTH_PORT/authelia/api/health" 2>/dev/null && return 0
        sleep 1
    done
    warn "Authelia did not come up — docker compose -p $AUTH_PROJECT logs authelia"
    return 1
}

# ─────────────────────────────────────────────── host nginx config

# Body of every proxy 'location /' (same for HTTP and HTTPS servers).
# $1 = a fixed site (a per-site port server); default: the domain map.
# $2 = "gate" adds the Authelia check (the server then needs
#      _auth_locations for its /internal/sspl-authz).
_proxy_location() {
    local S='$sspl_site'
    if [ -n "${1:-}" ]; then
        S="$1"
    else
        cat <<'EOF'
        # A domain that is not in the table: close the connection, no page.
        if ($sspl_site = "") { return 444; }
EOF
    fi
    if [ "${2:-}" = gate ]; then
        cat <<'EOF'
        # Authelia: no session → the login page (sites/auth.sh link).
        auth_request /internal/sspl-authz;
        auth_request_set $sspl_auth_redirect $upstream_http_location;
        error_page 401 =302 $sspl_auth_redirect;
EOF
    fi
    cat <<EOF
        proxy_pass http://127.0.0.1:$FRONTEND_PORT;
        proxy_http_version 1.1;
        # The stack's nginx and Frappe route on Host: give them the site name.
        proxy_set_header Host $S;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$sspl_connection_upgrade;
        # Absolute redirects (to the site name, or to an http:// host_name)
        # would leave the user's domain or drop HTTPS: make them relative.
        proxy_redirect http://$S/ /;
        proxy_redirect http://$S:$HTTP_PORT/ /;
        proxy_redirect https://$S/ /;
        proxy_redirect http://\$host/ /;
        proxy_redirect http://\$host:$HTTP_PORT/ /;
        proxy_redirect http://\$http_host/ /;
EOF
}

# The Authelia locations of one server.
#   $1 ""      a domain-map server: gate only linked domains ($sspl_auth_skip)
#      <port>  a linked site's port server: always gated, marker path
#   $2 "portal" also serve the login portal (HTTPS servers)
_auth_locations() {
    local p="${1:-}" skip='$sspl_auth_skip' url='$scheme://$http_host$request_uri'
    if [ -n "$p" ]; then
        skip='$sspl_bypass'
        url="https://$(server_ip):$p/__sspl_port/$p\$request_uri"
    fi
    cat <<EOF

    # Asks Authelia whether this browser may open this URL.
    location = /internal/sspl-authz {
        internal;
        if ($skip) { return 204; }
        proxy_pass http://127.0.0.1:$AUTH_PORT/authelia/api/authz/auth-request;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header Connection "";
        proxy_set_header X-Original-Method \$request_method;
        proxy_set_header X-Original-URL $url;
        # The address nginx saw, never one the client sent: the trusted-LAN
        # rule (password only, no 2FA) depends on it.
        proxy_set_header X-Forwarded-For \$remote_addr;
    }
EOF
    if [ -n "$p" ]; then
        cat <<EOF
    # Back from the login page: drop the port marker.
    location ^~ /__sspl_port/$p/ {
        rewrite ^/__sspl_port/$p(/.*)\$ \$1 redirect;
    }
EOF
        return 0
    fi
    echo '    location ^~ /__sspl_port/ { return 404; }'
    [ "${2:-}" = portal ] || return 0
    cat <<EOF
    # The login portal, on the user's own domain.
    location = /authelia { return 301 /authelia/; }
    location ^~ /authelia/ {
        if (\$sspl_auth_portal = 0) { return 404; }
        proxy_pass http://127.0.0.1:$AUTH_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Host \$http_host;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-URI \$request_uri;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header Connection "";
    }
EOF
}

_proxy_server_common() {
    cat <<'EOF'
    client_max_body_size 0;      # the stack's own nginx enforces the limit
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
EOF
}

# The host nginx config, from DOMAINS_FILE, PORTS_FILE and AUTH_SITES_FILE.
render_proxy_conf() {
    local d s p https=no redirect=no gate="" auth=no
    https_on && https=yes
    [ "$https" = yes ] && [ "$HTTPS_REDIRECT" = yes ] && redirect=yes
    if auth_active; then auth=yes; gate=gate; fi

    cat <<EOF
# Generated by the SSPL multi-tenant tools from $DOMAINS_FILE — do not edit.
# Change domains with: ${SSPL_TOOLS_DIR:-$TOOLS_DIR}/sites/domains.sh

map \$host \$sspl_site {
    default "";
EOF
    domain_lines | while read -r d s; do printf '    %-40s %s;\n' "$d" "$s"; done
    cat <<'EOF'
}

map $http_upgrade $sspl_connection_upgrade {
    default upgrade;
    ''      close;
}

# The ERP containers (PDFs fetch the site's own URL) and AUTH_BYPASS_NETWORKS:
# no login gate, no forced HTTPS. Decided on the connection's address.
geo $sspl_bypass {
    default 0;
EOF
    if [ "$auth" = yes ]; then
        { erp_stack_subnets; _cidr_list "$AUTH_BYPASS_NETWORKS"; } | awk '!seen[$1]++' \
            | while read -r d; do printf '    %-40s 1;\n' "$d"; done
    fi
    cat <<'EOF'
}

# 1 = has a real certificate, 2 = behind Authelia: HTTP is redirected to HTTPS
# (Authelia's cookie is HTTPS-only).
map $host $sspl_https_only {
    default 0;
EOF
    {
        if [ "$redirect" = yes ]; then
            all_domains | while read -r d; do
                if ! is_ip "$d" && domain_has_cert "$d"; then printf '%s 1\n' "$d"; fi
            done
        fi
        if [ "$auth" = yes ] && [ "$https" = yes ]; then auth_linked_domains | sed 's/$/ 2/'; fi
    } | awk '!seen[$1]++ {printf "    %-40s %s;\n", $1, $2}'
    cat <<'EOF'
}
map "$sspl_https_only:$sspl_bypass" $sspl_redirect {
    default 0;
    "~^1:"  1;
    "2:0"   1;
}
EOF
    if [ "$auth" = yes ]; then
        echo ""
        echo "# Sites behind Authelia (sites/auth.sh link)."
        echo 'map $host $sspl_auth {'
        echo '    default 0;'
        auth_linked_domains | while read -r d; do printf '    %-40s 1;\n' "$d"; done
        echo '}'
        cat <<'EOF'
map "$sspl_auth:$sspl_bypass" $sspl_auth_skip {
    default 1;
    "1:0"   0;
}
EOF
        echo '# Where the login portal answers: every linked domain, and the IP for ports.'
        echo 'map $host $sspl_auth_portal {'
        echo '    default 0;'
        { auth_linked_domains; [ -z "$(auth_linked_ports)" ] || server_ip; } | awk 'NF && !seen[$1]++' \
            | while read -r d; do printf '    %-40s 1;\n' "$d"; done
        echo '}'
    fi
    local hp=""
    [ "$HTTPS_PORT" = 443 ] || hp=":$HTTPS_PORT"
    cat <<EOF

server {
    listen $HTTP_PORT default_server;
    listen [::]:$HTTP_PORT default_server;
    server_name _;
$(_proxy_server_common)

    # Let's Encrypt HTTP-01 challenges (domains.sh cert), for any host.
    location ^~ /.well-known/acme-challenge/ {
        root $ACME_WEBROOT;
        default_type text/plain;
    }
$( [ "$auth" = yes ] && _auth_locations "")

    location / {
        if (\$sspl_redirect) { return 301 https://\$host$hp\$request_uri; }
$(_proxy_location "" $gate)
    }
}
EOF
    # Per-site ports: http://<server-ip>:<port>/ is that site, whatever the
    # Host header says (sites/domains.sh port). A linked site's port is
    # HTTPS (self-signed) and gated.
    port_lines | while read -r p s; do
        if [ "$auth" = yes ] && [ "$https" = yes ] && site_auth_linked "$s"; then
            cat <<EOF

server {
    listen $p ssl;
    listen [::]:$p ssl;
    server_name _;
    ssl_certificate     $SSL_DIR/default.crt;
    ssl_certificate_key $SSL_DIR/default.key;
    # Plain HTTP on this TLS port: send the browser to https://.
    error_page 497 =301 https://\$host:\$server_port\$request_uri;
$(_proxy_server_common)
$(_auth_locations "$p")

    location / {
$(_proxy_location "$s" gate)
    }
}
EOF
        else
            cat <<EOF

server {
    listen $p;
    listen [::]:$p;
    server_name _;
$(_proxy_server_common)

    location / {
$(_proxy_location "$s")
    }
}
EOF
        fi
    done
    [ "$https" = yes ] || return 0

    # Default HTTPS server: every domain, self-signed certificate.
    cat <<EOF

server {
    listen $HTTPS_PORT ssl default_server;
    listen [::]:$HTTPS_PORT ssl default_server;
    server_name _;
    ssl_certificate     $SSL_DIR/default.crt;
    ssl_certificate_key $SSL_DIR/default.key;
$(_proxy_server_common)
$( [ "$auth" = yes ] && _auth_locations "" portal)

    location / {
$(_proxy_location "" $gate)
    }
}
EOF
    # One server per domain that has a Let's Encrypt certificate.
    all_domains | while read -r d; do
        is_ip "$d" && continue
        domain_has_cert "$d" || continue
        cat <<EOF

server {
    listen $HTTPS_PORT ssl;
    listen [::]:$HTTPS_PORT ssl;
    server_name $d;
    ssl_certificate     $LE_LIVE/$d/fullchain.pem;
    ssl_certificate_key $LE_LIVE/$d/privkey.pem;
$(_proxy_server_common)
$( [ "$auth" = yes ] && _auth_locations "" portal)

    location / {
$(_proxy_location "" $gate)
    }
}
EOF
    done
}

# Write the config, test it, reload. A config nginx rejects is rolled back,
# so a bad edit never takes every tenant down.
apply_proxy() {
    proxy_installed || { warn "nginx is not installed — run install/install-proxy.sh from the checkout"; return 1; }
    # A stack from before the proxy: its frontend still owns HTTP_PORT, and a
    # 'listen HTTP_PORT' file here would race it for the port at every boot.
    grep -q '^HTTP_PUBLISH_PORT=127\.0\.0\.1:' "$FD_DIR/.env" 2>/dev/null \
        || { warn "the frontend still holds port $HTTP_PORT — run install/install-proxy.sh from the checkout"; return 1; }
    # Another program on HTTPS_PORT: nginx -t would pass (it does not bind),
    # but the reload would silently keep the old config. Render HTTP only.
    local HTTPS_PORT="$HTTPS_PORT"
    if [ -n "$HTTPS_PORT" ] && port_busy_by_other "$HTTPS_PORT"; then
        warn "port $HTTPS_PORT is held by another program — HTTPS is off until it is free: $(ss -ltnpH "sport = :$HTTPS_PORT" | head -1)"
        HTTPS_PORT=""
    fi
    # Authelia first: its cookie domains must cover a domain before nginx
    # sends browsers there to log in.
    if auth_installed; then
        apply_auth || { warn "Authelia config not applied — nginx left unchanged"; return 1; }
        if auth_active && ! https_on; then
            warn "sites are linked to Authelia but HTTPS is off — they refuse every request until HTTPS works"
        fi
    fi
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
    # A reload that could not bind a port reports success and keeps the old
    # config: check nginx really listens.
    local p
    sleep 1
    for p in $HTTP_PORT $(https_on && echo "$HTTPS_PORT") $(port_lines | awk '{print $1}'); do
        nginx_listens "$p" || { warn "nginx is not listening on port $p — journalctl -u nginx"; return 1; }
    done
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

# ────────────────────────────────────────────────────────────── wireguard
# This server is the hub; each CUPS print server is a peer that dials in, so it
# works behind a shop router. Only connections started here (by the host or
# the ERP containers) pass: see WG_DIR/sspl-wg-fw.sh.
WG_NAME_RE='^[a-z0-9][a-z0-9-]{0,30}$'
WG_ENDPOINT_RE='^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$'
WG_SUBNET_RE='^(10|172|192)(\.[0-9]{1,3}){2}\.0/24$'

wg_prefix()    { printf '%s' "${WG_SUBNET%.0/24}"; }
wg_hub_ip()    { printf '%s.1' "$(wg_prefix)"; }
wg_conf_file() { printf '%s/%s.conf' "$WG_DIR" "$WG_IFACE"; }
wg_peer_key()  { printf '%s/sspl-peers/%s.key' "$WG_DIR" "$1"; }
wg_psk()       { printf '%s/sspl-peers/%s.psk' "$WG_DIR" "$1"; }
wg_installed() { [ -s "$WG_DIR/sspl-wg.key" ] && [ -f "$(wg_conf_file)" ]; }

wg_peer_lines() {
    [ -r "$WG_PEERS_FILE" ] || return 0
    awk '!/^#/ && NF >= 3 {print $1, $2, $3, $4}' "$WG_PEERS_FILE"
}
wg_peer_ip() { wg_peer_lines | awk -v n="$1" '$1 == n {print $2}'; }

render_wg_conf() {
    local name ip pub _site
    cat <<EOF
# Generated by the SSPL multi-tenant tools from $WG_PEERS_FILE. Do not edit.
[Interface]
Address = $(wg_hub_ip)/24
ListenPort = $WG_PORT
PrivateKey = $(cat "$WG_DIR/sspl-wg.key")
EOF
    while read -r name ip pub _site; do
        printf '\n[Peer]\n# %s\nPublicKey = %s\nPresharedKey = %s\nAllowedIPs = %s/32\n' \
            "$name" "$pub" "$(cat "$(wg_psk "$name")")" "$ip"
    done < <(wg_peer_lines)
}

# The firewall runs from systemd, not from PostUp: where AppArmor confines
# wg-quick, it may not run a script ("bad interpreter: Permission denied").
# The rules match the interface by name, so they are in place before it exists.
write_wg_unit() {
    local d="/etc/systemd/system/wg-quick@$WG_IFACE.service.d"
    [ -x "$WG_DIR/sspl-wg-fw.sh" ] || { warn "$WG_DIR/sspl-wg-fw.sh is missing — run install/install-wireguard.sh"; return 1; }
    mkdir -p "$d" && cat > "$d/sspl-fw.conf" <<EOF || return 1
# Generated by the SSPL multi-tenant tools. Do not edit.
[Service]
ExecStartPre=$WG_DIR/sspl-wg-fw.sh up %i
ExecStopPost=$WG_DIR/sspl-wg-fw.sh down %i
EOF
    chmod 755 "$d" && chmod 644 "$d/sspl-fw.conf" && systemctl daemon-reload
}

# Write the config; sync the peers into the running interface without
# dropping the other tunnels.
apply_wg() {
    local conf tmp
    conf=$(wg_conf_file)
    write_wg_unit || return 1
    tmp=$(mktemp "$conf.XXXXXX")
    render_wg_conf > "$tmp" || { rm -f "$tmp"; return 1; }
    chmod 600 "$tmp"; mv -f "$tmp" "$conf"
    if [ -d "/sys/class/net/$WG_IFACE" ]; then
        wg-quick strip "$WG_IFACE" | wg syncconf "$WG_IFACE" /dev/stdin
    else
        systemctl restart "wg-quick@$WG_IFACE"
    fi
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
