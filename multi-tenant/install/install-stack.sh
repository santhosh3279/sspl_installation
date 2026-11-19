#!/bin/bash
# Install & start the multi-tenant ERPNext Docker stack. Creates NO site —
# that is sites/new-site.sh, once per tenant.
#
# Env-driven, idempotent, safe to re-run.
#
# Required env:
#   DB_PASSWORD     MariaDB root password for the new database server
# Optional env:
#   SSPL_CONF       config file (default /opt/sspl-erp/sspl-mt.conf); written
#                   from the values below if it does not exist yet
#   HTTP_PORT       public HTTP port, where the host nginx listens (default 80)
#   FRONTEND_PORT   the stack's own nginx, on 127.0.0.1 only (default 18080)
#   ERP_DIR, COMPOSE_PROJECT, IMAGE, IMAGE_TAG, BACKUP_ROOT — see the config
#
# Routing: a host nginx on HTTP_PORT maps each domain to its site and passes
# the site name on as the Host header (install-proxy.sh, sites/domains.sh).
# FRAPPE_SITE_NAME_HEADER is deliberately NOT set — frappe_docker then routes
# on $host. Every domain must resolve to this server (DNS / hosts file).
#
# SECURITY: never echoes DB_PASSWORD. Do not add 'set -x'.
set -euo pipefail

: "${DB_PASSWORD:?DB_PASSWORD is required}"
SSPL_CONF="${SSPL_CONF:-/opt/sspl-erp/sspl-mt.conf}"
export SSPL_CONF

# Write the config first (if new), so common.sh and every tool agree on paths.
if [ ! -f "$SSPL_CONF" ]; then
    mkdir -p "$(dirname "$SSPL_CONF")"
    ( umask 077
      cat > "$SSPL_CONF" <<EOF
# SSPL ERP multi-tenant configuration — read by every tool in tools/.
# Edit freely; tooling updates never overwrite this file.

ERP_DIR=${ERP_DIR:-/opt/sspl-erp}
COMPOSE_PROJECT=${COMPOSE_PROJECT:-sspl-erp}
HTTP_PORT=${HTTP_PORT:-80}
# The stack's own nginx, on 127.0.0.1 only; the host nginx (HTTP_PORT)
# proxies to it. Domains → sites: \$ERP_DIR/site-domains (sites/domains.sh)
FRONTEND_PORT=${FRONTEND_PORT:-18080}
IMAGE=${IMAGE:-ghcr.io/santhosh3279/sspl-erpnext}
IMAGE_TAG=${IMAGE_TAG:-latest}

# Backups: \$BACKUP_ROOT/sites/<site>/{full,db-only}/
BACKUP_ROOT=${BACKUP_ROOT:-/opt/backups/frappe}
RETENTION_DAYS=30        # full backups older than this are removed ...
DB_RETENTION_DAYS=14     # (db-only dumps)
LOCAL_KEEP_MIN=20        # ... but the newest N per site are always kept
CLOUD_KEEP=10            # newest N per site kept on each cloud remote

# Cloud destinations (rclone), space-separated. Empty = no upload.
#   "gdrive:frappe-backups"   or   "gdrive:frappe-backups mega:frappe-backups"
# Each site goes to <remote>/sites/<site>/. Backups carry each site's
# encryption key and DB password: only name remotes you trust.
RCLONE_REMOTE=""
CLEAR_CLOUD_TRASH=yes
MEGA_HARD_DELETE=yes

IMAGE_KEEP=3             # image snapshots kept for rollback
DOCKER_PRUNE=yes         # 'docker image prune' before an update pull; set
                         # 'no' if this server runs other Docker workloads
SERVICE_WAIT_TIMEOUT=180
MAX_AGE_HOURS=26         # verify.sh: newest full backup must be younger
LOCK_FILE=/run/lock/sspl-erp.lock
EOF
    )
    echo "✓ Wrote $SSPL_CONF"
fi

source "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"

# The host nginx takes HTTP_PORT at the end; find a clash before anything is
# installed, not after the stack is half up. (A re-run finds our own nginx
# and frontend on them: skip.)
if [ ! -f "$COMPOSE_FILE" ]; then
    if ss -ltnH "sport = :$HTTP_PORT" 2>/dev/null | grep -q .; then
        err "Port $HTTP_PORT is already in use: $(ss -ltnpH "sport = :$HTTP_PORT" | head -1). Pick another HTTP port."
    fi
    if ss -ltnH "sport = :$FRONTEND_PORT" 2>/dev/null | grep -q .; then
        err "Port $FRONTEND_PORT (FRONTEND_PORT, the stack's internal nginx) is in use. Set FRONTEND_PORT in $SSPL_CONF."
    fi
fi

# ─────────────────────────────────────────────── 1. working dir + frappe_docker
step "Working directory and frappe_docker"
mkdir -p "$ERP_DIR"
if [ -d "$FD_DIR/.git" ]; then
    ok "frappe_docker already cloned — keeping it"
else
    git clone --depth 1 https://github.com/frappe/frappe_docker "$FD_DIR"
fi

# ─────────────────────────────────────────────────────── 2. .env + overrides
step "Environment and overrides"
FD_ENV="$FD_DIR/.env"
[ -f "$FD_ENV" ] && cp "$FD_ENV" "$FD_ENV.bak.$(date +%Y%m%d_%H%M%S)"
( umask 077
cat > "$FD_ENV" <<EOF
CUSTOM_IMAGE=$IMAGE
CUSTOM_TAG=$IMAGE_TAG
PULL_POLICY=missing
DB_PASSWORD=$DB_PASSWORD
WORKER_REPLICAS=2
# Only the host nginx reaches the frontend (install-proxy.sh)
HTTP_PUBLISH_PORT=127.0.0.1:$FRONTEND_PORT
UPSTREAM_REAL_IP_ADDRESS=0.0.0.0/0
# FRAPPE_SITE_NAME_HEADER is intentionally unset: route by Host header.
EOF
)
ok "Wrote $FD_ENV"

( umask 077; printf 'MARIADB_ROOT_PASSWORD=%s\n' "$DB_PASSWORD" > "$ENV_FILE" )
ok "Wrote $ENV_FILE (root password for the tools)"

cat > "$FD_DIR/overrides/compose.sspl.yaml" <<'EOF'
# SSPL: containers can reach the host's published port (PDF rendering
# fetches the site's own URL; see compose.sspl-tenants.yaml for hostnames).
services:
  backend:
    extra_hosts: ["host.docker.internal:host-gateway"]
  queue-short:
    extra_hosts: ["host.docker.internal:host-gateway"]
  queue-long:
    extra_hosts: ["host.docker.internal:host-gateway"]
  scheduler:
    extra_hosts: ["host.docker.internal:host-gateway"]
EOF
# Tenant hostnames: none yet on a fresh stack; new-site.sh regenerates this.
[ -f "$FD_DIR/overrides/compose.sspl-tenants.yaml" ] || write_tenant_hosts_override
ok "Wrote overrides"

# ───────────────────────────────────────────── 3. compose file, pull, start
step "Generating $COMPOSE_FILE"
generate_compose || err "docker compose config failed"
grep -q "FRAPPE_SITE_NAME_HEADER: \$\$host" "$COMPOSE_FILE" \
    || warn "FRAPPE_SITE_NAME_HEADER is not \$\$host in $COMPOSE_FILE — host routing will not work"
ok "Generated (project: $COMPOSE_PROJECT)"

step "Pulling image and starting containers"
docker pull "$IMAGE:$IMAGE_TAG"
dc up -d
wait_for_services || err "Stack did not come up — check: docker compose -f $COMPOSE_FILE logs"
# No global host_name on purpose: each site gets its own (new-site.sh), which
# is what PDFs and email links use.

step "Host nginx (domain → site routing)"
bash "$(dirname "$0")/install-proxy.sh" || err "Host nginx setup failed — fix it, then re-run install/install-proxy.sh"

echo ""
echo "✅ Stack is up on port $HTTP_PORT. Create the first tenant:"
echo "   $TOOLS_DIR/sites/new-site.sh <host>"
