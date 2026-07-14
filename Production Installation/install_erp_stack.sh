#!/bin/bash

# SSPL ERP — install & start the ERPNext Docker stack and create the site.
#
# Fully env-driven and idempotent. Used both by the terminal orchestrator
# (install_sspl_erp.sh) and by the web admin panel's "Install ERPNext"
# switch, so the two paths run the exact same logic.
#
# Required env:
#   SERVER_IP        LAN IP of the server; also the ERPNext site name
#   DB_PASSWORD      MariaDB root password for the new database
#   ADMIN_PASSWORD   ERPNext Administrator password
# Optional env:
#   HTTP_PORT        external HTTP port (default 80)
#   SSPL_ERP_DIR     deployment directory (default /opt/sspl-erp)   [testing]
#   SSPL_IMAGE       image (default ghcr.io/santhosh3279/sspl-erpnext) [testing]
#
# SECURITY: this script never echoes DB_PASSWORD or ADMIN_PASSWORD to stdout
# (they are only written into the 0600 .env file and passed to bench).
# Do NOT add `set -x` — it would leak them into the job log.
#
# NOTE: there is deliberately NO "refuse if already running" guard here —
# that lives in the terminal orchestrator. Re-running is safe: an existing
# site is detected and never re-created.

set -e

ERP_DIR="${SSPL_ERP_DIR:-/opt/sspl-erp}"
FD_DIR="$ERP_DIR/frappe_docker"
COMPOSE_FILE="$ERP_DIR/docker-compose.yml"
IMAGE="${SSPL_IMAGE:-ghcr.io/santhosh3279/sspl-erpnext}"
HTTP_PORT="${HTTP_PORT:-80}"
OWNER="$(id -un)"

err()  { echo "❌ $*" >&2; exit 1; }
step() { echo ""; echo "──────── $* ────────"; }
dc()   { sudo docker compose -f "$COMPOSE_FILE" "$@"; }

[ -n "$SERVER_IP" ]      || err "SERVER_IP is required"
[ -n "$DB_PASSWORD" ]    || err "DB_PASSWORD is required"
[ -n "$ADMIN_PASSWORD" ] || err "ADMIN_PASSWORD is required"

# ─────────────────────────────────────────── 1. working dir + frappe_docker
step "Working directory and frappe_docker"
sudo mkdir -p "$ERP_DIR"
sudo chown "$OWNER:$OWNER" "$ERP_DIR"
if [ -d "$FD_DIR/.git" ]; then
    echo "✓ frappe_docker already cloned — keeping it"
else
    git clone https://github.com/frappe/frappe_docker "$FD_DIR"
fi

# ──────────────────────────────────────────────────── 2. .env + override
step "Environment file and pdf-fix override"
ENV_FILE="$FD_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%Y%m%d_%H%M%S)"
    echo "✓ Existing .env backed up"
fi
# Write the .env (secrets go to the file, never to stdout)
( umask 077
cat > "$ENV_FILE" <<EOF
# Image
CUSTOM_IMAGE=$IMAGE
CUSTOM_TAG=latest
PULL_POLICY=always

# Site — the server's LAN IP
SITE_NAME=$SERVER_IP
FRAPPE_SITE_NAME_HEADER=$SERVER_IP

# Database root password
DB_PASSWORD=$DB_PASSWORD

# Workers
WORKER_REPLICAS=2

# External port: browsers use http://$SERVER_IP:$HTTP_PORT and socket.io
# must be told the same external port (not the internal 9000)
HTTP_PUBLISH_PORT=$HTTP_PORT
SOCKETIO_PORT=$HTTP_PORT
EOF
)
chmod 600 "$ENV_FILE"
echo "✓ Wrote $ENV_FILE"

# The update/rollback scripts read the root password from /opt/sspl-erp/.env
( umask 077; printf 'MARIADB_ROOT_PASSWORD=%s\n' "$DB_PASSWORD" > "$ERP_DIR/.env" )
chmod 600 "$ERP_DIR/.env"
echo "✓ Wrote $ERP_DIR/.env (used by the update/rollback scripts)"

# PDF-fix override: lets wkhtmltopdf inside the containers reach the site
# through host.docker.internal, and pins socketio_port to the external port
cat > "$FD_DIR/overrides/compose.pdf-fix.yaml" <<'EOF'
services:
  backend:
    extra_hosts:
      - "host.docker.internal:host-gateway"
  queue-short:
    extra_hosts:
      - "host.docker.internal:host-gateway"
  queue-long:
    extra_hosts:
      - "host.docker.internal:host-gateway"
  scheduler:
    extra_hosts:
      - "host.docker.internal:host-gateway"
  configurator:
    environment:
      FRAPPE_HOST_NAME: "http://host.docker.internal:${HTTP_PUBLISH_PORT:-8080}"
    command:
      - |
        ls -1 apps > sites/apps.txt
        bench set-config -g db_host $$DB_HOST
        bench set-config -gp db_port $$DB_PORT
        bench set-config -g redis_cache "redis://$$REDIS_CACHE"
        bench set-config -g redis_queue "redis://$$REDIS_QUEUE"
        bench set-config -g redis_socketio "redis://$$REDIS_QUEUE"
        bench set-config -gp socketio_port $$SOCKETIO_PORT
        bench set-config -g host_name "$$FRAPPE_HOST_NAME"
EOF
echo "✓ Wrote overrides/compose.pdf-fix.yaml"

# ───────────────────────────────────────────── 3. generate docker-compose.yml
step "Generating $COMPOSE_FILE"
(cd "$FD_DIR" && sudo docker compose \
    -f compose.yaml \
    -f overrides/compose.mariadb.yaml \
    -f overrides/compose.redis.yaml \
    -f overrides/compose.noproxy.yaml \
    -f overrides/compose.pdf-fix.yaml \
    --env-file .env config) > "$COMPOSE_FILE"
echo "✓ Generated merged compose file"

if ! grep -Eq "published:? \"?$HTTP_PORT\"?" "$COMPOSE_FILE"; then
    echo "⚠ WARNING: port $HTTP_PORT not found in the generated compose file."
    echo "  Check the 'frontend' service ports in $COMPOSE_FILE before going live."
fi

# ───────────────────────────────────────────────── 4. pull + start stack
step "Pulling image and starting containers"
sudo docker pull "$IMAGE:latest"
dc up -d
dc ps

echo "→ Waiting for database to be ready..."
WAITED=0
until dc exec -T -e MYSQL_PWD="$DB_PASSWORD" db mariadb-admin -uroot ping --silent >/dev/null 2>&1; do
    WAITED=$((WAITED + 5))
    if [ "$WAITED" -ge 300 ]; then
        err "Database not ready after 300s — check: sudo docker compose -f $COMPOSE_FILE logs db"
    fi
    sleep 5
done
echo "→ Waiting for backend to be ready..."
until dc exec -T backend true >/dev/null 2>&1; do
    WAITED=$((WAITED + 5))
    if [ "$WAITED" -ge 300 ]; then
        err "Backend not ready after 300s — check: sudo docker compose -f $COMPOSE_FILE logs backend"
    fi
    sleep 5
done
echo "✓ Services are up"

# ────────────────────────────────────────────────── 5. create the site (once)
step "ERPNext site $SERVER_IP"
if dc exec -T backend test -d "sites/$SERVER_IP" 2>/dev/null; then
    echo "✓ Site $SERVER_IP already exists — skipping creation (one-time step)"
else
    # Install every app shipped in the image (erpnext, india_compliance,
    # ssplbilling, printer_server_configuration, frappe_whatsapp, ...)
    APP_ARGS=""
    for app in $(dc exec -T backend ls apps); do
        [ "$app" = "frappe" ] && continue
        APP_ARGS="$APP_ARGS --install-app $app"
    done
    echo "→ Creating site with apps:$(echo "$APP_ARGS" | sed 's/--install-app//g')"
    echo "  (this takes 5-10 minutes — do not interrupt)"
    dc exec -T backend bench new-site "$SERVER_IP" \
        --mariadb-root-password "$DB_PASSWORD" \
        --admin-password "$ADMIN_PASSWORD" \
        $APP_ARGS
    echo "✓ Site created"
fi

dc exec -T backend bench use "$SERVER_IP"
echo "✓ $SERVER_IP set as default site"

# Re-create the site DB user with host '%' so grants survive container
# IP changes (same fix the update script applies after every update)
SITE_CONFIG=$(dc exec -T backend bash -c "cat sites/$SERVER_IP/site_config.json" 2>/dev/null || true)
DB_NAME=$(echo "$SITE_CONFIG" | grep -oP '"db_name":\s*"\K[^"]+' || true)
DB_PASS=$(echo "$SITE_CONFIG" | grep -oP '"db_password":\s*"\K[^"]+' || true)
if [ -n "$DB_NAME" ] && [ -n "$DB_PASS" ]; then
    if dc exec -T -e MYSQL_PWD="$DB_PASSWORD" db mariadb -uroot -e "
        DROP USER IF EXISTS '${DB_NAME}'@'%';
        CREATE USER '${DB_NAME}'@'%' IDENTIFIED BY '${DB_PASS}';
        GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_NAME}'@'%';
        FLUSH PRIVILEGES;" 2>/dev/null; then
        echo "✓ Database grants fixed for '%' host"
    else
        echo "⚠ Grant fix failed (may already be correct)"
    fi
fi

sudo systemctl enable docker >/dev/null 2>&1 || true
echo "✓ Docker auto-start on reboot enabled"

echo ""
echo "✅ ERPNext stack is up — http://$SERVER_IP$( [ "$HTTP_PORT" != 80 ] && echo ":$HTTP_PORT" )"
