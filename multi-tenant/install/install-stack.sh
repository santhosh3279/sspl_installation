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
#   HTTP_PORT       external HTTP port (default 80)
#   ERP_DIR, COMPOSE_PROJECT, IMAGE, IMAGE_TAG, BACKUP_ROOT — see the config
#
# Routing: nginx picks the site from the Host header, so FRAPPE_SITE_NAME_HEADER
# is deliberately NOT set — frappe_docker then defaults to $host. Every tenant
# needs its own hostname (DNS rewrite / hosts file) pointing at this server.
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
HTTP_PUBLISH_PORT=$HTTP_PORT
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

echo ""
echo "✅ Stack is up on port $HTTP_PORT. Create the first tenant:"
echo "   $TOOLS_DIR/sites/new-site.sh <host>"
